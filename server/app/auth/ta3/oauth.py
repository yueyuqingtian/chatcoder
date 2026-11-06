"""plan-20261106-ta3-080：网页登录使用 ta3-desktop/S256，后台任务独立持有数据库会话。"""
from __future__ import annotations

import asyncio
import base64
import logging
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qs, urljoin

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.ta3 import callback, endpoints, session
from app.auth.ta3.pkce import gen_code_verifier, s256_challenge
from app.persistence.database import async_session_factory

DEFAULT_TA3_API_BASE = endpoints.DEFAULT_TA3_API_BASE
YINHAI_OAUTH_CLIENT_ID = endpoints.OAUTH_CLIENT_ID
OAUTH_AUTHORIZE_PATH = "/api/oauth/authorize"
OAUTH_TOKEN_PATH = "/api/oauth/token"


@dataclass
class LoginResult:
    access_token: str
    refresh_token: str = ""
    account: dict | None = None
    expires_in: float | None = None
    source: str = "pkce"


@dataclass
class Attempt:
    server: callback.CallbackServer
    generation: int
    start: dict
    task: asyncio.Task | None = None


_attempts: dict[int, Attempt] = {}
_login_locks: dict[int, asyncio.Lock] = {}
_terminal: dict[int, dict] = {}
logger = logging.getLogger(__name__)


async def exchange_authorization_code(api_base, code, code_verifier, redirect_uri, client_kwargs=None):
    body = await session.request_oauth_token(api_base, {
        "grant_type": "authorization_code", "code": code, "code_verifier": code_verifier,
        "redirect_uri": redirect_uri, "client_id": YINHAI_OAUTH_CLIENT_ID,
    }, client_kwargs)
    account = {"id": body.get("loginId") or "", "loginId": body.get("loginId") or "",
               "userId": body.get("userId") or "", "orgId": body.get("orgId") or "",
               "label": body.get("userName") or body.get("loginId") or "已登录"}
    return LoginResult(body["accessToken"], body.get("refreshToken") or "", account, body.get("expiresIn"))


async def browser_entry(authorize_url, client_kwargs=None):
    original = urlsplit(authorize_url)
    if original.hostname in ("localhost", "127.0.0.1"):
        return authorize_url
    try:
        async with httpx.AsyncClient(timeout=5, **{**(client_kwargs or {}), "follow_redirects": False}) as client:
            response = await client.get(authorize_url)
        if response.status_code not in (301, 302, 303, 307, 308):
            return authorize_url
        location = urlsplit(urljoin(authorize_url, response.headers.get("location", "")))
        if location.hostname not in ("localhost", "127.0.0.1") or location.port != 8080:
            return authorize_url
        encoded = (parse_qs(location.query).get("oauth_authorize") or [""])[0]
        if base64.b64decode(encoded, validate=True).decode("utf-8") != authorize_url:
            return authorize_url
        host = original.hostname
        if ":" in host:
            host = f"[{host}]"
        return urlunsplit((location.scheme, f"{host}:8080", location.path, location.query, location.fragment))
    except (httpx.HTTPError, ValueError, UnicodeError):
        return authorize_url


async def start_login(db: AsyncSession, provider_id: int, api_base: str,
                      timeout_ms: int = callback.DEFAULT_TIMEOUT_MS):
    from app.persistence.models.model_reg import Provider
    async with _login_locks.setdefault(provider_id, asyncio.Lock()):
        current = _attempts.get(provider_id)
        if current and current.task and not current.task.done():
            return current.start
        provider = await db.get(Provider, provider_id, populate_existing=True)
        if provider is None:
            raise session.Ta3AuthError("供应商已删除")
        base = await endpoints.resolve(provider)
        return await start_browser_login(db, provider_id, base, timeout_ms, endpoints.client_options(provider))


async def start_browser_login(db, provider_id, api_base, timeout_ms=callback.DEFAULT_TIMEOUT_MS, client_kwargs=None):
    gen = await session.advance_generation(db, provider_id)
    server = await callback.start_callback_server(timeout_ms)
    server.success_base = api_base
    verifier = gen_code_verifier()
    url = api_base.rstrip("/") + OAUTH_AUTHORIZE_PATH + "?" + urlencode({
        "response_type": "code", "client_id": YINHAI_OAUTH_CLIENT_ID, "redirect_uri": server.callback_url,
        "code_challenge": s256_challenge(verifier), "code_challenge_method": "S256", "state": server.state,
    })
    try:
        url = await browser_entry(url, client_kwargs)
    except BaseException:
        await server.stop()
        raise
    start = {"status": "pending", "authorize_url": url, "state": server.state,
             "port": server.port, "expires_in": timeout_ms // 1000}
    attempt = Attempt(server, gen, start)
    _attempts[provider_id] = attempt
    _terminal.pop(provider_id, None)

    async def complete():
        result = {"status": "cancelled"}
        try:
            response = await server.wait_result()
            if response is None:
                return result
            if response.get("error"):
                return {"status": "failed", "error": response["error"]}
            if response.get("state") != server.state or not response.get("code"):
                return {"status": "failed", "error": "授权回调校验失败，请重试"}
            login = await exchange_authorization_code(api_base, response["code"], verifier, server.callback_url, client_kwargs)
            async with async_session_factory() as own_db:
                saved = await session.save_auth(own_db, provider_id, access_token=login.access_token,
                    refresh_token=login.refresh_token, account=login.account, expires_in=login.expires_in,
                    expected_generation=gen)
            if not saved:
                await session.revoke(login.refresh_token, api_base, client_kwargs)
                return result
            return {"status": "logged_in", "account": login.account}
        except asyncio.CancelledError:
            return result
        except session.Ta3AuthError as exc:
            logger.warning("[ta3] provider=%s 网页授权失败：%s", provider_id, exc.kind)
            return {"status": "failed", "error": str(exc)}
        except Exception:
            logger.warning("[ta3] provider=%s 网页授权回调处理失败", provider_id, exc_info=True)
            return {"status": "failed", "error": "网页登录未完成，请检查网络或重新授权"}
        finally:
            await server.stop()

    attempt.task = asyncio.create_task(complete())
    return start


async def get_login_status(db, provider_id):
    attempt = _attempts.get(provider_id)
    if attempt and attempt.task:
        if not attempt.task.done():
            return {"status": "pending"}
        result = await attempt.task
        if _attempts.get(provider_id) is attempt:
            _attempts.pop(provider_id, None)
            _terminal[provider_id] = result
        return result
    if provider_id in _terminal and _terminal[provider_id].get("status") != "logged_in":
        return _terminal[provider_id]
    row = await session.load_auth(db, provider_id)
    if row and row.access_token and row.auth_version == endpoints.AUTH_VERSION:
        return {"status": "logged_in", "account": row.account or {}}
    return {"status": "failed", "error": "请先网页登录"}


async def cancel_login(db, provider_id):
    # plan-20261106-ta3-080：与启动共享锁，确保取消涵盖尚在构造授权入口的尝试。
    async with _login_locks.setdefault(provider_id, asyncio.Lock()):
        await session.advance_generation(db, provider_id)
        attempt = _attempts.pop(provider_id, None)
        if attempt:
            await attempt.server.cancel()
            if attempt.task:
                attempt.task.cancel()
                await asyncio.gather(attempt.task, return_exceptions=True)
            await attempt.server.stop()
        _terminal[provider_id] = {"status": "cancelled"}
