"""plan-20261106-ta3-080：隔离各供应商的入口与网络故障，避免鉴权错误引发地址漂移。"""
from __future__ import annotations

import asyncio
import ssl
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

import httpx

DEFAULT_TA3_API_BASE = "https://newcoder-inner.yinhaiyun.com/ta3api"
DEFAULT_ALT_BASES = ["https://newcoder.yinhaiyun.com/ta3api"]
LEGACY_API_BASE = "https://lc.yinhaiyun.com/newcoder"
OAUTH_CLIENT_ID = "ta3-desktop"
AUTH_VERSION = 2


def normalize_base(value: str) -> str:
    value = str(value or "").strip().rstrip("/")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as exc:
        raise ValueError("TA3 地址格式无效") from exc
    if (parts.scheme not in ("http", "https") or not parts.hostname or parts.username is not None
            or parts.password is not None or parts.query or parts.fragment
            or any(c.isspace() for c in value) or "\\" in value):
        raise ValueError("TA3 地址须为不含账号、查询参数或片段的完整 HTTP/HTTPS API 基址")
    del port
    return value


def normalize_alternates(values: list[str], primary: str) -> list[str]:
    return list(dict.fromkeys(base for value in values if (base := normalize_base(value)) != primary))


def candidates(provider) -> list[str]:
    primary = normalize_base(getattr(provider, "base_url", None) or DEFAULT_TA3_API_BASE)
    alternates = getattr(provider, "ta3_alt_base_urls", None)
    if alternates is None:
        alternates = DEFAULT_ALT_BASES if primary == DEFAULT_TA3_API_BASE else []
    return [primary, *normalize_alternates(alternates, primary)]


def front_url(api_base: str) -> str:
    p = urlsplit(api_base)
    netloc = p.netloc
    if p.port == 8081:
        netloc = netloc.rsplit(":", 1)[0] + ":8080"
    return urlunsplit((p.scheme, netloc, "/newcoder/index.html", "", ""))


def degradable(error: BaseException) -> bool:
    current = error
    while current is not None:
        if isinstance(current, ssl.SSLError) or "certificate" in str(current).lower():
            return False
        current = current.__cause__ or current.__context__
    return isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout,
                              httpx.WriteTimeout, httpx.ReadError, httpx.WriteError))


@dataclass
class Resolution:
    bases: tuple[str, ...]
    resolved: str = ""
    failed_at: float = 0
    cooling: dict[str, float] = field(default_factory=dict)
    unavailable: dict[tuple[str, str], float] = field(default_factory=dict)
    endpoint_bases: dict[str, str] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_states: dict[int, Resolution] = {}


def state_for(provider) -> Resolution:
    key = provider.id
    bases = tuple(candidates(provider))
    state = _states.get(key)
    if state is None or state.bases != bases:
        state = _states[key] = Resolution(bases)
    return state


def current_base(provider, endpoint: str = "") -> str:
    state = state_for(provider)
    if endpoint and endpoint in state.endpoint_bases:
        return state.endpoint_bases[endpoint]
    return state.resolved or state.bases[0]


def resolved_base(provider) -> str | None:
    return state_for(provider).resolved or None


def reset(provider_id: int) -> None:
    _states.pop(provider_id, None)


def client_options(provider, overrides=None) -> dict:
    from app.services.credential_service import provider_client_kwargs
    return {**provider_client_kwargs(provider), **(overrides or {}), "follow_redirects": False}


async def resolve(provider, *, client_kwargs=None) -> str:
    state = state_for(provider)
    async with state.lock:
        if state.resolved:
            return state.resolved
        if len(state.bases) == 1:
            state.resolved = state.bases[0]
            return state.resolved
        now = time.monotonic()
        if state.failed_at and now - state.failed_at < 15:
            return state.bases[0]
        order = [b for b in state.bases if state.cooling.get(b, 0) <= now] or list(state.bases)
        async with httpx.AsyncClient(timeout=1, **client_options(provider, client_kwargs)) as client:
            for base in order:
                try:
                    response = await client.get(base + "/", headers={"Cache-Control": "no-cache"})
                except httpx.HTTPError as exc:
                    if not degradable(exc):
                        raise
                    state.cooling[base] = now + 60
                    continue
                text = response.text.lstrip()
                business = text.startswith("{") and ('"serviceSuccess"' in text or '"errorCode"' in text)
                if response.status_code in (404, 405, 501) and not business:
                    continue
                state.resolved = base
                state.failed_at = 0
                return base
        state.failed_at = time.monotonic()
        return state.bases[0]


async def request(provider, method: str, path: str, *, timeout=10, client_kwargs=None,
                  endpoint: str = "", **kwargs) -> httpx.Response:
    """plan-20261106-ta3-080：写操作结果未知时禁止向备用地址重放。"""
    await resolve(provider, client_kwargs=client_kwargs)
    state = state_for(provider)
    attempted: set[str] = set()
    last_error = None
    last_response = None
    readonly = method.upper() == "GET" or path.startswith("/ai/continue/ide/list-")
    while len(attempted) < len(state.bases):
        now = time.monotonic()
        ordered = [state.endpoint_bases.get(endpoint, ""), state.resolved, *state.bases]
        available = [b for b in ordered if b and b not in attempted
                     and (not endpoint or state.unavailable.get((b, endpoint), 0) <= now)]
        if not available:
            break
        base = next((b for b in available if state.cooling.get(b, 0) <= now), available[0])
        attempted.add(base)
        try:
            async with httpx.AsyncClient(timeout=timeout, **client_options(provider, client_kwargs)) as client:
                response = await client.request(method, base + path, **kwargs)
        except httpx.HTTPError as exc:
            if not degradable(exc):
                raise
            state.cooling[base] = time.monotonic() + 60
            if state.resolved == base:
                state.resolved = ""
            last_error = exc
            if not readonly:
                raise
            continue
        last_response = response
        if readonly and endpoint and response.status_code in (404, 405, 501):
            state.unavailable[base, endpoint] = time.monotonic() + 300
            continue
        if endpoint and response.is_success:
            state.endpoint_bases[endpoint] = base
        if not state.resolved:
            state.resolved = base
        return response
    if last_error:
        raise last_error
    if last_response is not None:
        return last_response
    raise httpx.ConnectError("TA3 该接口所有候选地址暂不可用")
