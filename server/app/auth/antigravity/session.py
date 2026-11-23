"""antigravity 登录态存储与会话管理（对齐 kiro/workbuddy session 范式）。

- load/save/clear：AntigravityAuth 表按「provider + credential」双维度读写。
  每条登录账号对应一条 provider_credentials，其 antigravity_auth 行的 credential_id
  指向该凭据；credential_id 为空表示 provider 级旧行（兼容未迁移数据）。
- ensure_token：返回可用 access_token，距过期不足 300s 时刷新（带 in-flight 锁双检）。
- force_refresh：业务请求 401/403 后强制刷新一次。
- refresh_token 失效（invalid_grant / invalid_client）→ 清会话并抛 login_required。

方案 docs/plan-antigravity-provider-integration.md §5.5。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.antigravity.token import (
    INVALID_CLIENT,
    INVALID_GRANT,
    AntigravityAuthError,
    AntigravityEndpoints,
    build_account,
    resolve_endpoints,
)
from app.core.config import settings
from app.persistence.models.antigravity_auth import AntigravityAuth

logger = logging.getLogger(__name__)

# 距过期不足该秒数即视为「该刷新」，避免请求途中过期
_REFRESH_MARGIN_SECONDS = 300
_TOKEN_TIMEOUT = 20.0

# refresh 续期 in-flight 锁（按 provider::credential）
_refresh_locks: dict[str, asyncio.Lock] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _expires_at_from(expires_in: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=max(0, int(expires_in)))).isoformat()


def _lock_key(provider_id: int, credential_id: int | None) -> str:
    return f"{provider_id}::{credential_id or 0}"


def _get_refresh_lock(provider_id: int, credential_id: int | None) -> asyncio.Lock:
    key = _lock_key(provider_id, credential_id)
    lock = _refresh_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _refresh_locks[key] = lock
    return lock


def _auth_filter(provider_id: int, credential_id: int | None):
    """双维度定位条件：credential_id 为空 → provider 级旧行（credential_id IS NULL）。"""
    if credential_id is None:
        return (
            AntigravityAuth.provider_id == provider_id,
            AntigravityAuth.credential_id.is_(None),
        )
    return (
        AntigravityAuth.provider_id == provider_id,
        AntigravityAuth.credential_id == credential_id,
    )


def _select_auth(provider_id: int, credential_id: int | None):
    return (
        select(AntigravityAuth)
        .where(*_auth_filter(provider_id, credential_id))
        .order_by(AntigravityAuth.id.asc())
    )


def _find_row(s, provider_id: int, credential_id: int | None) -> AntigravityAuth | None:
    """按「provider + credential」列定位登录态行（写路径与 load_auth 取同一行）。

    与 kiro_auth 的教训一致：不能用 s.get(Model, provider_id) ——那是按主键查，
    provider_id 只是普通列，取不到时刷新结果会被静默丢弃。
    """
    return s.execute(_select_auth(provider_id, credential_id)).scalars().first()


async def load_auth(db: AsyncSession, provider_id: int,
                    credential_id: int | None = None) -> AntigravityAuth | None:
    """精确读取登录态行（无回落）。

    populate_existing 保证每次读到库内最新值：登录态写入走独立写会话
    （run_write_locked），与请求读会话不是同一 identity map。

    注意：credential_id 为空时只匹配 provider 级旧行（credential_id IS NULL）。
    登录后写入的行都带 credential_id，只想「取这个供应商当前能用的账号」时
    必须用 `resolve_auth`，否则会读不到。
    """
    res = await db.execute(_select_auth(provider_id, credential_id)
                           .execution_options(populate_existing=True))
    return res.scalars().first()


async def resolve_auth(db: AsyncSession, provider_id: int,
                       credential_id: int | None = None) -> AntigravityAuth | None:
    """取「当前可用」的登录态行（读取路径专用，带回落）。

    回退顺序（多账号改造后登录态行都带 credential_id，不能再只查 provider 级行）：
    1. 指定 credential_id → 只认该行（用户明确选了某个账号）；
    2. 未指定 → 优先 provider 级旧行（迁移前数据）；
    3. 再退到该供应商下第一条有 access_token 的行（新建账号的常态）。
    """
    if credential_id is not None:
        return await load_auth(db, provider_id, credential_id)

    legacy = await load_auth(db, provider_id, None)
    if legacy is not None and legacy.access_token:
        return legacy
    return await _first_logged_in_auth(db, provider_id)


async def _first_logged_in_auth(db: AsyncSession, provider_id: int) -> AntigravityAuth | None:
    """该供应商下第一条有 access_token 的登录态行（按 id 升序）。"""
    res = await db.execute(
        select(AntigravityAuth)
        .where(AntigravityAuth.provider_id == provider_id)
        .order_by(AntigravityAuth.id.asc())
        .execution_options(populate_existing=True)
    )
    for row in res.scalars().all():
        if row.access_token:
            return row
    return None


async def save_auth(
    db: AsyncSession,
    provider_id: int,
    *,
    access_token: str,
    refresh_token: str | None = None,
    id_token: str | None = None,
    project: str | None = None,
    tier: str | None = None,
    scope: str | None = None,
    auth_kind: str = "oauth",
    account: dict | None = None,
    catalog: dict | None = None,
    expires_at: str | None = None,
    credential_id: int | None = None,
) -> None:
    """写入登录态并按账号分行落库。

    多账号语义（方案 §11.2）：
    - 同一账号（按 sub，其次 email 识别）重复登录 → 复用其凭据行与登录态行，只更新令牌；
    - 不同账号 → 各建一条凭据与登录态行，互不覆盖；
    - 旧数据里 credential_id 为空的 provider 级行由 `_adopt_legacy_row` 接管，
      避免同一账号残留两行、也避免换账号后取到旧账号的令牌。
    """
    from app.persistence.database import run_write_locked

    # 先解析/创建该账号的凭据，拿到本账号专属的 credential_id
    label = None
    if isinstance(account, dict):
        label = str(account.get("label") or account.get("email") or account.get("sub") or "")[:80] or None
    resolved_cid = await ensure_account_credential(
        db, provider_id, label or "Antigravity 账号",
        credential_id=credential_id, account=account,
    )

    def patch(s):
        row = _find_row(s, provider_id, resolved_cid)
        if row is None and resolved_cid is not None:
            row = _adopt_legacy_row(s, provider_id, resolved_cid, account)
        if row is None:
            row = AntigravityAuth(provider_id=provider_id, credential_id=resolved_cid,
                                  auth_kind=auth_kind, updated_at=_now())
            s.add(row)
        row.access_token = access_token
        if refresh_token is not None:
            row.refresh_token = refresh_token
        if id_token is not None:
            row.id_token = id_token
        if project is not None:
            row.project = project
        if tier is not None:
            row.tier = tier
        if scope is not None:
            row.scope = scope
        if account is not None:
            row.account = account
        if catalog is not None:
            row.catalog = catalog
        if expires_at is not None:
            row.expires_at = expires_at
        row.auth_kind = auth_kind
        row.updated_at = _now()
        s.commit()

    await run_write_locked(patch, label=f"antigravity.auth.save.{provider_id}")


def _account_key(account: dict | None) -> str:
    """账号唯一标识：优先 sub（Google 账号稳定 id），其次邮箱。"""
    if not isinstance(account, dict):
        return ""
    return str(account.get("sub") or "").strip() or str(account.get("email") or "").strip()


def _adopt_legacy_row(s, provider_id: int, credential_id: int, account: dict | None):
    """把旧式 provider 级登录态行（credential_id IS NULL）接管到本账号凭据。

    只在「旧行没有账号信息」或「旧行就是同一账号」时接管——否则会把别的账号
    的令牌挂到新账号上，造成串号。
    """
    row = s.execute(
        select(AntigravityAuth).where(
            AntigravityAuth.provider_id == provider_id,
            AntigravityAuth.credential_id.is_(None),
        ).order_by(AntigravityAuth.id.asc())
    ).scalars().first()
    if row is None:
        return None
    legacy_key = _account_key(row.account if isinstance(row.account, dict) else None)
    new_key = _account_key(account)
    if legacy_key and new_key and legacy_key != new_key:
        return None
    row.credential_id = credential_id
    row.updated_at = _now()
    return row


async def ensure_account_credential(db: AsyncSession, provider_id: int, label: str,
                                    credential_id: int | None = None,
                                    account: dict | None = None) -> int | None:
    """确保该账号在凭据表里有一行（幂等），返回其 id。

    匹配优先级：
    1. credential_id 给定 → 直接维护该行（调用方已确定归属）；
    2. 按账号标识（sub/email）命中已有行 → 复用（同账号重复登录不会产生新账号）；
    3. 旧式单账号行（有 kind 但没有账号标识）→ 认领并补上标识（迁移兼容）；
    4. 都没有 → 新建。
    """
    from app.persistence.models.model_reg import ProviderCredential

    key = _account_key(account)
    res = await db.execute(
        select(ProviderCredential).where(ProviderCredential.provider_id == provider_id)
    )
    existing = list(res.scalars().all())

    def _touch(c, *, adopt: bool) -> None:
        extra = dict(c.extra or {})
        extra.setdefault("kind", "antigravity_account")
        if key:
            # 存一份账号标识，供下次登录按账号匹配（不存令牌）
            extra["account_key"] = key
            if isinstance(account, dict):
                if account.get("email"):
                    extra["account_email"] = str(account["email"])[:120]
                if account.get("sub"):
                    extra["account_sub"] = str(account["sub"])[:80]
        c.extra = extra
        if label and c.label != label:
            c.label = label
        if adopt:
            if not c.is_active:
                c.is_active = True
            if (c.status or "ok") != "ok":
                c.status = "ok"
                c.cooldown_until = None
                c.last_error = None
                c.fail_count = 0

    # 1) 指定凭据
    if credential_id is not None:
        target = next((c for c in existing if c.id == credential_id), None)
        if target is not None:
            _touch(target, adopt=True)
            await db.commit()
            return target.id

    antigravity_creds = [
        c for c in existing
        if (c.extra or {}).get("kind") == "antigravity_account"
    ]

    # 2) 按账号标识命中
    if key:
        for c in antigravity_creds:
            if str((c.extra or {}).get("account_key") or "") == key:
                _touch(c, adopt=True)
                await db.commit()
                return c.id

    # 3) 认领旧式无标识单账号行（仅当本次带账号信息时才认领，避免误占）
    if key and len(antigravity_creds) == 1:
        only = antigravity_creds[0]
        if not (only.extra or {}).get("account_key"):
            _touch(only, adopt=True)
            await db.commit()
            return only.id

    # 4) 新建
    from app.services import credential_service

    extra: dict = {"kind": "antigravity_account"}
    if key:
        extra["account_key"] = key
        if isinstance(account, dict):
            if account.get("email"):
                extra["account_email"] = str(account["email"])[:120]
            if account.get("sub"):
                extra["account_sub"] = str(account["sub"])[:80]
    return await credential_service.create_credential(
        db, provider_id,
        label=label or "Antigravity 账号",
        is_active=True,
        status="ok",
        extra=extra,
    )


async def attach_credential(db: AsyncSession, provider_id: int,
                            credential_id: int) -> None:
    """把该供应商下尚未归属账号的登录态行挂到指定凭据（登录后补挂用）。"""
    from app.persistence.database import run_write_locked

    def patch(s):
        row = s.execute(
            select(AntigravityAuth).where(
                AntigravityAuth.provider_id == provider_id,
                AntigravityAuth.credential_id.is_(None),
            ).order_by(AntigravityAuth.id.asc())
        ).scalars().first()
        if row is not None:
            row.credential_id = credential_id
            row.updated_at = _now()
        s.commit()

    await run_write_locked(patch, label=f"antigravity.auth.attach.{provider_id}")


async def clear_auth(db: AsyncSession, provider_id: int,
                     credential_id: int | None = None) -> None:
    """登出语义 = 清干净：删除该供应商（或该凭据）的全部登录态行。"""
    from app.persistence.database import run_write_locked

    def patch(s):
        stmt = select(AntigravityAuth).where(AntigravityAuth.provider_id == provider_id)
        if credential_id is not None:
            stmt = stmt.where(AntigravityAuth.credential_id == credential_id)
        rows = s.execute(stmt).scalars().all()
        for row in rows:
            s.delete(row)
        s.commit()

    await run_write_locked(patch, label=f"antigravity.auth.clear.{provider_id}")


async def get_access_token(db: AsyncSession, provider_id: int,
                           credential_id: int | None = None) -> str | None:
    row = await load_auth(db, provider_id, credential_id)
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


async def refresh_access_token(
    endpoints: AntigravityEndpoints,
    refresh_token: str,
    *,
    client_kwargs: dict | None = None,
) -> dict:
    """用 refresh_token 换新 access_token。

    返回 {accessToken, refreshToken?, expiresIn, scope?, idToken?}。
    与 Google OAuth 的响应字段保持驼峰，便于上层统一处理。
    """
    if not refresh_token:
        raise AntigravityAuthError("Antigravity 登录态缺少刷新令牌，请重新导入或登录", "login_required")

    data = {
        "client_id": settings.antigravity_client_id,
        "client_secret": settings.antigravity_client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    try:
        async with httpx.AsyncClient(timeout=_TOKEN_TIMEOUT, **(client_kwargs or {})) as client:
            resp = await client.post(
                endpoints.token, data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
    except httpx.HTTPError as e:
        raise AntigravityAuthError(f"Antigravity 令牌刷新请求失败：{e}", "refresh_failed") from e

    if resp.status_code >= 400:
        body = (resp.text or "")[:300]
        if resp.status_code in (400, 401) and (
            INVALID_GRANT in body or INVALID_CLIENT in body or "unauthorized_client" in body
        ):
            # 刷新令牌或客户端凭据失效：只能重新登录
            raise AntigravityAuthError(
                "Antigravity 登录已失效，请重新导入本机登录态或重新登录", "login_required",
            )
        raise AntigravityAuthError(
            f"Antigravity 令牌刷新失败（HTTP {resp.status_code}）：{body}", "refresh_failed",
        )

    try:
        payload = resp.json()
    except ValueError as e:
        raise AntigravityAuthError("Antigravity 令牌刷新响应不是合法 JSON", "refresh_failed") from e

    access_token = payload.get("access_token")
    if not access_token:
        raise AntigravityAuthError("Antigravity 令牌刷新响应缺少 access_token", "refresh_failed")

    result = {
        "accessToken": access_token,
        "expiresIn": int(payload.get("expires_in") or 3600),
    }
    if payload.get("refresh_token"):
        result["refreshToken"] = payload["refresh_token"]
    if payload.get("scope"):
        result["scope"] = payload["scope"]
    if payload.get("id_token"):
        result["idToken"] = payload["id_token"]
    return result


async def _persist_refresh_result(provider_id: int, credential_id: int | None,
                                  result: dict) -> None:
    """把刷新结果落库（写引擎单写线程）。"""
    from app.persistence.database import run_write_locked

    def _persist(s):
        r = _find_row(s, provider_id, credential_id)
        if r is None:
            s.commit()
            return
        r.access_token = result["accessToken"]
        if result.get("refreshToken"):
            r.refresh_token = result["refreshToken"]
        if result.get("idToken"):
            r.id_token = result["idToken"]
        if result.get("scope"):
            r.scope = result["scope"]
        r.expires_at = _expires_at_from(result.get("expiresIn") or 3600)
        r.updated_at = _now()
        s.commit()

    await run_write_locked(
        _persist, label=f"antigravity.auth.refresh.{provider_id}.{credential_id or 0}",
    )


async def _do_refresh(db: AsyncSession, provider_id: int, row: AntigravityAuth,
                      provider, credential_id: int | None) -> str:
    """用 row.refresh_token 换新 token 并落库（调用方负责并发锁）。"""
    from app.persistence.models.model_reg import Provider
    from app.services.credential_service import provider_client_kwargs

    if provider is None:
        provider = await db.get(Provider, provider_id)
    endpoints = resolve_endpoints(getattr(provider, "base_url", None))
    result = await refresh_access_token(
        endpoints, row.refresh_token or "",
        client_kwargs=provider_client_kwargs(provider),
    )
    await _persist_refresh_result(provider_id, credential_id, result)
    return result["accessToken"]


async def ensure_token(db: AsyncSession, provider_id: int, provider=None,
                       credential_id: int | None = None) -> str:
    """返回可用 access_token；过期则刷新。

    - 无登录态 → login_required
    - 无 refresh_token → 直接返回现有 token（由调用方在 401 时引导重登）
    - 未到期 → 直接返回；到期 → 刷新（带锁双检）

    未指定 credential_id 时按 `resolve_auth` 回落取当前可用账号，并用它真实的
    credential_id 做刷新与加锁——否则多账号登录后（行带 credential_id）会误报
    「请先导入登录态」。
    """
    from app.persistence.models.model_reg import Provider

    row = await resolve_auth(db, provider_id, credential_id)
    if row is None or not row.access_token:
        raise AntigravityAuthError("请先导入 Antigravity 登录态或登录账号", "login_required")
    # 回落后以行上真实的凭据 id 为准，保证刷新结果写回同一行
    cid = credential_id if credential_id is not None else getattr(row, "credential_id", None)
    if not row.refresh_token:
        return row.access_token
    if not _is_expiring(row.expires_at):
        return row.access_token

    if provider is None:
        provider = await db.get(Provider, provider_id)

    lock = _get_refresh_lock(provider_id, cid)
    async with lock:
        row = await resolve_auth(db, provider_id, credential_id)
        if row is None or not row.access_token:
            raise AntigravityAuthError("请先导入 Antigravity 登录态或登录账号", "login_required")
        if not _is_expiring(row.expires_at):
            return row.access_token
        if not row.refresh_token:
            return row.access_token
        try:
            return await _do_refresh(db, provider_id, row, provider, cid)
        except AntigravityAuthError as e:
            if e.kind == "login_required":
                await clear_auth(db, provider_id, cid)
            raise


async def force_refresh(db: AsyncSession, provider_id: int, provider=None,
                        credential_id: int | None = None) -> str:
    """忽略过期判定，强制刷新（业务请求 401/403 后重试用）。

    与 `ensure_token` 同口径：未指定凭据时回落取当前可用账号。
    """
    row = await resolve_auth(db, provider_id, credential_id)
    if row is None or not row.access_token:
        raise AntigravityAuthError("请先导入 Antigravity 登录态或登录账号", "login_required")
    cid = credential_id if credential_id is not None else getattr(row, "credential_id", None)
    if not row.refresh_token:
        raise AntigravityAuthError("Antigravity 登录态缺少刷新令牌，请重新登录", "login_required")

    lock = _get_refresh_lock(provider_id, cid)
    async with lock:
        row = await resolve_auth(db, provider_id, credential_id)
        if row is None or not row.refresh_token:
            raise AntigravityAuthError("Antigravity 登录态缺少刷新令牌，请重新登录", "login_required")
        # 用回落得到的真实凭据 id 写回，否则刷新结果会落到 provider 级行（不存在）而被丢弃
        cid = credential_id if credential_id is not None else getattr(row, "credential_id", None)
        try:
            return await _do_refresh(db, provider_id, row, provider, cid)
        except AntigravityAuthError as e:
            if e.kind == "login_required":
                await clear_auth(db, provider_id, cid)
            raise


async def mark_login_required(db: AsyncSession, provider_id: int,
                              credential_id: int | None = None) -> None:
    """业务请求返回 401 且无 refresh_token 可用时，清会话要求重登。"""
    await clear_auth(db, provider_id, credential_id)


def account_label(row: AntigravityAuth | None) -> str:
    """登录态行的展示标签（账号邮箱优先）。"""
    if row is None:
        return ""
    account = row.account if isinstance(row.account, dict) else {}
    return str(account.get("label") or account.get("email") or "")


__all__ = [
    "AntigravityAuthError",
    "account_label",
    "attach_credential",
    "build_account",
    "clear_auth",
    "ensure_account_credential",
    "ensure_token",
    "force_refresh",
    "get_access_token",
    "load_auth",
    "mark_login_required",
    "refresh_access_token",
    "save_auth",
]
