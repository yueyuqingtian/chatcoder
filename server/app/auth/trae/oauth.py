"""TRAE SOLO CN 浏览器授权 + AuthCode 换 Token（对齐 out/main.js oauth/marscode/request.js）。

流程：
1. 生成 PKCE(S256) + EC P-256 设备密钥对，启动本地回调 server
2. 构造授权 URL（www.trae.cn/authorization，auth_from=solo），前端打开系统浏览器
3. 回调 /authorize 收到 authCodeInfo（含 AuthCode）→
   POST https://api.trae.cn/trae/api/v3/oauth/ExchangeToken
   （ClientID + AuthCode + CodeVerifier + DeviceInfo）→ Result.Token / RefreshToken
4. 可选 GET/UserInfo 补全账号（有 userInfo 回调参数则跳过）

认证头：Content-Type: application/json + x-cloudide-token（AuthCode 换 token 时为空串）。
方案: docs/plan-trae-solo-provider-integration.md §1.2 / §5.1。
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.trae import callback as trae_callback
from app.auth.trae import device as trae_device
from app.auth.trae import pkce as trae_pkce
from app.core.config import settings

logger = logging.getLogger(__name__)

EXCHANGE_TOKEN_PATH = "/trae/api/v3/oauth/ExchangeToken"
GET_USER_INFO_PATH = "/cloudide/api/v3/trae/GetUserInfo"
CHECK_LOGIN_PATH = "/cloudide/api/v3/trae/CheckLogin"
CLEAR_REFRESH_TOKEN_PATH = "/cloudide/api/v3/trae/oauth/ClearRefreshToken"
AUTHORIZATION_PATH = "/authorization"

# CheckLogin 判定为"登录失效"的错误码（request.js Wx）
INVALID_TOKEN_CODES = {"20324", "20101", "20315", "20125", "20126", "20401", "20403"}

_TOKEN_TIMEOUT = 60.0

# 设备硬件描述（与真实客户端一致以过风控；从本机读取可留空）
_PLATFORM_CODE = "SOLO_PC"


@dataclass
class LoginResult:
    access_token: str
    refresh_token: str = ""
    token_expires_at: str | None = None
    refresh_expires_at: str | None = None
    account: dict | None = None


class TraeAuthError(Exception):
    """TRAE 认证错误。kind: login_required | refresh_failed | network | invalid_token"""

    def __init__(self, message: str, kind: str = "login_required", code: str = ""):
        super().__init__(message)
        self.kind = kind
        self.code = code


def _auth_headers(token: str) -> dict:
    """TRAE 认证接口头（request.js m()）。"""
    return {
        "Content-Type": "application/json",
        "x-cloudide-token": token,
        "User-Agent": getattr(settings, "trae_user_agent", ""),
    }


def _unwrap_result(resp: httpx.Response, url: str) -> dict:
    """解 {ResponseMetadata, Result} 信封；业务错误码抛 TraeAuthError。"""
    try:
        body = resp.json() if resp.content else {}
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    meta = body.get("ResponseMetadata") if isinstance(body.get("ResponseMetadata"), dict) else {}
    error = meta.get("Error") if isinstance(meta.get("Error"), dict) else {}
    code = str(error.get("Code") or "")
    if code:
        if code in INVALID_TOKEN_CODES:
            raise TraeAuthError(f"TRAE 登录态失效（{code}）", "login_required", code=code)
        raise TraeAuthError(f"TRAE 业务错误 {code}：{error.get('Message') or ''}".strip(),
                            "refresh_failed", code=code)
    result = body.get("Result")
    if not isinstance(result, dict):
        raise TraeAuthError(f"TRAE 响应无 Result：{url}", "refresh_failed")
    return result


async def exchange_auth_code(api_host: str, client_id: str, auth_code: str,
                             code_verifier: str, keypair: dict,
                             *, device_id: str, machine_id: str, ide_version: str) -> LoginResult:
    """POST {api_host}/trae/api/v3/oauth/ExchangeToken（AuthCode 模式）。"""
    url = f"{api_host.rstrip('/')}{EXCHANGE_TOKEN_PATH}"
    info = trae_device.device_info(
        keypair["public_key"], device_id=device_id, machine_id=machine_id,
        ide_version=ide_version, platform_code=_PLATFORM_CODE)
    body = {
        "ClientID": client_id,
        "AuthCode": auth_code,
        "CodeVerifier": code_verifier,
        "DeviceInfo": info,
        "IDEVersion": ide_version,
    }
    try:
        async with httpx.AsyncClient(timeout=_TOKEN_TIMEOUT) as client:
            resp = await client.post(url, json=body, headers=_auth_headers(""))
    except httpx.HTTPError as e:
        raise TraeAuthError(f"TRAE ExchangeToken 请求失败：{url}", "network") from e
    result = _unwrap_result(resp, url)
    return _parse_result(result)


def _parse_result(result: dict) -> LoginResult:
    token = str(result.get("Token") or "")
    refresh = str(result.get("RefreshToken") or "")
    if not token:
        raise TraeAuthError("TRAE ExchangeToken 响应缺 Token", "refresh_failed")
    account = {
        "user_id": str(result.get("UserID") or ""),
        "name": str(result.get("ScreenName") or ""),
        "region": str(result.get("AIRegion") or result.get("StoreCountry") or "cn"),
        "ai_region": str(result.get("AIRegion") or ""),
        "store_country": str(result.get("StoreCountry") or ""),
        "label": str(result.get("ScreenName") or "") or "已登录",
    }
    token_expire_at = result.get("TokenExpireAt")
    if token_expire_at is None:
        # 服务端只给 TokenExpireDuration（相对当前时刻的毫秒时长）
        token_expires_at = trae_device.parse_expire(
            result.get("TokenExpireDuration"), relative=True)
    else:
        token_expires_at = trae_device.parse_expire(token_expire_at)
    return LoginResult(
        access_token=token,
        refresh_token=refresh,
        token_expires_at=token_expires_at,
        refresh_expires_at=trae_device.parse_expire(result.get("RefreshExpireAt")),
        account=account,
    )


async def fetch_user_info(api_host: str, token: str, ide_version: str) -> dict:
    """POST {api_host}/cloudide/api/v3/trae/GetUserInfo → Result dict。"""
    url = f"{api_host.rstrip('/')}{GET_USER_INFO_PATH}"
    body = {"ReqSource": "Lite", "IDEVersion": ide_version}
    try:
        async with httpx.AsyncClient(timeout=_TOKEN_TIMEOUT) as client:
            resp = await client.post(url, json=body, headers=_auth_headers(token))
    except httpx.HTTPError as e:
        raise TraeAuthError(f"TRAE GetUserInfo 请求失败：{url}", "network") from e
    return _unwrap_result(resp, url)


async def check_login(api_host: str, token: str, ide_version: str) -> bool:
    """POST {api_host}/cloudide/api/v3/trae/CheckLogin → 是否仍有效。"""
    url = f"{api_host.rstrip('/')}{CHECK_LOGIN_PATH}"
    body = {"IDEVersion": ide_version, "ReqSource": "Lite", "GetAIPayHost": True}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=body, headers=_auth_headers(token))
    except httpx.HTTPError as e:
        raise TraeAuthError(f"TRAE CheckLogin 请求失败：{url}", "network") from e
    result = _unwrap_result(resp, url)
    return bool(result.get("IsLogin"))


async def revoke_refresh_token(api_host: str, token: str, *, client_id: str,
                               device_id: str, machine_id: str) -> None:
    """登出：POST /cloudide/api/v3/trae/oauth/ClearRefreshToken（忽略失败）。"""
    url = f"{api_host.rstrip('/')}{CLEAR_REFRESH_TOKEN_PATH}"
    body = {"ClientID": client_id, "DeviceID": device_id,
            "MachineID": machine_id, "PlatformCode": _PLATFORM_CODE}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            await client.post(url, json=body, headers=_auth_headers(token))
    except Exception as e:  # noqa: BLE001
        logger.warning("[trae] ClearRefreshToken 请求失败（忽略）: %s", e)


async def build_authorize_url(server: trae_callback.CallbackServer, *, client_id: str,
                              console_host: str, ide_version: str, machine_id: str,
                              device_id: str, device_brand: str = "", device_model: str = "",
                              os_name: str = "", os_version: str = "") -> tuple[str, str]:
    """构造授权 URL（对齐 loginUrlBuilder.js aie.buildLoginUrl），返回 (url, code_verifier)。

    code_verifier 须与授权 URL 中的 code_challenge 配套，回调后用于 ExchangeToken。
    """
    code_verifier = trae_pkce.gen_code_verifier()
    challenge = trae_pkce.s256_challenge(code_verifier)
    query = urlencode({
        "login_version": "1",
        "auth_from": "solo",
        "login_channel": "native_ide",
        "plugin_version": ide_version,
        "auth_type": "local",
        "client_id": client_id,
        "redirect": "0",
        "login_trace_id": uuid.uuid4().hex,
        "auth_callback_url": server.authorize_url,
        "machine_id": machine_id,
        "device_id": device_id,
        "x_device_id": device_id,
        "x_machine_id": machine_id,
        "x_device_brand": device_brand,
        "x_device_type": os_name,
        "x_os_version": os_version,
        "x_env": "",
        "x_app_version": ide_version,
        "x_app_type": "stable",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "hide_saas_login": "true",
    })
    return f"{console_host.rstrip('/')}{AUTHORIZATION_PATH}?{query}", code_verifier


async def start_login(db: AsyncSession, provider_id: int, *, api_host: str,
                      console_host: str, client_id: str, ide_version: str,
                      machine_id: str, device_id: str,
                      timeout_ms: int = trae_callback.DEFAULT_TIMEOUT_MS) -> dict:
    """登录入口：起回调 server + 构造授权 URL + 后台等回调换 token。

    返回 {"status": "pending", "authorize_url", "state", "expires_in"}；
    结果经 /login/status 轮询读取（对齐 ta3 oauth.start_browser_login）。
    """
    from app.auth.trae import session as trae_session

    server = await trae_callback.start_callback_server(timeout_ms)
    authorize_url, code_verifier = await build_authorize_url(
        server, client_id=client_id, console_host=console_host, ide_version=ide_version,
        machine_id=machine_id, device_id=device_id)

    keypair = trae_device.gen_device_keypair()

    async def _wait_and_exchange() -> dict:
        try:
            result = await server.wait_result()
            if result is None:
                return {"status": "failed", "error": "登录超时或被取消，请重试"}
            if result.get("error_code"):
                err_msg = result.get("error_msg") or ""
                return {"status": "failed",
                        "error": f"授权失败（{result['error_code']}）{err_msg}".strip()}
            auth_code = result.get("auth_code")
            if not auth_code:
                return {"status": "failed", "error": "未收到授权码，请重试"}
            login = await exchange_auth_code(
                api_host, client_id, auth_code, code_verifier, keypair,
                device_id=device_id, machine_id=machine_id, ide_version=ide_version)
            account = login.account or {}
            user_info = result.get("user_info")
            if isinstance(user_info, dict) and user_info:
                for k, src in (("user_id", "UserID"), ("name", "ScreenName"),
                               ("region", "AIRegion"), ("store_country", "StoreCountry")):
                    if user_info.get(src):
                        account[k] = user_info[src]
            await trae_session.save_auth(
                db, provider_id,
                access_token=login.access_token,
                refresh_token=login.refresh_token or None,
                account=account,
                token_expires_at=login.token_expires_at,
                refresh_expires_at=login.refresh_expires_at,
                device_private_key=keypair["private_key"],
                device_public_key=keypair["public_key"],
                device_id=device_id,
                machine_id=machine_id,
            )
            await db.commit()
            return {"status": "logged_in", "account": account,
                    "access_token": login.access_token[:24] + "…"}
        except Exception as e:  # noqa: BLE001
            logger.warning("[trae] provider=%s 登录回调处理失败: %s", provider_id, e)
            return {"status": "failed", "error": str(e)[:300]}
        finally:
            await trae_callback.stop_active_server()

    task = asyncio.create_task(_wait_and_exchange())
    _active_tasks[provider_id] = task

    return {
        "status": "pending",
        "authorize_url": authorize_url,
        "state": server.state,
        "port": server.port,
        "expires_in": timeout_ms // 1000,
    }


async def get_login_status(db: AsyncSession, provider_id: int) -> dict:
    """查询登录状态：优先 in-flight 任务结果，其次 DB 登录态。"""
    from app.auth.trae import session as trae_session

    task = _active_tasks.get(provider_id)
    if task is not None:
        if task.done():
            _active_tasks.pop(provider_id, None)
            try:
                return await task
            except Exception as e:  # noqa: BLE001
                return {"status": "failed", "error": str(e)[:300]}
        return {"status": "pending"}

    row = await trae_session.load_auth(db, provider_id)
    if row and row.access_token:
        return {"status": "logged_in", "account": row.account or {}}
    return {"status": "pending", "error": "未登录"}


async def cancel_login(db: AsyncSession, provider_id: int) -> None:
    await trae_callback.stop_active_server()
    task = _active_tasks.pop(provider_id, None)
    if task is not None:
        task.cancel()


# provider_id → in-flight 登录协程（进程内，重启即失效——登录本就是短生命周期操作）
_active_tasks: dict[int, object] = {}
