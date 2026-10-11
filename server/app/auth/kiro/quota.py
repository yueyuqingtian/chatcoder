"""kiro 剩余积分（额度）查询。

复刻 CodeWhisperer RTS GetUsageLimits（Kiro IDE 积分计同源）：

  GET {quota}?isEmailRequired=true&origin=AI_EDITOR&resourceType=AGENTIC_REQUEST&profileArn=…

响应按 usageBreakdownList 解析：resourceType=CREDIT 行的
currentUsage / usageLimit（及 *_WithPrecision 高精度版本）、nextDateReset。

缓存 10s（按 provider + token 失效；force 跳过）；401 刷新后重试一次。
"""
from __future__ import annotations

import logging
import time
from urllib.parse import urlencode

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.kiro import session as kiro_session

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 15.0
_CACHE_SECONDS = 10.0

# provider_id -> (token, at_monotonic, value)
_cache: dict[int, tuple[str, float, dict]] = {}


class KiroQuotaError(RuntimeError):
    """额度服务错误。kind: network | timeout | http | parse"""

    def __init__(self, message: str, kind: str = "http"):
        super().__init__(message)
        self.kind = kind


def clear_cache(provider_id: int) -> None:
    """清 provider 的额度缓存（登出/退出登录时调用，防残留展示）。"""
    _cache.pop(provider_id, None)


def _as_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pick(entry: dict, *names: str) -> object:
    for name in names:
        if name in entry and entry[name] is not None:
            return entry[name]
    return None


def normalize_usage(body: dict) -> dict:
    """把 GetUsageLimits 响应规范化为前端友好的积分视图。"""
    breakdown: list[dict] = []
    raw_list = _pick(body, "usageBreakdownList", "usageBreakdown", "breakdowns") or []
    total_used = 0.0
    total_limit = 0.0
    primary: dict | None = None

    if isinstance(raw_list, list):
        for entry in raw_list:
            if not isinstance(entry, dict):
                continue
            rtype = str(_pick(entry, "resourceType", "type") or "").upper()
            used = _as_float(_pick(entry, "currentUsageWithPrecision", "currentUsage", "used")) or 0.0
            limit = _as_float(_pick(entry, "usageLimitWithPrecision", "usageLimit", "limit")) or 0.0
            item = {"type": rtype or "CREDIT", "used": used, "limit": limit,
                    "remaining": max(0.0, limit - used)}
            breakdown.append(item)
            if rtype == "CREDIT" or primary is None:
                if primary is None or rtype == "CREDIT":
                    primary = item
            total_used += used
            total_limit += limit

    if primary is None:
        used = _as_float(_pick(body, "currentUsage", "used")) or 0.0
        limit = _as_float(_pick(body, "usageLimit", "limit")) or 0.0
        primary = {"type": "CREDIT", "used": used, "limit": limit,
                   "remaining": max(0.0, limit - used)}

    reset_at = _pick(body, "nextDateReset", "resetAt", "resetDate")
    return {
        "available": bool(primary and primary.get("limit")),
        "used": primary.get("used", 0.0),
        "limit": primary.get("limit", 0.0),
        "remaining": primary.get("remaining", 0.0),
        "reset_at": str(reset_at) if reset_at else None,
        "unit": "credits",
        "breakdown": breakdown,
        "raw": body,
    }


async def get_usage_limits(
    db: AsyncSession, provider_id: int, provider, *, force: bool = False,
    client_kwargs: dict | None = None,
) -> dict:
    """查询剩余积分。401 时刷新令牌后重试一次。"""
    row = await kiro_session.load_auth(db, provider_id)
    if row is None or not row.access_token:
        raise kiro_session.KiroAuthError("请先登录 Kiro 账号", "login_required")

    token = await kiro_session.ensure_token(db, provider_id, provider)
    cached = _cache.get(provider_id)
    if (not force and cached and cached[0] == token
            and time.monotonic() - cached[1] < _CACHE_SECONDS):
        return cached[2]

    endpoints = kiro_session.endpoints_for(provider, row)
    params = {
        "isEmailRequired": "true",
        "origin": "AI_EDITOR",
        "resourceType": "AGENTIC_REQUEST",
    }
    if row.profile_arn:
        params["profileArn"] = row.profile_arn
    url = f"{endpoints.quota}?{urlencode(params)}"

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    token_holder = {"token": token}

    async def _do(t: str) -> httpx.Response:
        h = dict(headers)
        h["Authorization"] = f"Bearer {t}"
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT, **(client_kwargs or {})) as client:
            return await client.get(url, headers=h)

    try:
        resp = await _do(token_holder["token"])
    except httpx.TimeoutException as e:
        raise KiroQuotaError("积分服务请求超时", "timeout") from e
    except httpx.HTTPError as e:
        raise KiroQuotaError("无法连接积分服务，请检查网络", "network") from e

    if resp.status_code == 401:
        new_token = await kiro_session.force_refresh(db, provider_id, provider)
        token_holder["token"] = new_token
        try:
            resp = await _do(new_token)
        except httpx.HTTPError as e:
            raise KiroQuotaError("无法连接积分服务，请检查网络", "network") from e
        if resp.status_code == 401:
            raise kiro_session.KiroAuthError("Kiro 登录已过期，请重新登录", "login_required")

    if not resp.is_success:
        raise KiroQuotaError(f"积分服务请求失败：HTTP {resp.status_code}", "http")
    try:
        body = resp.json() if resp.content else {}
    except ValueError as e:
        raise KiroQuotaError("积分服务返回了无法解析的数据", "parse") from e
    if not isinstance(body, dict):
        raise KiroQuotaError("积分服务返回了非对象数据", "parse")

    value = normalize_usage(body)
    account = (row.account or {}).get("label")
    if account:
        value["account"] = account
    _cache[provider_id] = (token_holder["token"], time.monotonic(), value)
    return value
