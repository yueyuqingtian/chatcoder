"""workbuddy 登录态存储与会话管理。

- load/save/clear：WorkBuddyAuth 表单行读写
- ensure_token：返回当前 access_token（不预判过期，401 兜底刷新）
- refresh_session：业务请求 401 时自动 refresh（带 in-flight 锁防并发 stampede，
  对齐 CodeBuddy CLI ExternalLinkAuthenticationProvider.refreshSession：
  POST /v2/plugin/auth/token/refresh + X-Refresh-Token 头）
- refresh_token 失效 → 清会话并抛错，调用方提示重新登录
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.persistence.models.workbuddy_auth import WorkBuddyAuth

logger = logging.getLogger(__name__)

TOKEN_REFRESH_PATH = "/v2/plugin/auth/token/refresh"
# 对齐 CLI refreshSession：refresh_token 走独立请求头
_X_REFRESH_TOKEN = "X-Refresh-Token"
_X_REFRESH_SOURCE = "X-Auth-Refresh-Source"
_HTTP_HEADER_DOMAIN = "X-Domain"
_TOKEN_TIMEOUT = 10.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _domain_of(api_base: str) -> str:
    from urllib.parse import urlparse

    return urlparse(api_base).netloc or ""


async def load_auth(db: AsyncSession, provider_id: int) -> WorkBuddyAuth | None:
    res = await db.execute(select(WorkBuddyAuth).where(WorkBuddyAuth.provider_id == provider_id))
    return res.scalars().first()


async def get_auth_row(db: AsyncSession, provider_id: int) -> WorkBuddyAuth:
    """取或建（惰性创建）登录态行。"""
    row = await load_auth(db, provider_id)
    if row is None:
        row = WorkBuddyAuth(provider_id=provider_id, updated_at=_now())
        db.add(row)
        await db.flush()
    return row


async def save_auth(db: AsyncSession, provider_id: int, *, access_token: str,
                    refresh_token: str | None = None, account: dict | None = None,
                    catalog: dict | None = None) -> WorkBuddyAuth:
    row = await get_auth_row(db, provider_id)
    row.access_token = access_token
    if refresh_token is not None:
        row.refresh_token = refresh_token
    if account is not None:
        row.account = account
    if catalog is not None:
        row.catalog = catalog
    row.updated_at = _now()
    await db.flush()
    return row


async def clear_auth(db: AsyncSession, provider_id: int) -> None:
    row = await load_auth(db, provider_id)
    if row is not None:
        await db.delete(row)
        await db.flush()


async def get_access_token(db: AsyncSession, provider_id: int) -> str | None:
    row = await load_auth(db, provider_id)
    return row.access_token if row else None


# refresh 续期 in-flight 锁（按 provider_id），防多个 401 并发触发 stampede
# （refresh_token 轮转会让并发的第二次失败）
_refresh_locks: dict[int, object] = {}


def _get_refresh_lock(provider_id: int):
    import asyncio

    lock = _refresh_locks.get(provider_id)
    if lock is None:
        lock = asyncio.Lock()
        _refresh_locks[provider_id] = lock
    return lock


class WorkBuddyAuthError(Exception):
    """workbuddy 认证错误。kind: login_required | refresh_failed | network"""

    def __init__(self, message: str, kind: str = "login_required"):
        super().__init__(message)
        self.kind = kind


async def refresh_access_token(api_base: str, access_token: str, refresh_token: str) -> dict:
    """POST {api_base}/v2/plugin/auth/token/refresh，refresh_token 轮转。

    对齐 CLI：Authorization: Bearer {access_token} + X-Refresh-Token: {refresh_token}
    + X-Auth-Refresh-Source: plugin + X-Domain。
    返回 {accessToken, refreshToken, ...}；失败抛 WorkBuddyAuthError。
    """
    url = f"{api_base.rstrip('/')}{TOKEN_REFRESH_PATH}"
    ua = getattr(settings, "workbuddy_user_agent", "") or "WorkBuddy/5.3.14 WorkBuddy/5.3.14 CLI/2.115.0"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
        _X_REFRESH_TOKEN: refresh_token,
        _X_REFRESH_SOURCE: "plugin",
        _HTTP_HEADER_DOMAIN: _domain_of(api_base),
        "User-Agent": ua,
    }
    try:
        async with httpx.AsyncClient(timeout=_TOKEN_TIMEOUT) as client:
            resp = await client.post(url, json={}, headers=headers)
    except httpx.HTTPError as e:
        raise WorkBuddyAuthError(f"WorkBuddy token 刷新请求失败：{url}", "network") from e

    body = {}
    try:
        body = resp.json() if resp.content else {}
    except ValueError:
        body = {}
    payload = body.get("data") if isinstance(body, dict) else {}
    if not isinstance(payload, dict):
        payload = {}
    access_token_new = payload.get("accessToken") or payload.get("access_token") or ""
    if not resp.is_success or not access_token_new:
        code = (body or {}).get("code") or f"http_{resp.status_code}"
        if resp.status_code in (401, 403) or code in ("invalid_grant", 401, 403):
            msg = "登录已过期（refresh_token 失效），请重新登录"
            raise WorkBuddyAuthError(msg, "login_required")
        raise WorkBuddyAuthError(f"WorkBuddy token 刷新失败：{code}", "refresh_failed")
    return {
        "accessToken": access_token_new,
        "refreshToken": payload.get("refreshToken") or payload.get("refresh_token") or "",
    }


async def ensure_token(db: AsyncSession, provider_id: int, api_base: str) -> str:
    """返回可用 access_token；未登录抛 WorkBuddyAuthError。

    不预判过期（对齐 CLI：不做定时刷新，依赖 401 兜底 refresh）。
    """
    row = await load_auth(db, provider_id)
    if row is None or not row.access_token:
        raise WorkBuddyAuthError("请先登录 WorkBuddy 账号", "login_required")
    return row.access_token


async def refresh_session(db: AsyncSession, provider_id: int, api_base: str) -> str:
    """业务请求 401 时刷新会话，返回新 access_token。

    refresh_token 轮转防并发由 in-flight 锁保证；失效时清会话抛 login_required。
    """
    row = await load_auth(db, provider_id)
    if row is None or not row.access_token or not row.refresh_token:
        raise WorkBuddyAuthError("请先登录 WorkBuddy 账号", "login_required")

    lock = _get_refresh_lock(provider_id)
    async with lock:
        # 双检：等待锁期间可能已被其它协程刷新
        row = await load_auth(db, provider_id)
        if row is None or not row.access_token or not row.refresh_token:
            raise WorkBuddyAuthError("请先登录 WorkBuddy 账号", "login_required")
        try:
            result = await refresh_access_token(api_base, row.access_token, row.refresh_token)
        except WorkBuddyAuthError as e:
            if e.kind == "login_required":
                await clear_auth(db, provider_id)
                await db.commit()
            raise
        row.access_token = result["accessToken"]
        if result.get("refreshToken"):
            row.refresh_token = result["refreshToken"]
        row.updated_at = _now()
        await db.flush()
        await db.commit()
        logger.info("[workbuddy] provider=%s token 已刷新", provider_id)
        return row.access_token


async def mark_login_required(db: AsyncSession, provider_id: int) -> None:
    """业务请求 401 且无 refresh_token 可用时，清会话要求重登。"""
    await clear_auth(db, provider_id)
    await db.commit()
