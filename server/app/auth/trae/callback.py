"""本地回调 HTTP server — TRAE 授权页凭证回收（对齐 out/main.js oauth/oauthLocalServer.js）。

TRAE 授权页登录完成后跳转 auth_callback_url（http://127.0.0.1:{port}/authorize），
query 携带：
- authCodeInfo：JSON 字符串，含 AuthCode（用于 ExchangeToken）+ userTag
- userInfo：可选 JSON（直接用户信息，有则不调 GetUserInfo）
- error_code / error_msg：登录失败信息

生命周期：start() 创建，wait_result() 阻塞等待（带超时），stop() 关闭。
模块级单例 _active，避免并发登录时端口堆积（对齐 ta3 callback.py）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_MS = 5 * 60 * 1000  # 对齐 OAuthLocalServer AUTHORIZATION_TIMEOUT_MS

_SUCCESS_HTML = """<!doctype html>
<html lang="zh-CN">
  <head><meta charset="utf-8"><title>登录成功</title></head>
  <body style="display:flex;align-items:center;justify-content:center;height:100vh;margin:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f7f8fa;color:#24282f">
    <main style="text-align:center">
      <h2 style="margin:0 0 8px;color:#168a4a">登录成功</h2>
      <p style="margin:0;color:#6b7280">请返回应用。</p>
      <script>setTimeout(function(){ window.close(); }, 1600)</script>
    </main>
  </body>
</html>"""

_ERROR_HTML = """<!doctype html>
<html lang="zh-CN">
  <head><meta charset="utf-8"><title>登录失败</title></head>
  <body style="display:flex;align-items:center;justify-content:center;height:100vh;margin:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f7f8fa;color:#24282f">
    <main style="text-align:center">
      <h2 style="margin:0 0 8px;color:#d92d20">登录失败</h2>
      <p style="margin:0;color:#6b7280">请返回应用重试。</p>
      <script>setTimeout(function(){ window.close(); }, 1600)</script>
    </main>
  </body>
</html>"""


class CallbackServer:
    def __init__(self, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        self.state = secrets.token_hex(16)
        self.port = 0
        self._timeout = timeout_ms
        self._server: asyncio.AbstractServer | None = None
        self._result: dict | None = None
        self._event = asyncio.Event()
        self._settled = False

    @property
    def callback_base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def authorize_url(self) -> str:
        """auth_callback_url：授权页完成登录后跳回本 server 的 /authorize 路径。"""
        return f"{self.callback_base_url}/authorize"

    async def start(self) -> "CallbackServer":
        self._server = await asyncio.start_server(self._handle, host="127.0.0.1", port=0)
        sockets = self._server.sockets or []
        if not sockets:
            raise RuntimeError("无法读取本地回调端口")
        self.port = sockets[0].getsockname()[1]
        logger.info("[trae] 回调 server 已启动 127.0.0.1:%s", self.port)
        return self

    def _settle(self, result: dict | None) -> None:
        if self._settled:
            return
        self._settled = True
        self._result = result
        self._event.set()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await reader.readline()
            if not request_line:
                writer.close()
                return
            while True:
                line = await reader.readline()
                if not line or line in (b"\r\n", b"\n"):
                    break
            line_text = request_line.decode("latin-1", errors="replace").strip()
            parts = line_text.split(" ")
            path = parts[1] if len(parts) > 1 else "/"
            parsed = urlparse(path)
            query = parse_qs(parsed.query)

            error_code = (query.get("error_code") or [""])[0]
            if error_code:
                logger.warning("[trae] 授权失败 error_code=%s error_msg=%s",
                               error_code, (query.get("error_msg") or [""])[0])
                self._settle({"error_code": error_code,
                              "error_msg": (query.get("error_msg") or [""])[0]})
                body = _ERROR_HTML.encode("utf-8")
            else:
                auth_code_info_raw = (query.get("authCodeInfo") or [""])[0]
                auth_code = ""
                user_tag = None
                if auth_code_info_raw:
                    try:
                        info = json.loads(auth_code_info_raw)
                        if isinstance(info, dict):
                            auth_code = str(info.get("AuthCode") or "")
                            user_tag = info.get("userTag")
                    except ValueError:
                        logger.warning("[trae] authCodeInfo JSON 解析失败")
                if auth_code:
                    state_val = (query.get("state") or [""])[0]
                    logger.info("[trae] 回调收到 AuthCode（state=%s）", state_val)
                    result = {"auth_code": auth_code, "state": state_val}
                    if user_tag is not None:
                        result["user_tag"] = user_tag
                    user_info_raw = (query.get("userInfo") or [""])[0]
                    if user_info_raw:
                        try:
                            result["user_info"] = json.loads(user_info_raw)
                        except ValueError:
                            logger.warning("[trae] userInfo JSON 解析失败")
                    self._settle(result)
                    body = _SUCCESS_HTML.encode("utf-8")
                else:
                    # 未带凭证：保持等待（OAuthLocalServer 行为），不 settle
                    body = "<html><body><h2>正在确认登录...</h2></body></html>".encode("utf-8")

            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                b"Cache-Control: no-store\r\nAccess-Control-Allow-Origin: *\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
                + body
            )
            await writer.drain()
        except Exception as e:  # noqa: BLE001
            logger.warning("[trae] 回调处理异常: %s", e)
            try:
                writer.write(b"HTTP/1.1 500 Internal Server Error\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
            except Exception:  # noqa: BLE001
                pass
        finally:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    async def wait_result(self) -> dict | None:
        """阻塞等待回调结果；超时返回 None。"""
        try:
            await asyncio.wait_for(self._event.wait(), timeout=self._timeout / 1000.0)
        except asyncio.TimeoutError:
            return None
        return self._result

    async def cancel(self) -> None:
        self._settle(None)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            self._server = None


_active: CallbackServer | None = None
_active_lock: asyncio.Lock | None = None


def _lock() -> asyncio.Lock:
    global _active_lock
    if _active_lock is None:
        _active_lock = asyncio.Lock()
    return _active_lock


async def start_callback_server(timeout_ms: int = DEFAULT_TIMEOUT_MS) -> CallbackServer:
    """启动（或替换）当前登录回调 server。旧的自动取消并关闭。"""
    global _active
    async with _lock():
        if _active is not None:
            await _active.cancel()
            await _active.stop()
        server = CallbackServer(timeout_ms=timeout_ms)
        await server.start()
        _active = server
        return server


async def stop_active_server() -> None:
    global _active
    async with _lock():
        if _active is not None:
            await _active.cancel()
            await _active.stop()
            _active = None
