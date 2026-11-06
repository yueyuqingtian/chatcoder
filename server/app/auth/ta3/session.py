"""plan-20261106-ta3-080：OAuth 会话按供应商隔离，代次保护避免退出后的迟到回写。"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.ta3 import endpoints
from app.persistence.database import run_write_locked
from app.persistence.models.ta3_auth import Ta3Auth
from app.persistence.models.model_reg import Provider, ProviderCredential, Model

logger = logging.getLogger(__name__)
OAUTH_TOKEN_PATH = "/api/oauth/token"
YINHAI_OAUTH_CLIENT_ID = endpoints.OAUTH_CLIENT_ID
_refresh_locks: dict[int, asyncio.Lock] = {}


class Ta3AuthError(Exception):
    def __init__(self, message: str, kind: str = "login_required"):
        super().__init__(message)
        self.kind = kind


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def expires_at(seconds) -> str | None:
    try:
        seconds = float(seconds or 0)
        return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat() if seconds > 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def is_expiring(value: str | None) -> bool:
    if not value:
        return False
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (when - datetime.now(timezone.utc)).total_seconds() <= 300
    except (ValueError, TypeError):
        return False


def _row(s, provider_id: int):
    return s.execute(select(Ta3Auth).where(Ta3Auth.provider_id == provider_id).order_by(Ta3Auth.id.desc())).scalars().first()


async def load_auth(db: AsyncSession, provider_id: int) -> Ta3Auth | None:
    result = await db.execute(select(Ta3Auth).where(Ta3Auth.provider_id == provider_id)
                              .order_by(Ta3Auth.id.desc()).execution_options(populate_existing=True))
    return result.scalars().first()


async def get_auth_row(db: AsyncSession, provider_id: int) -> Ta3Auth:
    def patch(s):
        if _row(s, provider_id) is None:
            s.add(Ta3Auth(provider_id=provider_id, auth_version=endpoints.AUTH_VERSION,
                          oauth_client_id=YINHAI_OAUTH_CLIENT_ID, session_generation=0, updated_at=_now()))
        s.commit()
    await run_write_locked(patch, label="ta3.auth.row")
    return await load_auth(db, provider_id)


async def generation(db: AsyncSession, provider_id: int) -> int:
    row = await get_auth_row(db, provider_id)
    return row.session_generation or 0


async def advance_generation(db: AsyncSession, provider_id: int) -> int:
    def patch(s):
        row = _row(s, provider_id)
        if row is None:
            row = Ta3Auth(provider_id=provider_id, auth_version=endpoints.AUTH_VERSION, session_generation=0)
            s.add(row)
        row.session_generation = (row.session_generation or 0) + 1
        s.commit()
        return row.session_generation
    return await run_write_locked(patch, label="ta3.auth.generation")


def _account_credential(s, provider_id, account):
    creds = s.execute(select(ProviderCredential).where(ProviderCredential.provider_id == provider_id)).scalars().all()
    credential = next((c for c in creds if (c.extra or {}).get("kind") == "ta3_account"), None)
    if credential is None:
        credential = ProviderCredential(provider_id=provider_id, extra={"kind": "ta3_account"}, priority=0)
        s.add(credential)
    account = account or {}
    credential.label = str(account.get("label") or account.get("id") or "TA3 账号")[:80]
    credential.is_active = True
    credential.status = "ok"
    credential.cooldown_until = credential.last_error = None
    credential.fail_count = 0
    s.flush()
    return credential.id


async def ensure_account_credential(db, provider_id, account):
    def patch(s):
        cid = _account_credential(s, provider_id, account)
        s.commit()
        return cid
    return await run_write_locked(patch, label="ta3.auth.credential")


async def save_auth(db: AsyncSession, provider_id: int, *, access_token: str,
                    refresh_token: str | None = None, account: dict | None = None,
                    catalog: dict | None = None, expires_in=None,
                    expected_generation: int | None = None) -> bool:
    def patch(s):
        provider = s.get(Provider, provider_id)
        if provider is None:
            return False
        row = _row(s, provider_id)
        if expected_generation is not None and (row is None or row.session_generation != expected_generation):
            return False
        if row is None:
            row = Ta3Auth(provider_id=provider_id, session_generation=0)
            s.add(row)
        row.access_token = access_token
        row.refresh_token = refresh_token or None
        row.account = account or {}
        row.catalog = catalog
        row.auth_version = endpoints.AUTH_VERSION
        row.oauth_client_id = YINHAI_OAUTH_CLIENT_ID
        row.issued_at = row.updated_at = _now()
        row.access_expires_at = expires_at(expires_in)
        row.credential_id = _account_credential(s, provider_id, account)
        provider.auth_status = "logged_in"
        provider.account_label = str((account or {}).get("label") or (account or {}).get("id") or "已登录")[:120]
        s.commit()
        return True
    return await run_write_locked(patch, label="ta3.auth.save")


async def clear_auth(db: AsyncSession, provider_id: int, *, expected_generation=None) -> bool:
    def patch(s):
        row = _row(s, provider_id)
        if expected_generation is not None and (row is None or row.session_generation != expected_generation):
            return False
        if row is not None:
            row.session_generation = (row.session_generation or 0) + 1
            row.access_token = row.refresh_token = None
            row.catalog = None
            row.access_expires_at = row.issued_at = None
        provider = s.get(Provider, provider_id)
        if provider is not None:
            provider.auth_status = "login_required"
            provider.account_label = None
        s.execute(update(Model).where(Model.provider_id == provider_id).values(api_key=None))
        for cred in s.execute(select(ProviderCredential).where(ProviderCredential.provider_id == provider_id)).scalars():
            if (cred.extra or {}).get("kind") == "ta3_account":
                cred.status = "error"
                cred.last_error = "请重新网页登录"
                cred.cooldown_until = None
        s.commit()
        return True
    changed = await run_write_locked(patch, label="ta3.auth.clear")
    if changed:
        from app.auth.ta3.quota import clear_cache
        clear_cache(provider_id)
    return changed


async def get_access_token(db: AsyncSession, provider_id: int) -> str | None:
    row = await load_auth(db, provider_id)
    return row.access_token if row and row.auth_version == endpoints.AUTH_VERSION else None


async def request_oauth_token(api_base: str, data: dict, client_kwargs=None) -> dict:
    try:
        async with httpx.AsyncClient(timeout=10, **{**(client_kwargs or {}), "follow_redirects": False}) as client:
            response = await client.post(api_base.rstrip("/") + OAUTH_TOKEN_PATH, data=data,
                                          headers={"Content-Type": "application/x-www-form-urlencoded"})
    except httpx.HTTPError as exc:
        raise Ta3AuthError("OAuth 服务暂不可达，请检查网络后重试", "network") from exc
    try:
        body = response.json()
        if not isinstance(body, dict):
            body = {}
    except ValueError:
        body = {}
    token = body.get("access_token") or body.get("accessToken") or body.get("token")
    if not response.is_success or not token:
        code = body.get("error") or body.get("errorCode") or body.get("error_code") or f"http_{response.status_code}"
        raise Ta3AuthError(f"OAuth 凭据换取失败：{code}", "login_required" if code == "invalid_grant" else "refresh_failed")
    return {
        "accessToken": token, "refreshToken": body.get("refresh_token") or body.get("refreshToken") or "",
        "expiresIn": body.get("expires_in"),
        **{k: body.get(snake) or body.get(k) for k, snake in
           (("loginId", "login_id"), ("userId", "user_id"), ("orgId", "org_id"), ("userName", "user_name"))},
    }


async def refresh_access_token(api_base: str, refresh_token: str, client_kwargs=None) -> dict:
    return await request_oauth_token(api_base, {"grant_type": "refresh_token", "refresh_token": refresh_token,
                                              "client_id": YINHAI_OAUTH_CLIENT_ID}, client_kwargs)


async def ensure_token(db: AsyncSession, provider_id: int, api_base: str, *,
                       failed_token: str | None = None) -> str:
    row = await load_auth(db, provider_id)
    if not row or not row.access_token or row.auth_version != endpoints.AUTH_VERSION:
        raise Ta3AuthError("请先网页登录 Ta+3 账号")
    if failed_token is None and not is_expiring(row.access_expires_at):
        return row.access_token
    async with _refresh_locks.setdefault(provider_id, asyncio.Lock()):
        row = await load_auth(db, provider_id)
        if not row or not row.access_token:
            raise Ta3AuthError("请先网页登录 Ta+3 账号")
        if failed_token is not None and row.access_token != failed_token:
            return row.access_token
        if failed_token is None and not is_expiring(row.access_expires_at):
            return row.access_token
        gen, old_refresh = row.session_generation, row.refresh_token
        if not old_refresh:
            await clear_auth(db, provider_id, expected_generation=gen)
            raise Ta3AuthError("登录无法续期，请重新网页登录")
        provider = await db.get(Provider, provider_id, populate_existing=True)
        if provider is None:
            raise Ta3AuthError("供应商已删除")
        base = await endpoints.resolve(provider)
        try:
            result = await refresh_access_token(base, old_refresh, endpoints.client_options(provider))
        except Ta3AuthError as exc:
            if exc.kind == "login_required":
                await clear_auth(db, provider_id, expected_generation=gen)
            raise
        def patch(s):
            current = _row(s, provider_id)
            if not current or current.session_generation != gen or current.refresh_token != old_refresh:
                return False
            current.access_token = result["accessToken"]
            current.refresh_token = result.get("refreshToken") or old_refresh
            account = dict(current.account or {})
            for key in ("loginId", "userId", "orgId"):
                if result.get(key):
                    account[key] = result[key]
            if result.get("loginId"):
                account["id"] = result["loginId"]
            if result.get("userName"):
                account["label"] = result["userName"]
            current.account = account
            current.issued_at = current.updated_at = _now()
            current.access_expires_at = expires_at(result.get("expiresIn"))
            p = s.get(Provider, provider_id)
            if p:
                p.auth_status = "logged_in"
            s.commit()
            return True
        if not await run_write_locked(patch, label="ta3.auth.refresh"):
            raise Ta3AuthError("登录状态已变化，请重试")
        return result["accessToken"]


async def mark_login_required(db: AsyncSession, provider_id: int) -> None:
    await clear_auth(db, provider_id)


async def validate_sessions() -> None:
    """plan-20261106-ta3-080：启动校验不阻塞启动，离线保留用户凭据。"""
    from app.persistence.database import async_session_factory
    from app.auth.ta3.catalog import fetch_catalog_probe, Ta3Unauthorized
    async with async_session_factory() as db:
        providers = (await db.execute(select(Provider).where(Provider.api_format == "ta3"))).scalars().all()
        for provider in providers:
            row = await load_auth(db, provider.id)
            if not row or not row.access_token:
                continue
            try:
                base = await endpoints.resolve(provider)
                token = await ensure_token(db, provider.id, base)
                try:
                    await fetch_catalog_probe(provider, token)
                except Ta3Unauthorized:
                    await ensure_token(db, provider.id, base, failed_token=token)
            except Exception:
                logger.info("[ta3] provider=%s 启动校验未完成，保留可恢复状态", provider.id)


async def revoke(refresh_token: str | None, api_base: str, client_kwargs=None) -> None:
    if not refresh_token:
        return
    try:
        async with httpx.AsyncClient(timeout=5, **{**(client_kwargs or {}), "follow_redirects": False}) as client:
            await client.post(api_base.rstrip("/") + "/api/oauth/revoke", data={
                "token": refresh_token, "token_type_hint": "refresh_token", "client_id": YINHAI_OAUTH_CLIENT_ID})
    except httpx.HTTPError:
        logger.info("[ta3] 远端吊销未完成，本地已退出")
