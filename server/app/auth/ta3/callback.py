"""本地回调 HTTP server — 浏览器 PKCE 登录凭证回收（对齐参考项目 auth/callbackServer.ts）。

监听 localhost 随机端口，路径 "/" 与 "/callback"：
- 收到 ?code=...&state=... → resolve 回调结果，返回"登录成功"页面
- 未带凭证 → 返回"正在确认登录"等待页（含 JS 把 hash/query 凭证转发到 /callback）

生命周期：start() 创建，wait_result() 阻塞等待（带超时），stop() 关闭。
模块级单例 _active，避免并发登录时端口堆积。
"""
from __future__ import annotations

import asyncio
import logging
import secrets
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_MS = 90 * 1000  # 对齐参考项目 BROWSER_LOGIN_TIMEOUT_MS

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

_WAITING_HTML = """<!doctype html>
<html lang="zh-CN">
  <head><meta charset="utf-8"><title>正在确认登录</title></head>
  <body style="display:flex;align-items:center;justify-content:center;height:100vh;margin:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f7f8fa;color:#24282f">
    <main style="text-align:center">
      <h2 id="title" style="margin:0 0 8px;color:#2f6fed">正在确认登录</h2>
      <p id="message" style="margin:0;color:#6b7280">请稍候，正在把网页登录状态同步到应用。</p>
      <script>
        (function () {
          function parseParams(text) { return new URLSearchParams(String(text || '').replace(/^#/, '').replace(/^\\?/, '')); }
          var searchParams = parseParams(window.location.search);
          var hashText = window.location.hash || '';
          if (hashText.indexOf('?') >= 0) { hashText = hashText.slice(hashText.indexOf('?') + 1); }
          var hashParams = parseParams(hashText);
          var code = searchParams.get('code') || hashParams.get('code') || '';
          var state = searchParams.get('state') || hashParams.get('state') || '';
          if (!code) {
            document.getElementById('title').textContent = '还未收到登录凭证';
            document.getElementById('message').textContent = '如果已经登录，请回到应用重新点击登录。';
            return;
          }
          fetch('/callback?code=' + encodeURIComponent(code) + (state ? '&state=' + encodeURIComponent(state) : ''), { cache: 'no-store' })
            .then(function () {
              document.getElementById('title').textContent = '登录成功';
              document.getElementById('title').style.color = '#168a4a';
              document.getElementById('message').textContent = '请返回应用。';
              setTimeout(function () { window.close(); }, 1200);
            })
            .catch(function () { window.location.replace('/callback?code=' + encodeURIComponent(code)); });
        }());
      </script>
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
        return f"http://localhost:{self.port}"

    async def start(self) -> "CallbackServer":
        self._server = await asyncio.start_server(self._handle, host="localhost", port=0)
        sockets = self._server.sockets or []
        if not sockets:
            raise RuntimeError("无法读取本地回调端口")
        self.port = sockets[0].getsockname()[1]
        logger.info("[ta3] 回调 server 已启动 localhost:%s", self.port)
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
            # 读完请求头（GET 无 body）
            while True:
                line = await reader.readline()
                if not line or line in (b"\r\n", b"\n"):
                    break
            line_text = request_line.decode("latin-1", errors="replace").strip()
            parts = line_text.split(" ")
            path = parts[1] if len(parts) > 1 else "/"
            parsed = urlparse(path)
            query = parse_qs(parsed.query)
            code = (query.get("code") or [""])[0]
            state = (query.get("state") or [""])[0]
            has_credential = bool(code)
            if has_credential:
                logger.info("[ta3] 回调收到 code（state=%s）", state or "-")
                self._settle({"code": code, "state": state})
            body = (_SUCCESS_HTML if has_credential else _WAITING_HTML).encode("utf-8")
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                b"Cache-Control: no-store\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
                + body
            )
            await writer.drain()
        except Exception as e:  # noqa: BLE001
            logger.warning("[ta3] 回调处理异常: %s", e)
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
