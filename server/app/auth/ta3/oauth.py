"""浏览器 PKCE(SM3) 登录（对齐参考项目 authService.ts:386-483 startBrowserLogin）。

流程：
1. 生成 verifier/challenge（SM3），启动本地回调 server
2. 返回 authorize_url（前端用系统浏览器打开）
3. 回调收到 code → POST /api/oauth/token（authorization_code + code_verifier）
4. 返回 token 结果（含账号字段）

另提供 IM 静默登录（银海通 :13631/getuid → /newcoder/aiContinueLogin），
仅在本地存在银海通服务时可用，失败静默降级 PKCE。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.ta3 import callback as ta3_callback
from app.auth.ta3.pkce import gen_code_verifier, sm3_challenge

logger = logging.getLogger(__name__)

OAUTH_AUTHORIZE_PATH = "/api/oauth/authorize"
OAUTH_TOKEN_PATH = "/api/oauth/token"
YINHAI_OAUTH_CLIENT_ID = "ide-vscode"
AICONTINUE_LOGIN_PATH = "/aiContinueLogin"
IM_SERVICE_URL = "http://localhost:13631/getuid"
# 默认服务端（对齐参考项目 serverEndpoints.ts 硬编码默认）
DEFAULT_TA3_API_BASE = "https://lc.yinhaiyun.com/newcoder"
_IM_TIMEOUT = 1.8
_IM_LOGIN_TIMEOUT = 8.0
_TOKEN_TIMEOUT = 10.0


@dataclass
class LoginResult:
    access_token: str
    refresh_token: str = ""
    account: dict | None = None
    source: str = "pkce"  # pkce | im


async def start_login(db: AsyncSession, provider_id: int, api_base: str,
                      timeout_ms: int = ta3_callback.DEFAULT_TIMEOUT_MS) -> dict:
    """登录入口（对齐参考项目 authService.ts:744-762 startLogin）。

    1. 先试银海通 IM 静默登录（本机 :13631 有银海通时立即成功，无需浏览器）；
    2. IM 失败才降级浏览器 PKCE(SM3)。
    返回 {"status": "logged_in"|"pending", ...}——status=logged_in 表示已登录。
    """
    from app.auth.ta3 import session as ta3_session

    im_result = await try_im_login(api_base)
    if im_result is not None:
        logger.info("[ta3] provider=%s IM 静默登录成功", provider_id)
        await ta3_session.save_auth(
            db, provider_id,
            access_token=im_result.access_token,
            refresh_token=None,  # IM 路径无 refresh_token（对齐参考项目，过期重走登录）
            account=im_result.account,
        )
        await db.commit()
        return {"status": "logged_in", "account": im_result.account, "source": "im"}

    return await start_browser_login(db, provider_id, api_base, timeout_ms=timeout_ms)


async def exchange_authorization_code(api_base: str, code: str, code_verifier: str,
                                      redirect_uri: str) -> LoginResult:
    """POST {apiBase}/api/oauth/token（grant_type=authorization_code + code_verifier）。"""
    url = f"{api_base.rstrip('/')}{OAUTH_TOKEN_PATH}"
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": code_verifier,
        "redirect_uri": redirect_uri,
        "client_id": YINHAI_OAUTH_CLIENT_ID,
    }
    try:
        async with httpx.AsyncClient(timeout=_TOKEN_TIMEOUT) as client:
            resp = await client.post(url, data=data,
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
    except httpx.HTTPError as e:
        raise RuntimeError(f"OAuth token 请求失败：{url}") from e

    try:
        body = resp.json() if resp.content else {}
    except ValueError:
        body = {}
    access_token = body.get("access_token") or body.get("accessToken") or body.get("token")
    if not resp.is_success or not access_token:
        error_code = body.get("error") or body.get("error_code") or f"http_{resp.status_code}"
        desc = body.get("error_description") or body.get("errorMessage") or ""
        raise RuntimeError(f"OAuth token 换取失败：{error_code}{('（' + str(desc) + '）') if desc else ''}")

    account = {
        "id": body.get("login_id") or body.get("loginId") or "",
        "loginId": body.get("login_id") or body.get("loginId") or "",
        "userId": body.get("user_id") or body.get("userId") or "",
        "orgId": body.get("org_id") or body.get("orgId") or "",
        "label": body.get("user_name") or body.get("userName") or body.get("login_id") or body.get("loginId") or "已登录",
    }
    return LoginResult(
        access_token=access_token,
        refresh_token=body.get("refresh_token") or body.get("refreshToken") or "",
        account=account,
        source="pkce",
    )


async def start_browser_login(db: AsyncSession, provider_id: int, api_base: str,
                              timeout_ms: int = ta3_callback.DEFAULT_TIMEOUT_MS) -> dict:
    """启动浏览器登录，返回 {authorize_url, state, port, expires_in}。

    登录完成（或超时）后自动 exchange code 并存库，返回的 dict 带 result 字段：
    - pending：回调尚未完成（前端需轮询 /login/status）
    - completed：已登录（含 access_token 等）
    - failed：超时/错误（含 error 信息）
    """
    from app.auth.ta3 import session as ta3_session

    verifier = gen_code_verifier()
    challenge = sm3_challenge(verifier)
    server = await ta3_callback.start_callback_server(timeout_ms)

    authorize_url = (
        f"{api_base.rstrip('/')}{OAUTH_AUTHORIZE_PATH}"
        f"?response_type=code&client_id={YINHAI_OAUTH_CLIENT_ID}"
        f"&redirect_uri={server.callback_base_url}"
        f"&code_challenge={challenge}&code_challenge_method=SM3&state={server.state}"
    )

    async def _wait_and_exchange() -> dict:
        result = await server.wait_result()
        try:
            if result is None:
                return {"status": "failed", "error": "登录超时或被取消，请重试"}
            if result.get("state") != server.state:
                return {"status": "failed", "error": "安全校验失败（state 不匹配），请重试"}
            code = result.get("code")
            if not code:
                return {"status": "failed", "error": "未收到授权码，请重试"}
            login = await exchange_authorization_code(
                api_base, code, verifier, server.callback_base_url,
            )
            await ta3_session.save_auth(
                db, provider_id,
                access_token=login.access_token,
                refresh_token=login.refresh_token or None,
                account=login.account,
            )
            return {
                "status": "completed",
                "account": login.account,
                "access_token": login.access_token[:24] + "…",
            }
        except Exception as e:  # noqa: BLE001
            logger.warning("[ta3] provider=%s 登录回调处理失败: %s", provider_id, e)
            return {"status": "failed", "error": str(e)[:300]}
        finally:
            await ta3_callback.stop_active_server()

    import asyncio
    task = asyncio.create_task(_wait_and_exchange())
    # 结果通过 /login/status 轮询读取；此 dict 仅携带启动信息
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
    from app.auth.ta3 import session as ta3_session

    task = _active_tasks.get(provider_id)
    if task is not None:
        if task.done():
            _active_tasks.pop(provider_id, None)
            try:
                return await task
            except Exception as e:  # noqa: BLE001
                return {"status": "failed", "error": str(e)[:300]}
        return {"status": "pending"}

    row = await ta3_session.load_auth(db, provider_id)
    if row and row.access_token:
        return {"status": "logged_in", "account": row.account or {}}
    return {"status": "pending", "error": "未登录"}


async def cancel_login(db: AsyncSession, provider_id: int) -> None:
    await ta3_callback.stop_active_server()
    task = _active_tasks.pop(provider_id, None)
    if task is not None:
        task.cancel()


# provider_id → in-flight 登录协程（进程内，重启即失效——登录本就是短生命周期操作）
_active_tasks: dict[int, object] = {}


async def try_im_login(api_base: str) -> LoginResult | None:
    """银海通 IM 静默登录（对齐参考项目 authService.ts:321-357 tryImLogin）。

    本机装有银海通（localhost:13631）时优先；失败返回 None 由调用方降级 PKCE。
    """
    try:
        async with httpx.AsyncClient(timeout=_IM_TIMEOUT) as client:
            resp = await client.get(IM_SERVICE_URL)
            data = resp.json() if resp.content else {}
        if not data.get("success"):
            return None
        uid = (data.get("data") or {}).get("uid")
        if not uid:
            return None
    except Exception:  # noqa: BLE001
        return None

    try:
        async with httpx.AsyncClient(timeout=_IM_LOGIN_TIMEOUT) as client:
            resp = await client.post(
                f"{api_base.rstrip('/')}{AICONTINUE_LOGIN_PATH}",
                headers={"Authorization": uid},
                content=b"",
            )
            body = resp.json() if resp.content else {}
        if not resp.is_success:
            return None
        data = body.get("data") or {}
        auth_token = data.get("authToken") or data.get("token")
        if not auth_token:
            return None
        return LoginResult(
            access_token=auth_token,
            account={
                "id": data.get("loginId") or "",
                "loginId": data.get("loginId") or "",
                "label": data.get("label") or "已登录",
            },
            source="im",
        )
    except Exception:  # noqa: BLE001
        return None
