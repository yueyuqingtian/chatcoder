"""kiro 本机登录态导入。

Kiro IDE / CLI 登录后把凭据落在本机两处，按优先级依次尝试：

1. ``~/.aws/sso/cache/kiro-auth-token.json``（Kiro IDE 与本项目参考实现的共同位置）
   形如 ``{accessToken, refreshToken, profileArn, expiresAt, authMethod, provider}``；
2. ``%LOCALAPPDATA%/kiro-cli/data.sqlite3`` 的 token 键值行（CLI 侧，键名前缀
   ``kirocli:`` / ``codewhisperer:``），值同为上述 JSON。

导入即落库；若已过期则用 refresh_token 续期一次（失败提示重新登录）。
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.kiro import session as kiro_session
from app.auth.kiro.token import (
    DEFAULT_REGION,
    KiroAuthError,
    normalize_region,
)

logger = logging.getLogger(__name__)

_TOKEN_FILE_NAME = "kiro-auth-token.json"

# CLI 侧 sqlite 中的令牌键（对齐 KiroCrew kiro_usage_api._SQLITE_TOKEN_KEYS）
_SQLITE_TOKEN_KEYS = (
    "kirocli:odic:token",
    "codewhisperer:odic:token",
    "kirocli:social:token",
    "kirocli:external-idp:token",
)


def _home() -> Path:
    return Path.home()


def _local_token_paths() -> list[Path]:
    """候选令牌文件路径（存在即用，按优先级排序）。"""
    home = _home()
    return [
        home / ".aws" / "sso" / "cache" / _TOKEN_FILE_NAME,
        home / ".kiro" / _TOKEN_FILE_NAME,
    ]


def _cli_sqlite_path() -> Path | None:
    """CLI 数据目录下的 data.sqlite3（Windows 走 LOCALAPPDATA，其余走 XDG 数据目录）。"""
    import os

    candidates: list[Path] = []
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        candidates.append(Path(local_appdata) / "kiro-cli" / "data.sqlite3")
    candidates.append(_home() / ".local" / "share" / "kiro-cli" / "data.sqlite3")
    candidates.append(_home() / ".kiro-cli" / "data.sqlite3")
    for path in candidates:
        if path.is_file():
            return path
    return None


def _parse_token_payload(raw: object) -> dict | None:
    """把 JSON 文本/字典规范化为令牌字典；缺 accessToken 视为无效。"""
    data = raw
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            return None
    if not isinstance(data, dict):
        return None
    access_token = str(data.get("accessToken") or data.get("access_token") or "").strip()
    if not access_token:
        return None
    return {
        "accessToken": access_token,
        "refreshToken": str(data.get("refreshToken") or data.get("refresh_token") or "").strip(),
        "profileArn": str(data.get("profileArn") or data.get("profile_arn") or "").strip(),
        "expiresAt": str(data.get("expiresAt") or data.get("expires_at") or "").strip(),
        "authMethod": str(data.get("authMethod") or data.get("auth_method") or "").strip(),
        "provider": str(data.get("provider") or "").strip(),
        "region": str(data.get("region") or "").strip(),
    }


def _load_from_file() -> dict | None:
    for path in _local_token_paths():
        if not path.is_file():
            continue
        try:
            payload = _parse_token_payload(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as e:
            logger.debug("读取 Kiro 登录态文件失败 %s: %s", path, e)
            continue
        if payload:
            return payload
    return None


def _load_from_sqlite() -> dict | None:
    """从 CLI 的 data.sqlite3 读取令牌（结构未知，按 key 名探测，失败静默）。"""
    db_path = _cli_sqlite_path()
    if db_path is None:
        return None
    try:
        # uri 只读打开，避免与运行中的 CLI 争锁
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error as e:
        logger.debug("打开 Kiro CLI 数据文件失败: %s", e)
        return None
    try:
        tables = [
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        for table in tables:
            try:
                rows = conn.execute(f'SELECT * FROM "{table}"').fetchall()
                cols = [d[0] for d in conn.execute(f'SELECT * FROM "{table}" LIMIT 0').description or []]
            except sqlite3.Error:
                continue
            for row in rows:
                cells = dict(zip(cols, row))
                for key in _SQLITE_TOKEN_KEYS:
                    for cell in cells.values():
                        if isinstance(cell, str) and key in cell and "accessToken" in cell:
                            payload = _parse_token_payload(cell)
                            if payload:
                                return payload
    except sqlite3.Error as e:
        logger.debug("读取 Kiro CLI 令牌失败: %s", e)
    finally:
        conn.close()
    return None


def load_local_token() -> dict | None:
    """按优先级读取本机 Kiro 登录态；未找到返回 None。"""
    return _load_from_file() or _load_from_sqlite()


def _parse_expires_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _build_account(payload: dict) -> dict:
    """构造账号展示信息（label 取 profile 名，缺失回退邮箱/身份来源）。"""
    profile_arn = payload.get("profileArn") or ""
    profile_name = profile_arn.rsplit("/", 1)[-1] if profile_arn else ""
    identity = payload.get("provider") or ""
    auth_method = payload.get("authMethod") or "social"
    label = profile_name or identity or "Kiro 账号"
    return {
        "id": profile_name or identity or "kiro",
        "label": str(label)[:80],
        "provider": identity,
        "authMethod": auth_method,
        "profileArn": profile_arn,
    }


async def import_local_login(db: AsyncSession, provider_id: int) -> dict:
    """导入本机 Kiro 登录态到该供应商；返回 {account, region, profile_arn}。

    - 已过期 → 用 refresh_token 续期一次；续期失败抛 KiroAuthError(login_required)。
    - 无 refresh_token 且已过期 → 抛 KiroAuthError(login_required)。
    """
    payload = load_local_token()
    if not payload:
        raise KiroAuthError(
            "未找到本机 Kiro 登录态，请先在 Kiro IDE/CLI 中登录后再导入", "login_required")

    region = normalize_region(payload.get("region") or DEFAULT_REGION)
    refresh_token = payload.get("refreshToken") or ""
    profile_arn = payload.get("profileArn") or ""
    auth_method = payload.get("authMethod") or "social"
    access_token = payload["accessToken"]
    expires_at = payload.get("expiresAt") or ""

    expires_dt = _parse_expires_at(expires_at)
    expired = expires_dt is None or expires_dt <= datetime.now(timezone.utc)

    if expired:
        if not refresh_token:
            raise KiroAuthError("本机 Kiro 登录态已过期且无刷新令牌，请重新登录 Kiro", "login_required")
        from app.persistence.models.model_reg import Provider
        from app.services.credential_service import provider_client_kwargs
        from app.auth.kiro.token import refresh_access_token, resolve_endpoints

        provider = await db.get(Provider, provider_id)
        endpoints = resolve_endpoints(region, base_url_override=getattr(provider, "base_url", None))
        result = await refresh_access_token(
            endpoints, refresh_token, auth_method=auth_method,
            client_kwargs=provider_client_kwargs(provider))
        access_token = result["accessToken"]
        refresh_token = result.get("refreshToken") or refresh_token
        profile_arn = result.get("profileArn") or profile_arn
        from datetime import timedelta

        expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=int(result.get("expiresIn") or 3600))
        ).isoformat()

    account = _build_account({**payload, "profileArn": profile_arn, "provider": payload.get("provider")})
    await kiro_session.save_auth(
        db, provider_id,
        access_token=access_token,
        refresh_token=refresh_token or None,
        profile_arn=profile_arn or None,
        region=region,
        auth_method=auth_method,
        account=account,
        expires_at=expires_at or None,
    )
    return {"account": account, "region": region, "profile_arn": profile_arn}
