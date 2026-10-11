"""kiro（AWS Kiro）端点推导与令牌刷新。

契约来源（与 claude-kiro.js / kiro-cli 一致）：

- 对话：POST https://q.{region}.amazonaws.com/generateAssistantResponse
- 额度：GET  https://q.{region}.amazonaws.com/getUsageLimits
- 刷新（社交登录）：POST https://prod.{region}.auth.desktop.kiro.dev/refreshToken
  body {"refreshToken": …} → {accessToken, refreshToken, profileArn, expiresIn}
- 刷新（IDC）：POST https://oidc.{region}.amazonaws.com/token
  body {clientId, clientSecret, refreshToken, grantType:"refresh_token"}

region 默认为 us-east-1（Kiro 主分区，模型最全）；「放开地区限制」即在此处
把分区锁定为可控值：默认不跟随账号自动路由，端点可被供应商 base_url 覆盖。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

DEFAULT_REGION = "us-east-1"

# AWS 区域名：两字母分区 + 连字符分组 + 数字后缀（对齐 KiroCrew _REGION_RE）
_REGION_RE = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d{1,2}$")

_REFRESH_TIMEOUT = 15.0

# 社交登录固定客户端标识（非机密，kiro-cli 同款）
SOCIAL_AUTH_METHOD = "social"


@dataclass(frozen=True)
class KiroEndpoints:
    """一个区域的端点集合（base_url 覆盖时 chat/quota 走覆盖基址）。"""

    region: str
    chat: str
    quota: str
    refresh_social: str
    refresh_idc: str


def normalize_region(region: str | None) -> str:
    """规范化 region；缺失或非法一律回退 us-east-1（保证端点始终指向已知生产主机）。"""
    value = (region or "").strip().lower()
    if value and _REGION_RE.match(value):
        return value
    return DEFAULT_REGION


def resolve_endpoints(region: str | None, base_url_override: str | None = None) -> KiroEndpoints:
    """按 region 推导端点；base_url_override（供应商自填）优先于内置对话端点。

    覆盖值有三种形态：
    - 纯区域名（如 ``eu-central-1``）：等价于切换 API 分区（「放开地区限制」的快捷入口）；
    - 以 generateAssistantResponse 结尾：直接作为对话端点，quota 由同名替换得到；
    - 其它（反代根地址）：拼 /generateAssistantResponse 与 /getUsageLimits。
    """
    override = (base_url_override or "").strip().rstrip("/")
    if override and _REGION_RE.match(override.lower()):
        # 区域名快捷切换：覆盖值即目标分区，端点全部按该分区重建
        region = normalize_region(override)
        override = ""
    else:
        region = normalize_region(region)

    chat = f"https://q.{region}.amazonaws.com/generateAssistantResponse"
    quota = f"https://q.{region}.amazonaws.com/getUsageLimits"

    if override:
        if override.endswith("generateAssistantResponse"):
            chat = override
            quota = override[: -len("generateAssistantResponse")] + "getUsageLimits"
        else:
            chat = f"{override}/generateAssistantResponse"
            quota = f"{override}/getUsageLimits"

    return KiroEndpoints(
        region=region,
        chat=chat,
        quota=quota,
        refresh_social=f"https://prod.{region}.auth.desktop.kiro.dev/refreshToken",
        refresh_idc=f"https://oidc.{region}.amazonaws.com/token",
    )


class KiroAuthError(Exception):
    """kiro 认证错误。kind: login_required | refresh_failed | network"""

    def __init__(self, message: str, kind: str = "login_required"):
        super().__init__(message)
        self.kind = kind


async def refresh_access_token(
    endpoints: KiroEndpoints,
    refresh_token: str,
    *,
    auth_method: str = SOCIAL_AUTH_METHOD,
    client_id: str = "",
    client_secret: str = "",
    client_kwargs: dict | None = None,
) -> dict:
    """刷新 access_token；社交与 IDC 双路。

    返回 {accessToken, refreshToken, profileArn, expiresIn}。
    失败（invalid_grant 等）抛 KiroAuthError(kind='login_required')。
    """
    is_social = (auth_method or SOCIAL_AUTH_METHOD).lower() in ("social", "", "google", "github")
    if is_social:
        url = endpoints.refresh_social
        payload: dict = {"refreshToken": refresh_token}
    else:
        if not (client_id and client_secret):
            raise KiroAuthError("IDC 刷新需要 client_id 与 client_secret，请重新登录", "login_required")
        url = endpoints.refresh_idc
        payload = {
            "clientId": client_id,
            "clientSecret": client_secret,
            "refreshToken": refresh_token,
            "grantType": "refresh_token",
        }

    try:
        async with httpx.AsyncClient(timeout=_REFRESH_TIMEOUT, **(client_kwargs or {})) as client:
            resp = await client.post(url, json=payload)
    except httpx.HTTPError as e:
        raise KiroAuthError("Kiro 令牌刷新请求失败，请检查网络", "network") from e

    try:
        body = resp.json() if resp.content else {}
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}

    access_token = str(body.get("accessToken") or "").strip()
    if not resp.is_success or not access_token:
        error = str(body.get("error") or body.get("message") or f"http_{resp.status_code}")
        lowered = error.lower()
        if resp.status_code in (400, 401, 403) or "invalid_grant" in lowered:
            raise KiroAuthError("Kiro 登录已过期，请重新登录", "login_required")
        raise KiroAuthError(f"Kiro 令牌刷新失败：{error}", "refresh_failed")

    try:
        expires_in = int(body.get("expiresIn") or 3600)
    except (TypeError, ValueError):
        expires_in = 3600
    return {
        "accessToken": access_token,
        "refreshToken": str(body.get("refreshToken") or refresh_token),
        "profileArn": str(body.get("profileArn") or ""),
        "expiresIn": expires_in,
    }
