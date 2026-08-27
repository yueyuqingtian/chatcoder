"""TRAE 登录态存储与会话管理。

- load/save/clear：TraeAuth 表单行读写（含设备签名材料）
- refresh_session：刷新 Token（ExchangeToken RefreshToken 模式 + DeviceProof 签名，
  带 in-flight 锁防并发 stampede；对齐 workbuddy/ta3 session 骨架）
- ensure_token：返回当前 access_token（401 兜底刷新，5 分钟预刷新）
- refresh_token 失效（错误码命中 INVALID_TOKEN_CODES）→ 清会话并抛错
方案: docs/plan-trae-solo-provider-integration.md §5.1。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.trae import device as trae_device
from app.auth.trae.oauth import (
    EXCHANGE_TOKEN_PATH,
    INVALID_TOKEN_CODES,
    TraeAuthError,
    _auth_headers,
)
from app.persistence.models.trae_auth import TraeAuth

logger = logging.getLogger(__name__)

_TOKEN_TIMEOUT = 60.0
# 距 token 过期小于此阈值时预刷新
_TOKEN_PRE_REFRESH_SECONDS = 5 * 60


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _expires_within(row: TraeAuth, seconds: int) -> bool:
    if not row.token_expires_at:
        return False
    try:
        exp = datetime.fromisoformat(str(row.token_expires_at))
    except ValueError:
        return False
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return (exp - datetime.now(timezone.utc)).total_seconds() < seconds


async def load_auth(db: AsyncSession, provider_id: int) -> TraeAuth | None:
    res = await db.execute(select(TraeAuth).where(TraeAuth.provider_id == provider_id))
    return res.scalars().first()


async def get_auth_row(db: AsyncSession, provider_id: int) -> TraeAuth:
    """取或建（惰性创建）登录态行。"""
    row = await load_auth(db, provider_id)
    if row is None:
        row = TraeAuth(provider_id=provider_id, updated_at=_now())
        db.add(row)
        await db.flush()
    return row


async def save_auth(db: AsyncSession, provider_id: int, *, access_token: str,
                    refresh_token: str | None = None, account: dict | None = None,
                    token_expires_at: str | None = None,
                    refresh_expires_at: str | None = None,
                    device_private_key: str | None = None,
                    device_public_key: str | None = None,
                    device_id: str | None = None, machine_id: str | None = None,
                    catalog: dict | None = None) -> TraeAuth:
    row = await get_auth_row(db, provider_id)
    row.access_token = access_token
    if refresh_token is not None:
        row.refresh_token = refresh_token
    if account is not None:
        row.account = account
    if token_expires_at is not None:
        row.token_expires_at = token_expires_at
    if refresh_expires_at is not None:
        row.refresh_expires_at = refresh_expires_at
    if device_private_key is not None:
        row.device_private_key = device_private_key
    if device_public_key is not None:
        row.device_public_key = device_public_key
    if device_id is not None:
        row.device_id = device_id
    if machine_id is not None:
        row.machine_id = machine_id
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


def _get_refresh_lock(provider_id: int) -> asyncio.Lock:
    lock = _refresh_locks.get(provider_id)
    if lock is None:
        lock = asyncio.Lock()
        _refresh_locks[provider_id] = lock
    return lock


async def refresh_access_token(api_host: str, row: TraeAuth, *, client_id: str,
                               ide_version: str) -> dict:
    """POST {api_host}/trae/api/v3/oauth/ExchangeToken（RefreshToken 模式）。

    DeviceProof = ECDSA-P256-SHA256 签名（见 device.sign_device_proof）。
    返回 {accessToken, refreshToken, tokenExpiresAt, refreshExpiresAt}。
    """
    if not row.device_private_key:
        raise TraeAuthError("TRAE 缺少设备私钥（登录信息不完整），请重新登录", "login_required")
    proof = trae_device.sign_device_proof(
        "POST", EXCHANGE_TOKEN_PATH, client_id,
        row.refresh_token or "", row.device_private_key)
    info = trae_device.device_info(
        row.device_public_key or "", device_id=row.device_id or "",
        machine_id=row.machine_id or "", ide_version=ide_version)
    body = {
        "ClientID": client_id,
        "ClientSecret": "",
        "RefreshToken": row.refresh_token or "",
        "DeviceInfo": info,
        "DeviceProof": proof,
        "IDEVersion": ide_version,
    }
    url = f"{api_host.rstrip('/')}{EXCHANGE_TOKEN_PATH}"
    try:
        async with httpx.AsyncClient(timeout=_TOKEN_TIMEOUT) as client:
            resp = await client.post(url, json=body, headers=_auth_headers(row.access_token or ""))
    except httpx.HTTPError as e:
        raise TraeAuthError(f"TRAE token 刷新请求失败：{url}", "network") from e

    try:
        body_out = resp.json() if resp.content else {}
    except ValueError:
        body_out = {}
    meta_raw = body_out.get("ResponseMetadata") if isinstance(body_out, dict) else {}
    meta = meta_raw if isinstance(meta_raw, dict) else {}
    error = meta.get("Error") if isinstance(meta.get("Error"), dict) else {}
    code = str(error.get("Code") or "")
    result = body_out.get("Result") if isinstance(body_out, dict) else None
    token = (result or {}).get("Token") if isinstance(result, dict) else None
    if not resp.is_success or not token:
        if code in INVALID_TOKEN_CODES:
            raise TraeAuthError(f"TRAE 登录已过期（{code}），请重新登录",
                                "login_required", code=code)
        raise TraeAuthError(f"TRAE token 刷新失败：{code or f'http_{resp.status_code}'}",
                            "refresh_failed", code=code)
    token_expire_at = result.get("TokenExpireAt")
    if token_expire_at is None:
        token_expires_at = trae_device.parse_expire(
            result.get("TokenExpireDuration"), relative=True)
    else:
        token_expires_at = trae_device.parse_expire(token_expire_at)
    return {
        "accessToken": str(token),
        "refreshToken": str(result.get("RefreshToken") or ""),
        "tokenExpiresAt": token_expires_at,
        "refreshExpiresAt": trae_device.parse_expire(result.get("RefreshExpireAt")),
    }


async def ensure_token(db: AsyncSession, provider_id: int, *, api_host: str,
                       client_id: str, ide_version: str) -> str:
    """返回可用 access_token；未登录抛 TraeAuthError。

    距过期 < 5 分钟预刷新；无过期时间则不预判（依赖 401 兜底刷新）。
    """
    row = await load_auth(db, provider_id)
    if row is None or not row.access_token:
        raise TraeAuthError("请先登录 TRAE 账号", "login_required")
    if row.refresh_token and _expires_within(row, _TOKEN_PRE_REFRESH_SECONDS):
        return await refresh_session(db, provider_id, api_host=api_host,
                                     client_id=client_id, ide_version=ide_version)
    return row.access_token


async def refresh_session(db: AsyncSession, provider_id: int, *, api_host: str,
                          client_id: str, ide_version: str) -> str:
    """刷新会话，返回新 access_token。refresh_token 轮转防并发由 in-flight 锁保证。"""
    row = await load_auth(db, provider_id)
    if row is None or not row.access_token or not row.refresh_token:
        raise TraeAuthError("请先登录 TRAE 账号", "login_required")

    lock = _get_refresh_lock(provider_id)
    async with lock:
        # 双检：等待锁期间可能已被其它协程刷新
        row = await load_auth(db, provider_id)
        if row is None or not row.access_token or not row.refresh_token:
            raise TraeAuthError("请先登录 TRAE 账号", "login_required")
        try:
            result = await refresh_access_token(
                api_host, row, client_id=client_id, ide_version=ide_version)
        except TraeAuthError as e:
            if e.kind == "login_required":
                await clear_auth(db, provider_id)
                await db.commit()
            raise
        row.access_token = result["accessToken"]
        if result.get("refreshToken"):
            row.refresh_token = result["refreshToken"]
        if result.get("tokenExpiresAt"):
            row.token_expires_at = result["tokenExpiresAt"]
        if result.get("refreshExpiresAt"):
            row.refresh_expires_at = result["refreshExpiresAt"]
        row.updated_at = _now()
        await db.flush()
        await db.commit()
        logger.info("[trae] provider=%s token 已刷新", provider_id)
        return row.access_token


async def mark_login_required(db: AsyncSession, provider_id: int) -> None:
    """业务请求 401 且无 refresh_token 可用时，清会话要求重登。"""
    await clear_auth(db, provider_id)
    await db.commit()
