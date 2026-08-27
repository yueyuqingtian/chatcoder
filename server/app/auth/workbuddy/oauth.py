"""workbuddy（腾讯 CodeBuddy/WorkBuddy）浏览器登录（对齐 CodeBuddy CLI
ExternalLinkAuthenticationProvider，cli-external-link 认证流）。

流程（无需本地回调服务器，比 ta3 简单）：
1. POST {endpoint}/v2/plugin/auth/state?platform=workbuddy（X-No-* 头跳过鉴权注入）
   → {authUrl, state}
2. 前端用系统浏览器打开 authUrl（用户完成扫码/账号密码登录）
3. 后台轮询 GET {endpoint}/v2/plugin/auth/token?state={state}
   （业务错误码 RetryFetchToken 时继续）→ {accessToken, refreshToken, expiresIn}
4. 轮询 GET {endpoint}/v2/plugin/login/account?state={state}（Bearer）
   → {uid, nickname, avatarUrl, enterpriseId}
5. save_auth 落库 → 前端 /login/status 读到 logged_in
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings

logger = logging.getLogger(__name__)

AUTH_STATE_PATH = "/v2/plugin/auth/state"
AUTH_TOKEN_PATH = "/v2/plugin/auth/token"
AUTH_ACCOUNT_PATH = "/v2/plugin/login/account"
PLATFORM = "workbuddy"
# 默认服务端（product.json endpoint，允许用户改环境）
DEFAULT_WORKBUDDY_API_BASE = "https://copilot.tencent.com"

_HTTP_HEADER_DOMAIN = "X-Domain"
_STATE_TIMEOUT = 10.0
_TOKEN_TIMEOUT = 10.0
_ACCOUNT_TIMEOUT = 10.0
# 轮询间隔与总超时（对齐 CLI：ev 轮询、eI 总超时）
_POLL_INTERVAL_S = 2.0
_LOGIN_TIMEOUT_S = 300.0


@dataclass
class LoginResult:
    access_token: str
    refresh_token: str = ""
    account: dict | None = None


def _domain_of(api_base: str) -> str:
    return urlparse(api_base).netloc or ""


def _no_auth_headers(api_base: str) -> dict:
    """登录端点专用头：跳过 Authorization/X-User-Id 等自动注入。"""
    ua = getattr(settings, "workbuddy_user_agent", "") or "WorkBuddy/5.3.14 WorkBuddy/5.3.14 CLI/2.115.0"
    return {
        "Content-Type": "application/json",
        "X-No-Authorization": "true",
        "X-No-User-Id": "true",
        "X-No-Enterprise-Id": "true",
        "X-No-Department-Info": "true",
        _HTTP_HEADER_DOMAIN: _domain_of(api_base),
        "User-Agent": ua,
    }


def _bearer_headers(token: str, api_base: str) -> dict:
    ua = getattr(settings, "workbuddy_user_agent", "") or "WorkBuddy/5.3.14 WorkBuddy/5.3.14 CLI/2.115.0"
    return {
        "Authorization": f"Bearer {token}",
        "X-No-User-Id": "true",
        "X-No-Enterprise-Id": "true",
        "X-No-Department-Info": "true",
        _HTTP_HEADER_DOMAIN: _domain_of(api_base),
        "User-Agent": ua,
    }


def _unwrap(body: dict | None) -> dict:
    """解业务信封 {code, data: {...}} → 内层 dict。"""
    if not isinstance(body, dict):
        return {}
    data = body.get("data")
    return data if isinstance(data, dict) else {}


async def fetch_auth_state(api_base: str) -> dict:
    """POST {api_base}/v2/plugin/auth/state?platform=workbuddy → {authUrl, state}。"""
    url = f"{api_base.rstrip('/')}{AUTH_STATE_PATH}?platform={PLATFORM}"
    try:
        async with httpx.AsyncClient(timeout=_STATE_TIMEOUT) as client:
            resp = await client.post(url, json={}, headers=_no_auth_headers(api_base))
    except httpx.HTTPError as e:
        raise RuntimeError(f"WorkBuddy 登录启动失败：{url}") from e

    body = {}
    try:
        body = resp.json() if resp.content else {}
    except ValueError:
        body = {}
    payload = _unwrap(body)
    auth_url = payload.get("authUrl") or payload.get("auth_url") or ""
    state = payload.get("state") or ""
    if not resp.is_success or not (auth_url and state):
        fallback = f"http_{resp.status_code}"
        msg = ((body.get("msg") or body.get("message") or fallback)
               if isinstance(body, dict) else fallback)
        raise RuntimeError(f"WorkBuddy 登录启动失败：{msg}")
    return {"auth_url": auth_url, "state": state, **payload}


async def poll_auth_token(api_base: str, state: str) -> dict | None:
    """GET {api_base}/v2/plugin/auth/token?state=... → token payload。

    业务错误码 RetryFetchToken（未完成扫码）时返回 None，调用方继续轮询。
    """
    url = f"{api_base.rstrip('/')}{AUTH_TOKEN_PATH}?state={state}"
    try:
        async with httpx.AsyncClient(timeout=_TOKEN_TIMEOUT) as client:
            resp = await client.get(url, headers=_no_auth_headers(api_base))
    except httpx.HTTPError as e:
        raise RuntimeError(f"WorkBuddy 登录状态查询失败：{url}") from e

    body = {}
    try:
        body = resp.json() if resp.content else {}
    except ValueError:
        body = {}
    payload = _unwrap(body)
    if not payload:
        return None
    if payload.get("accessToken") or payload.get("access_token"):
        return payload
    return None


async def poll_auth_account(api_base: str, token: str, state: str) -> dict:
    """GET {api_base}/v2/plugin/login/account?state=...（Bearer）→ 账号信息。"""
    url = f"{api_base.rstrip('/')}{AUTH_ACCOUNT_PATH}?state={state}"
    try:
        async with httpx.AsyncClient(timeout=_ACCOUNT_TIMEOUT) as client:
            resp = await client.get(url, headers=_bearer_headers(token, api_base))
    except httpx.HTTPError as e:
        raise RuntimeError(f"WorkBuddy 账号信息查询失败：{url}") from e
    body = {}
    try:
        body = resp.json() if resp.content else {}
    except ValueError:
        body = {}
    return _unwrap(body)


async def start_login(db: AsyncSession, provider_id: int, api_base: str) -> dict:
    """登录入口：发起 state → 启动后台轮询 → 返回 pending + auth_url。

    前端用系统浏览器打开 auth_url 完成登录后，轮询 /login/status 取结果。
    """
    from app.auth.workbuddy import session as wb_session

    state_payload = await fetch_auth_state(api_base)
    auth_url = state_payload["auth_url"]
    state = state_payload["state"]

    async def _poll_loop() -> dict:
        deadline = asyncio.get_event_loop().time() + _LOGIN_TIMEOUT_S
        token_payload = None
        while asyncio.get_event_loop().time() < deadline:
            try:
                token_payload = await poll_auth_token(api_base, state)
            except RuntimeError:
                token_payload = None
            if token_payload:
                break
            await asyncio.sleep(_POLL_INTERVAL_S)
        if not token_payload:
            return {"status": "failed", "error": "登录超时，请重试"}
        access_token = (token_payload.get("accessToken")
                        or token_payload.get("access_token") or "")
        if not access_token:
            return {"status": "failed", "error": "登录返回异常（无 accessToken）"}
        refresh_token = (token_payload.get("refreshToken")
                         or token_payload.get("refresh_token") or "")
        account_raw = await poll_auth_account(api_base, access_token, state)
        account = {
            "id": account_raw.get("uid") or account_raw.get("id") or "",
            "uid": account_raw.get("uid") or "",
            "nickname": account_raw.get("nickname") or account_raw.get("name") or "",
            "avatarUrl": account_raw.get("avatarUrl") or "",
            "enterpriseId": account_raw.get("enterpriseId") or "",
            "label": account_raw.get("nickname") or account_raw.get("name") or "已登录",
        }
        await wb_session.save_auth(
            db, provider_id,
            access_token=access_token,
            refresh_token=refresh_token or None,
            account=account,
        )
        await db.commit()
        logger.info("[workbuddy] provider=%s 浏览器登录完成", provider_id)
        return {"status": "logged_in", "account": account}

    task = asyncio.create_task(_poll_loop())
    _active_tasks[provider_id] = task

    return {
        "status": "pending",
        "auth_url": auth_url,
        "state": state,
        "expires_in": int(_LOGIN_TIMEOUT_S),
    }


async def get_login_status(db: AsyncSession, provider_id: int) -> dict:
    """查询登录状态：优先 in-flight 任务结果，其次 DB 登录态。"""
    from app.auth.workbuddy import session as wb_session

    task = _active_tasks.get(provider_id)
    if task is not None:
        if task.done():
            _active_tasks.pop(provider_id, None)
            try:
                return await task
            except Exception as e:  # noqa: BLE001
                return {"status": "failed", "error": str(e)[:300]}
        return {"status": "pending"}

    row = await wb_session.load_auth(db, provider_id)
    if row and row.access_token:
        return {"status": "logged_in", "account": row.account or {}}
    return {"status": "pending", "error": "未登录"}


async def cancel_login(db: AsyncSession, provider_id: int) -> None:
    task = _active_tasks.pop(provider_id, None)
    if task is not None:
        task.cancel()


# provider_id → in-flight 登录协程（进程内，重启即失效——登录本就是短生命周期操作）
_active_tasks: dict[int, object] = {}
