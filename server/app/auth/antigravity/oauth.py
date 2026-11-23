"""antigravity 鉴权流程：导入本机登录态 + 独立 Google OAuth 登录。

设计对齐 server/app/auth/kiro/oauth.py:178 的 import_local_login 范式（方案 §4.4）：
- 本机登录态来自 Antigravity 客户端写在用户目录的明文 JSON（方案 §1.2）；
- 过期时用 refresh_token 续期一次，续不上则引导重新登录；
- 登录成功后调 loadCodeAssist 拿 project / tier，再落库并挂一条账号凭据。

两条路径共用 `_finish_login()`，保证落库字段与凭据处理一致。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import secrets
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.antigravity import session as antigravity_session
from app.auth.antigravity.callback import CallbackServer
from app.auth.antigravity.token import (
    AntigravityAuthError,
    build_account,
    client_metadata,
    resolve_endpoints,
)
from app.core.config import settings

logger = logging.getLogger(__name__)

_TIMEOUT = 30.0

# Google OAuth 本地回环地址允许 http（无需 HTTPS），桌面端通行做法
_LOGIN_SCOPES = (
    "https://www.googleapis.com/auth/cloud-platform "
    "https://www.googleapis.com/auth/userinfo.email "
    "https://www.googleapis.com/auth/userinfo.profile openid"
)


# ─────────────────────────────────────────────
# 本机登录态定位
# ─────────────────────────────────────────────
def gemini_home() -> Path:
    """Antigravity / Gemini CLI 共用的用户目录（~/.gemini）。"""
    return Path.home() / ".gemini"


def local_cred_candidates() -> list[Path]:
    """本机登录态候选文件，按优先级排列。

    - ~/.gemini/oauth_creds.json：Antigravity 客户端实测写入位置（方案 §1.2）；
    - ~/.gemini/antigravity*/oauth_creds.json：IDE/备份目录变体；
    - ~/.gemini/<name>/oauth_creds.json：其它客户端分支。
    """
    home = gemini_home()
    candidates = [home / "oauth_creds.json"]
    try:
        for child in sorted(home.iterdir()):
            if child.is_dir() and child.name.startswith(("antigravity", "gemini")):
                candidates.append(child / "oauth_creds.json")
    except OSError:
        pass
    return candidates


def load_local_cred() -> dict | None:
    """读取本机登录态；找不到返回 None，找到但字段不全也返回 None。"""
    for path in local_cred_candidates():
        try:
            if not path.is_file():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or not data.get("access_token"):
            continue
        data["_source"] = str(path)
        return data
    return None


def load_local_profile() -> dict:
    """读取本机账号信息（~/.gemini/google_accounts.json 的 active 字段）。"""
    try:
        path = gemini_home() / "google_accounts.json"
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {"active": str(data.get("active") or "")}
    except (OSError, ValueError):
        pass
    return {}


def _expiry_from_ms(value) -> str | None:
    """oauth_creds.json 的 expiry_date 是毫秒时间戳，转 ISO8601。

    超出可表示范围的时间戳（数据损坏/被篡改）返回 None，不向外抛异常。
    """
    try:
        ms = int(value)
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _is_expired(expiry_date) -> bool:
    """是否已过期。无法解析或超出范围一律按过期处理（宁可多刷一次）。"""
    try:
        ms = int(expiry_date)
        deadline = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return True
    return deadline <= datetime.now(timezone.utc).replace(microsecond=0)


# ─────────────────────────────────────────────
# 网关探测：loadCodeAssist
# ─────────────────────────────────────────────
async def fetch_code_assist(access_token: str, *, base_url: str | None = None,
                            client_kwargs: dict | None = None) -> dict:
    """调 loadCodeAssist 拿档位与 project。

    实测：请求体**必须**带 metadata（与 fetchAvailableModels 相反），
    platform 必须是 WINDOWS_AMD64 这类带架构的值。
    """
    from app.auth.antigravity.token import build_headers

    endpoints = resolve_endpoints(base_url)
    url = f"{endpoints.gateway}/v1internal:loadCodeAssist"
    payload = {"metadata": client_metadata()}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, **(client_kwargs or {})) as client:
            resp = await client.post(url, json=payload, headers=build_headers(access_token))
    except httpx.HTTPError as e:
        raise AntigravityAuthError(f"Antigravity 档位查询失败：{e}", "refresh_failed") from e
    if resp.status_code >= 400:
        body = (resp.text or "")[:300]
        if resp.status_code in (401, 403):
            raise AntigravityAuthError(
                f"Antigravity 登录态无效（HTTP {resp.status_code}）：{body}", "login_required",
            )
        raise AntigravityAuthError(
            f"Antigravity 档位查询失败（HTTP {resp.status_code}）：{body}", "refresh_failed",
        )
    try:
        return resp.json()
    except ValueError as e:
        raise AntigravityAuthError("Antigravity 档位查询响应不是合法 JSON", "refresh_failed") from e


def _pick_project(code_assist: dict | None) -> tuple[str, str]:
    """从 loadCodeAssist 响应推导 (project, tier)。

    project 优先取 cloudaicompanionProject；缺失时回落免费档默认值。
    """
    data = code_assist if isinstance(code_assist, dict) else {}
    tiers = data.get("allowedTiers") if isinstance(data.get("allowedTiers"), list) else []
    current = data.get("currentTier") if isinstance(data.get("currentTier"), dict) else {}
    tier = str(current.get("id") or "")
    if not tier:
        for item in tiers:
            if isinstance(item, dict) and item.get("isDefault"):
                tier = str(item.get("id") or "")
                break
    project = str(data.get("cloudaicompanionProject") or "").strip()
    if not project:
        project = settings.antigravity_default_project
    return project, tier


async def fetch_available_models(access_token: str, project: str, *,
                                 base_url: str | None = None,
                                 client_kwargs: dict | None = None) -> dict:
    """调 fetchAvailableModels（实测：只接受 project，带 metadata 会 400）。"""
    from app.auth.antigravity.token import build_headers

    endpoints = resolve_endpoints(base_url)
    url = f"{endpoints.gateway}/v1internal:fetchAvailableModels"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, **(client_kwargs or {})) as client:
            resp = await client.post(url, json={"project": project},
                                     headers=build_headers(access_token))
    except httpx.HTTPError as e:
        raise AntigravityAuthError(f"Antigravity 模型清单查询失败：{e}", "refresh_failed") from e
    if resp.status_code >= 400:
        body = (resp.text or "")[:300]
        raise AntigravityAuthError(
            f"Antigravity 模型清单查询失败（HTTP {resp.status_code}）：{body}", "refresh_failed",
        )
    try:
        payload = resp.json()
    except ValueError as e:
        raise AntigravityAuthError("Antigravity 模型清单响应不是合法 JSON", "refresh_failed") from e
    return payload if isinstance(payload, dict) else {}


# ─────────────────────────────────────────────
# 共用收尾：落库 + 凭据
# ─────────────────────────────────────────────
async def _finish_login(
    db: AsyncSession,
    provider_id: int,
    *,
    access_token: str,
    refresh_token: str | None,
    id_token: str | None,
    expires_at: str | None,
    auth_kind: str = "oauth",
    scope: str | None = None,
    credential_id: int | None = None,
) -> dict:
    """登录收尾：探测档位 → 落库 → 挂账号凭据。返回账号/档位信息。"""
    from app.persistence.models.model_reg import Provider
    from app.services.credential_service import provider_client_kwargs

    provider = await db.get(Provider, provider_id)
    client_kwargs = provider_client_kwargs(provider)
    base_url = getattr(provider, "base_url", None) if provider is not None else None

    project = settings.antigravity_default_project
    tier = ""
    if auth_kind == "oauth":
        # 档位探测失败不阻断登录：令牌本身可用就够了，档位只影响界面展示
        try:
            code_assist = await fetch_code_assist(
                access_token, base_url=base_url, client_kwargs=client_kwargs,
            )
            project, tier = _pick_project(code_assist)
        except AntigravityAuthError as e:
            if e.kind == "login_required":
                raise
            logger.warning("[antigravity] 档位查询失败，按默认档位继续：%s", e.message)

    account = build_account(id_token)
    profile = load_local_profile()
    if not account.get("email") and profile.get("active"):
        account["email"] = profile["active"]
        account["label"] = profile["active"][:80]

    await antigravity_session.save_auth(
        db, provider_id,
        access_token=access_token,
        refresh_token=refresh_token,
        id_token=id_token,
        project=project,
        tier=tier or None,
        scope=scope,
        auth_kind=auth_kind,
        account=account,
        expires_at=expires_at,
        credential_id=credential_id,
    )
    return {
        "account": account,
        "project": project,
        "tier": tier,
        "auth_kind": auth_kind,
        "expires_at": expires_at,
    }


# ─────────────────────────────────────────────
# 路径一：导入本机登录态
# ─────────────────────────────────────────────
async def import_local_login(db: AsyncSession, provider_id: int) -> dict:
    """导入本机 Antigravity / Gemini 登录态到该供应商。

    - 找不到 `~/.gemini/oauth_creds.json` → login_required（提示先登录客户端）；
    - 已过期 → 用 refresh_token 续期一次；续期失败 → login_required；
    - 续期后 scope 以远端返回为准（实测远端多出 experimentsandconfigs/cclog）。
    """
    cred = load_local_cred()
    if not cred:
        raise AntigravityAuthError(
            "未找到本机 Antigravity 登录态，请先登录 Antigravity 客户端，或改用应用内登录",
            "login_required",
        )

    from app.persistence.models.model_reg import Provider
    from app.services.credential_service import provider_client_kwargs

    provider = await db.get(Provider, provider_id)
    client_kwargs = provider_client_kwargs(provider)
    endpoints = resolve_endpoints(getattr(provider, "base_url", None) if provider else None)

    access_token = str(cred.get("access_token") or "")
    refresh_token = str(cred.get("refresh_token") or "") or None
    id_token = str(cred.get("id_token") or "") or None
    scope = str(cred.get("scope") or "") or None
    expires_at = _expiry_from_ms(cred.get("expiry_date"))

    if _is_expired(cred.get("expiry_date")):
        if not refresh_token:
            raise AntigravityAuthError(
                "本机 Antigravity 登录态已过期且没有刷新令牌，请重新登录客户端或改用应用内登录",
                "login_required",
            )
        result = await antigravity_session.refresh_access_token(
            endpoints, refresh_token, client_kwargs=client_kwargs,
        )
        access_token = result["accessToken"]
        refresh_token = result.get("refreshToken") or refresh_token
        id_token = result.get("idToken") or id_token
        scope = result.get("scope") or scope
        from datetime import timedelta

        expires_at = (
            datetime.now(timezone.utc)
            + timedelta(seconds=int(result.get("expiresIn") or 3600))
        ).isoformat()

    return await _finish_login(
        db, provider_id,
        access_token=access_token,
        refresh_token=refresh_token,
        id_token=id_token,
        expires_at=expires_at,
        auth_kind="oauth",
        scope=scope,
    )


# ─────────────────────────────────────────────
# 路径二：应用内独立 Google OAuth 登录（授权码 + PKCE）
# ─────────────────────────────────────────────
def _pkce_pair() -> tuple[str, str]:
    """生成 (code_verifier, code_challenge)，S256。"""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


class LoginFlow:
    """一次登录尝试（对齐 ta3：回调实例归属一次登录，旧尝试不能关掉新尝试）。"""

    def __init__(self):
        self.callback = CallbackServer()
        self.verifier, self.challenge = _pkce_pair()
        self._task: asyncio.Task | None = None
        self.result: dict | None = None
        self.error: str | None = None
        self.done = False

    @property
    def auth_url(self) -> str:
        endpoints = resolve_endpoints(None)
        query = urlencode({
            "client_id": settings.antigravity_client_id,
            "redirect_uri": self.callback.redirect_uri,
            "response_type": "code",
            "scope": _LOGIN_SCOPES,
            "access_type": "offline",
            "prompt": "consent",
            "code_challenge": self.challenge,
            "code_challenge_method": "S256",
            "state": self.callback.state,
        })
        return f"{endpoints.authorize}?{query}"

    async def start(self) -> str:
        await self.callback.start()
        return self.auth_url

    def cancel(self) -> None:
        self.callback.cancel()
        if self._task is not None and not self._task.done():
            self._task.cancel()

    async def stop(self) -> None:
        await self.callback.stop()


async def exchange_code(code: str, verifier: str, redirect_uri: str, *,
                        client_kwargs: dict | None = None) -> dict:
    """授权码换 token（PKCE）。"""
    endpoints = resolve_endpoints(None)
    data = {
        "client_id": settings.antigravity_client_id,
        "client_secret": settings.antigravity_client_secret,
        "code": code,
        "code_verifier": verifier,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, **(client_kwargs or {})) as client:
            resp = await client.post(
                endpoints.token, data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
    except httpx.HTTPError as e:
        raise AntigravityAuthError(f"Antigravity 授权码兑换失败：{e}", "refresh_failed") from e
    if resp.status_code >= 400:
        body = (resp.text or "")[:300]
        raise AntigravityAuthError(
            f"Antigravity 授权码兑换失败（HTTP {resp.status_code}）：{body}", "login_required",
        )
    try:
        payload = resp.json()
    except ValueError as e:
        raise AntigravityAuthError("Antigravity 授权响应不是合法 JSON", "refresh_failed") from e
    access_token = payload.get("access_token")
    if not access_token:
        raise AntigravityAuthError("Antigravity 授权响应缺少 access_token", "login_required")
    return payload


async def complete_login(db: AsyncSession, provider_id: int, flow: LoginFlow,
                         callback_result: dict) -> dict:
    """回调收到授权码后完成登录并落库。"""
    from app.persistence.models.model_reg import Provider
    from app.services.credential_service import provider_client_kwargs

    if callback_result.get("error"):
        raise AntigravityAuthError(str(callback_result["error"]), "login_required")
    code = str(callback_result.get("code") or "")
    if not code:
        raise AntigravityAuthError("未收到授权码，请重新登录", "login_required")

    provider = await db.get(Provider, provider_id)
    client_kwargs = provider_client_kwargs(provider)
    payload = await exchange_code(
        code, flow.verifier, flow.callback.redirect_uri, client_kwargs=client_kwargs,
    )
    from datetime import timedelta

    expires_at = (
        datetime.now(timezone.utc)
        + timedelta(seconds=int(payload.get("expires_in") or 3600))
    ).isoformat()
    return await _finish_login(
        db, provider_id,
        access_token=str(payload["access_token"]),
        refresh_token=payload.get("refresh_token"),
        id_token=payload.get("id_token"),
        expires_at=expires_at,
        auth_kind="oauth",
        scope=payload.get("scope"),
    )
