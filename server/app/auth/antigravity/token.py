"""antigravity 端点/客户端凭据/请求头（对齐 kiro token 范式的单一配置源）。

方案 docs/plan-antigravity-provider-integration.md §1.3-1.5、§2.1。

- 端点、OAuth 客户端凭据、ClientMetadata 枚举全部来自 settings，不散落在业务代码里
  （逆向通道随时可能变更，改配置即可）。
- build_headers() 产出 CloudCode 网关要求的全部伪装头；实测缺 X-Goog-Api-Client 或
  Client-Metadata 会 400。
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass

from app.core.config import settings

# 实测：刷新/授权失败的两个错误码，用于区分「该重新登录」与「临时故障」
INVALID_GRANT = "invalid_grant"
INVALID_CLIENT = "invalid_client"


class AntigravityAuthError(Exception):
    """鉴权失败。

    kind:
    - login_required：无登录态 / 刷新令牌失效，需要用户重新导入或重新登录；
    - refresh_failed：临时故障（网络、5xx），可稍后重试。
    """

    def __init__(self, message: str, kind: str = "login_required"):
        super().__init__(message)
        self.message = message
        self.kind = kind


@dataclass(frozen=True)
class AntigravityEndpoints:
    """一组端点（默认生产；可用 settings 指向 sandbox 便于排障）。"""

    gateway: str
    token: str
    authorize: str
    api_key_base: str


def resolve_endpoints(base_url_override: str | None = None) -> AntigravityEndpoints:
    """按「供应商 base_url 覆盖 → 全局配置」推导端点。

    供应商 base_url 只覆盖网关地址（与 kiro 的 region 覆盖同理），
    OAuth 端点始终用 google 官方，避免用户误填导致刷新失败。
    """
    gateway = (base_url_override or "").strip().rstrip("/") or settings.antigravity_gateway_endpoint
    return AntigravityEndpoints(
        gateway=gateway,
        token=settings.antigravity_token_endpoint,
        authorize=settings.antigravity_authorize_endpoint,
        api_key_base=settings.antigravity_api_key_base_url,
    )


def client_metadata() -> dict:
    """CloudCode ClientMetadata。platform 必须是 WINDOWS_AMD64 这类带架构的值。"""
    return {
        "ideType": settings.antigravity_ide_type,
        "platform": settings.antigravity_platform,
        "pluginType": settings.antigravity_plugin_type,
    }


def client_metadata_json() -> str:
    return json.dumps(client_metadata(), separators=(",", ":"))


def user_agent() -> str:
    """语言服务器真身的 User-Agent。

    Google 依据它判断客户端版本，只向足够新的版本下发 Gemini 4 系列模型，
    且格式必须与真身一致（缺少 cl= 或 os_type 会被当作旧/未知客户端）：

        antigravity/hub/2.32.1 (aidev_client; os_type=windows; arch=amd64; cl=1021296516)
    """
    return (
        f"antigravity/hub/{settings.antigravity_client_version} "
        f"(aidev_client; os_type={settings.antigravity_os_type}; "
        f"arch={settings.antigravity_arch}; cl={settings.antigravity_client_changelist})"
    )


def build_headers(access_token: str, *, stream: bool = False) -> dict[str, str]:
    """CloudCode 网关请求头（方案 §2.1）。"""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "User-Agent": user_agent(),
        "X-Goog-Api-Client": "google-cloud-sdk vscode_cloudshelleditor/0.1",
        "Client-Metadata": client_metadata_json(),
    }
    if stream:
        headers["Accept"] = "text/event-stream"
    return headers


def decode_id_token(id_token: str | None) -> dict:
    """解析 id_token 的 payload（不验签，仅取 email/sub 做账号标签）。

    失败返回空 dict——标签缺失不该让登录流程整体失败。
    """
    if not id_token or id_token.count(".") < 2:
        return {}
    try:
        seg = id_token.split(".")[1]
        seg += "=" * (-len(seg) % 4)
        raw = base64.urlsafe_b64decode(seg.replace("-", "+").replace("_", "/"))
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 - 解析失败按无信息处理
        return {}


def build_account(id_token: str | None, *, fallback: str = "Antigravity 账号") -> dict:
    """构造账号展示信息（label 取邮箱，缺失回退默认文案）。"""
    payload = decode_id_token(id_token)
    email = str(payload.get("email") or "")
    label = (email or fallback)[:80]
    return {
        "email": email,
        "sub": str(payload.get("sub") or ""),
        "name": str(payload.get("name") or ""),
        "picture": str(payload.get("picture") or ""),
        "label": label,
    }
