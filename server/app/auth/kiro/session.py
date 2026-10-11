"""kiro 登录态存储与会话管理（对齐 ta3 session 范式）。

- load/save/clear：KiroAuth 表单行读写
- ensure_token：业务请求 401 时自动 refresh（带 in-flight 锁防并发 stampede）
- refresh_token 失效（invalid_grant）→ 清会话并抛错，调用方提示重新登录
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.kiro.token import (
    DEFAULT_REGION,
    KiroAuthError,
    KiroEndpoints,
    normalize_region,
    refresh_access_token,
    resolve_endpoints,
)
from app.persistence.models.kiro_auth import KiroAuth

logger = logging.getLogger(__name__)

# 距过期不足该秒数即视为「该刷新」，避免请求途中过期
_REFRESH_MARGIN_SECONDS = 300

# refresh 续期 in-flight 锁（按 provider_id）
_refresh_locks: dict[int, asyncio.Lock] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _expires_at_from(expires_in: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=max(0, int(expires_in)))).isoformat()


def _get_refresh_lock(provider_id: int) -> asyncio.Lock:
    lock = _refresh_locks.get(provider_id)
    if lock is None:
        lock = asyncio.Lock()
        _refresh_locks[provider_id] = lock
    return lock


def endpoints_for(provider, row: KiroAuth | None) -> KiroEndpoints:
    """按「供应商 base_url 覆盖 → 认证行 region → 默认」推导端点。"""
    base_url = (getattr(provider, "base_url", None) or "").strip()
    region = normalize_region(row.region if row is not None else None)
    return resolve_endpoints(region, base_url_override=base_url)


def base_url_host(provider) -> str:
    """提取供应商 base_url 的 host（用于日志/诊断）。"""
    base = (getattr(provider, "base_url", None) or "").strip()
    return urlsplit(base).netloc if base else ""


async def load_auth(db: AsyncSession, provider_id: int) -> KiroAuth | None:
    """读取登录态行（始终以数据库最新值为准）。

    登录态写入全部经写引擎独立 session（run_write_locked），与请求读会话不是同一
    identity map——不强制刷新时，同请求内「刷新写新 token → 重试」会命中缓存旧对象。
    populate_existing 保证每次读取都覆盖为库内最新值。
    """
    res = await db.execute(
        select(KiroAuth)
        .where(KiroAuth.provider_id == provider_id)
        .order_by(KiroAuth.id.asc())
        .execution_options(populate_existing=True)
    )
    return res.scalars().first()


def _find_row(s, provider_id: int) -> "KiroAuth | None":
    """按 provider_id 列定位登录态行（写路径与 load_auth 取同一行）。

    plan-116-458: 此前写路径用 s.get(KiroAuth, provider_id) —— 那是按**主键 id**
    查询，而本表主键是自增 id、provider_id 只是普通列；两者不等时写路径取不到行，
    刷新结果被静默丢弃（实测：刷新成功但 expires_at 不更新，请求持续 403）。
    """
    return s.execute(
        select(KiroAuth)
        .where(KiroAuth.provider_id == provider_id)
        .order_by(KiroAuth.id.asc())
    ).scalars().first()


async def get_auth_row(db: AsyncSession, provider_id: int) -> KiroAuth:
    """取或建（惰性创建）登录态行（写引擎单写线程）。"""
    from app.persistence.database import run_write_locked

    def patch(s):
        row = _find_row(s, provider_id)
        if row is None:
            row = KiroAuth(provider_id=provider_id, region=DEFAULT_REGION, updated_at=_now())
            s.add(row)
            s.flush()
            s.commit()
        return True

    await run_write_locked(patch, label=f"kiro.auth.row.{provider_id}")
    return await load_auth(db, provider_id)


async def save_auth(
    db: AsyncSession,
    provider_id: int,
    *,
    access_token: str,
    refresh_token: str | None = None,
    profile_arn: str | None = None,
    region: str | None = None,
    auth_method: str | None = None,
    account: dict | None = None,
    catalog: dict | None = None,
    expires_at: str | None = None,
) -> None:
    from app.persistence.database import run_write_locked

    def patch(s):
        row = _find_row(s, provider_id)
        if row is None:
            row = KiroAuth(provider_id=provider_id, region=DEFAULT_REGION, updated_at=_now())
            s.add(row)
        row.access_token = access_token
        if refresh_token is not None:
            row.refresh_token = refresh_token
        if profile_arn is not None:
            row.profile_arn = profile_arn
        if region is not None:
            row.region = normalize_region(region)
        if auth_method is not None:
            row.auth_method = auth_method
        if account is not None:
            row.account = account
        if catalog is not None:
            row.catalog = catalog
        if expires_at is not None:
            row.expires_at = expires_at
        row.updated_at = _now()
        s.commit()

    await run_write_locked(patch, label=f"kiro.auth.save.{provider_id}")
    # 登录成功同时落一条「账号凭据」，否则界面凭据列表永远为空、会话里无法取用模型
    await ensure_account_credential(db, provider_id, account)


async def ensure_account_credential(db: AsyncSession, provider_id: int,
                                    account: dict | None) -> int | None:
    """确保该 Kiro 账号在凭据表里有一行（幂等）。"""
    from app.persistence.models.model_reg import ProviderCredential

    label = None
    if isinstance(account, dict):
        label = str(account.get("label") or account.get("email") or account.get("id") or "")[:80] or None
    label = label or "Kiro 账号"

    res = await db.execute(
        select(ProviderCredential).where(ProviderCredential.provider_id == provider_id)
    )
    existing = list(res.scalars().all())
    for c in existing:
        extra = c.extra if isinstance(c.extra, dict) else {}
        if extra.get("kind") == "kiro_account":
            if not c.is_active:
                c.is_active = True
            if (c.status or "ok") != "ok":
                c.status = "ok"
                c.cooldown_until = None
                c.last_error = None
                c.fail_count = 0
            if label and c.label != label:
                c.label = label
            await db.commit()
            return c.id

    from app.services import credential_service

    return await credential_service.create_credential(
        db, provider_id,
        label=label,
        is_active=True,
        status="ok",
        extra={"kind": "kiro_account"},
    )


async def clear_auth(db: AsyncSession, provider_id: int) -> None:
    from app.persistence.database import run_write_locked

    def patch(s):
        # 登出语义 = 清干净：逐行删除该 provider 的全部登录态行
        # （历史重复行一并清掉，避免残留旧令牌被后续读取命中）
        rows = s.execute(
            select(KiroAuth).where(KiroAuth.provider_id == provider_id)
        ).scalars().all()
        for row in rows:
            s.delete(row)
        s.commit()

    await run_write_locked(patch, label=f"kiro.auth.clear.{provider_id}")


async def get_access_token(db: AsyncSession, provider_id: int) -> str | None:
    row = await load_auth(db, provider_id)
    return row.access_token if row else None


def _is_expiring(expires_at: str | None) -> bool:
    """expires_at 为空或解析失败视为「需要刷新」（宁可多刷一次也不放过期请求）。"""
    if not expires_at:
        return True
    try:
        dt = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    except ValueError:
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt <= datetime.now(timezone.utc) + timedelta(seconds=_REFRESH_MARGIN_SECONDS)


async def _persist_refresh_result(provider_id: int, result: dict) -> None:
    """把刷新结果落库（写引擎单写线程）。"""
    from app.persistence.database import run_write_locked

    def _persist(s):
        r = _find_row(s, provider_id)
        if r is None:
            s.commit()
            return
        r.access_token = result["accessToken"]
        if result.get("refreshToken"):
            r.refresh_token = result["refreshToken"]
        if result.get("profileArn"):
            r.profile_arn = result["profileArn"]
        r.expires_at = _expires_at_from(result.get("expiresIn") or 3600)
        r.updated_at = _now()
        s.commit()

    await run_write_locked(_persist, label=f"kiro.auth.refresh.{provider_id}")


async def _do_refresh(db: AsyncSession, provider_id: int, row: KiroAuth, provider) -> str:
    """用 row.refresh_token 换新 token 并落库（调用方负责并发锁）。"""
    from app.persistence.models.model_reg import Provider
    from app.services.credential_service import provider_client_kwargs

    if provider is None:
        provider = await db.get(Provider, provider_id)
    endpoints = endpoints_for(provider, row)
    result = await refresh_access_token(
        endpoints,
        row.refresh_token or "",
        auth_method=row.auth_method or "",
        client_id=str((row.account or {}).get("clientId") or ""),
        client_secret=str((row.account or {}).get("clientSecret") or ""),
        client_kwargs=provider_client_kwargs(provider),
    )
    await _persist_refresh_result(provider_id, result)
    return result["accessToken"]


async def force_refresh(db: AsyncSession, provider_id: int, provider=None) -> str:
    """忽略过期判定，强制刷新（业务请求 401 后重试用）。"""
    row = await load_auth(db, provider_id)
    if row is None or not row.access_token:
        raise KiroAuthError("请先登录 Kiro 账号", "login_required")
    if not row.refresh_token:
        raise KiroAuthError("Kiro 登录态缺少刷新令牌，请重新登录", "login_required")

    lock = _get_refresh_lock(provider_id)
    async with lock:
        row = await load_auth(db, provider_id)
        if row is None or not row.refresh_token:
            raise KiroAuthError("Kiro 登录态缺少刷新令牌，请重新登录", "login_required")
        try:
            return await _do_refresh(db, provider_id, row, provider)
        except KiroAuthError as e:
            if e.kind == "login_required":
                await clear_auth(db, provider_id)
            raise


async def ensure_token(db: AsyncSession, provider_id: int, provider=None) -> str:
    """返回可用 access_token；过期则刷新。

    - 无登录态 → KiroAuthError(login_required)
    - 无 refresh_token → 直接返回现有 token（由调用方在 401 时引导重登）
    - 未到期 → 直接返回；到期 → 刷新（带锁双检）
    """
    from app.persistence.models.model_reg import Provider

    row = await load_auth(db, provider_id)
    if row is None or not row.access_token:
        raise KiroAuthError("请先登录 Kiro 账号", "login_required")
    if not row.refresh_token:
        return row.access_token
    if not _is_expiring(row.expires_at):
        return row.access_token

    if provider is None:
        provider = await db.get(Provider, provider_id)

    lock = _get_refresh_lock(provider_id)
    async with lock:
        # 双检：等待锁期间可能已被其它协程刷新
        row = await load_auth(db, provider_id)
        if row is None or not row.access_token:
            raise KiroAuthError("请先登录 Kiro 账号", "login_required")
        if not _is_expiring(row.expires_at):
            return row.access_token
        if not row.refresh_token:
            return row.access_token
        try:
            return await _do_refresh(db, provider_id, row, provider)
        except KiroAuthError as e:
            if e.kind == "login_required":
                await clear_auth(db, provider_id)
            raise


async def mark_login_required(db: AsyncSession, provider_id: int) -> None:
    """业务请求返回 401 且无 refresh_token 可用时，清会话要求重登。"""
    await clear_auth(db, provider_id)
