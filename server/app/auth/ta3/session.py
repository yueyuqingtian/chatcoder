"""ta3 登录态存储与会话管理。

- load/save/clear：Ta3Auth 表单行读写（对齐参考项目 auth-session.json 结构）
- ensure_token：业务请求 401 时自动 refresh（带 in-flight 锁防并发 stampede，
  对齐参考项目 authService.ts:787-792 refreshIfNeeded）
- refresh_token 失效（invalid_grant）→ 清会话并抛错，调用方提示重新登录
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.ta3_auth import Ta3Auth

logger = logging.getLogger(__name__)

OAUTH_TOKEN_PATH = "/api/oauth/token"
YINHAI_OAUTH_CLIENT_ID = "ide-vscode"  # 复用服务端已注册 ClientRegistry（参考项目 auth/settings.ts:44）
_TOKEN_TIMEOUT = 10.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def load_auth(db: AsyncSession, provider_id: int) -> Ta3Auth | None:
    res = await db.execute(select(Ta3Auth).where(Ta3Auth.provider_id == provider_id))
    return res.scalars().first()


async def get_auth_row(db: AsyncSession, provider_id: int) -> Ta3Auth:
    """取或建（惰性创建）登录态行。"""
    row = await load_auth(db, provider_id)
    if row is None:
        row = Ta3Auth(provider_id=provider_id, updated_at=_now())
        db.add(row)
        await db.flush()
    return row


async def save_auth(db: AsyncSession, provider_id: int, *, access_token: str,
                    refresh_token: str | None = None, account: dict | None = None,
                    catalog: dict | None = None) -> Ta3Auth:
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
_refresh_locks: dict[int, asyncio.Lock] = {}


def _get_refresh_lock(provider_id: int):
    import asyncio
    lock = _refresh_locks.get(provider_id)
    if lock is None:
        lock = asyncio.Lock()
        _refresh_locks[provider_id] = lock
    return lock


class Ta3AuthError(Exception):
    """ta3 认证错误。kind: login_required | refresh_failed | network"""

    def __init__(self, message: str, kind: str = "login_required"):
        super().__init__(message)
        self.kind = kind


async def refresh_access_token(api_base: str, refresh_token: str) -> dict:
    """POST {api_base}/api/oauth/token（grant_type=refresh_token），refresh 轮转。

    返回 {accessToken, refreshToken, loginId, userId, orgId, userName}。
    失败（invalid_grant）抛 Ta3AuthError(kind='login_required')。
    """
    url = f"{api_base.rstrip('/')}{OAUTH_TOKEN_PATH}"
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": YINHAI_OAUTH_CLIENT_ID,
    }
    try:
        async with httpx.AsyncClient(timeout=_TOKEN_TIMEOUT) as client:
            resp = await client.post(url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
    except httpx.HTTPError as e:
        raise Ta3AuthError(f"OAuth token 请求失败：{url}", "network") from e

    try:
        body = resp.json() if resp.content else {}
    except ValueError:
        body = {}
    access_token = body.get("access_token") or body.get("accessToken") or body.get("token")
    if not resp.is_success or not access_token:
        code = body.get("error") or body.get("error_code") or f"http_{resp.status_code}"
        if code == "invalid_grant":
            raise Ta3AuthError("登录已过期（refresh_token 失效），请重新登录", "login_required")
        raise Ta3AuthError(f"OAuth token 刷新失败：{code}", "refresh_failed")
    return {
        "accessToken": access_token,
        "refreshToken": body.get("refresh_token") or body.get("refreshToken") or "",
        "loginId": body.get("login_id") or body.get("loginId"),
        "userId": body.get("user_id") or body.get("userId"),
        "orgId": body.get("org_id") or body.get("orgId"),
        "userName": body.get("user_name") or body.get("userName"),
    }


async def ensure_token(db: AsyncSession, provider_id: int, api_base: str) -> str:
    """返回可用 access_token；无 refresh_token 或刷新失败时抛 Ta3AuthError。"""
    row = await load_auth(db, provider_id)
    if row is None or not row.access_token:
        raise Ta3AuthError("请先登录 Ta+3 账号", "login_required")
    if not row.refresh_token:
        return row.access_token  # IM 静默登录路径无 refresh，直接返回（由调用方在 401 时引导重登）

    lock = _get_refresh_lock(provider_id)
    async with lock:
        # 双检：等待锁期间可能已被其它协程刷新
        row = await load_auth(db, provider_id)
        if row is None or not row.access_token:
            raise Ta3AuthError("请先登录 Ta+3 账号", "login_required")
        try:
            result = await refresh_access_token(api_base, row.refresh_token)
        except Ta3AuthError as e:
            if e.kind == "login_required":
                await clear_auth(db, provider_id)
                await db.commit()
            raise
        row.access_token = result["accessToken"]
        if result.get("refreshToken"):
            row.refresh_token = result["refreshToken"]
        account = dict(row.account or {})
        for key, src in (("id", "loginId"), ("label", "userName")):
            if result.get(src):
                account[key] = result[src]
        row.account = account
        row.updated_at = _now()
        await db.flush()
        await db.commit()
        logger.info("[ta3] provider=%s token 已刷新", provider_id)
        return row.access_token


async def mark_login_required(db: AsyncSession, provider_id: int) -> None:
    """业务请求返回 401 且无 refresh_token 可用时，清会话要求重登。"""
    await clear_auth(db, provider_id)
    await db.commit()
