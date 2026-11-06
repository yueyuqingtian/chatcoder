"""plan-20261106-ta3-080：回调实例归属一次登录，旧尝试不能关闭新尝试。"""
from __future__ import annotations

import asyncio
import secrets
from urllib.parse import parse_qs, urlsplit

DEFAULT_TIMEOUT_MS = 90_000


class CallbackServer:
    def __init__(self, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        self.state = secrets.token_urlsafe(32)
        self.port = 0
        self.success_base = ""
        self._timeout = timeout_ms
        self._server = None
        self._event = asyncio.Event()
        self._result = None
        self._settled = False

    @property
    def callback_base_url(self) -> str:
        return f"http://localhost:{self.port}"

    @property
    def callback_url(self) -> str:
        return self.callback_base_url + "/callback"

    async def start(self):
        # plan-20261106-ta3-080：不同地址族必须共用同一端口，避免 localhost 解析到未监听端口。
        self._server = await asyncio.start_server(self._handle, host="localhost", port=0)
        sockets = self._server.sockets or []
        self.port = sockets[0].getsockname()[1]
        if any(sock.getsockname()[1] != self.port for sock in sockets):
            await self.stop()
            self._server = await asyncio.start_server(self._handle, host="localhost", port=self.port)
        return self

    def _settle(self, result):
        if not self._settled:
            self._settled = True
            self._result = result
            self._event.set()

    async def _handle(self, reader, writer):
        status, location, message = "400 Bad Request", "", "无效的登录回调，请返回应用重试。"
        try:
            line = await asyncio.wait_for(reader.readline(), 5)
            parts = line.decode("latin-1").split()
            while True:
                header = await asyncio.wait_for(reader.readline(), 5)
                if header in (b"\r\n", b"\n", b""):
                    break
            parsed = urlsplit(parts[1] if len(parts) > 1 else "")
            query = parse_qs(parsed.query)
            if not parts or parts[0] != "GET":
                status = "405 Method Not Allowed"
            elif parsed.path != "/callback":
                status = "404 Not Found"
            elif self._settled:
                status, message = "410 Gone", "此次登录已结束，请返回应用查看状态。"
            elif not secrets.compare_digest((query.get("state") or [""])[0], self.state):
                message = "安全校验失败，请返回应用重新登录。"
            elif query.get("error"):
                self._settle({"error": "授权被拒绝或已取消，请重新登录"})
            elif not (query.get("code") or [""])[0]:
                message = "未收到授权码，请返回应用重新登录。"
            else:
                self._settle({"code": query["code"][0], "state": self.state})
                status = "303 See Other"
                location = (self.success_base.rstrip("/") +
                            "/api/oauth/login-success?client=desktop&status=success")
            body = message.encode("utf-8") if not location else b""
            response = f"HTTP/1.1 {status}\r\nContent-Type: text/plain; charset=utf-8\r\nCache-Control: no-store\r\n"
            if location:
                response += f"Location: {location}\r\n"
            writer.write((response + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode("ascii") + body)
            await writer.drain()
        except (OSError, ValueError, asyncio.TimeoutError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def wait_result(self):
        try:
            await asyncio.wait_for(self._event.wait(), self._timeout / 1000)
        except asyncio.TimeoutError:
            self._settle({"error": "登录超时，请重试"})
        return self._result

    async def cancel(self):
        self._settle(None)

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None


async def start_callback_server(timeout_ms=DEFAULT_TIMEOUT_MS):
    return await CallbackServer(timeout_ms).start()
