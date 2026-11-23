"""antigravity 独立 OAuth 登录的本地回调服务（对齐 ta3 CallbackServer 范式）。

- 只用回环地址 + 随机端口（port=0 由系统分配），不写死端口避免占用冲突；
- state 用 secrets.compare_digest 恒定时间比较，防 CSRF；
- 同一实例只接受一次有效回调（settled 后回 410），避免重放。
"""
from __future__ import annotations

import asyncio
import secrets
from urllib.parse import parse_qs, urlsplit

DEFAULT_TIMEOUT_MS = 120_000


class CallbackServer:
    def __init__(self, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        self.state = secrets.token_urlsafe(32)
        self.port = 0
        self._timeout = timeout_ms
        self._server = None
        self._event = asyncio.Event()
        self._result: dict | None = None
        self._settled = False

    @property
    def callback_base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def redirect_uri(self) -> str:
        return self.callback_base_url + "/callback"

    async def start(self) -> "CallbackServer":
        # 不同地址族必须共用同一端口，避免 localhost 解析到未监听端口
        self._server = await asyncio.start_server(self._handle, host="127.0.0.1", port=0)
        sockets = self._server.sockets or []
        self.port = sockets[0].getsockname()[1]
        if any(sock.getsockname()[1] != self.port for sock in sockets):
            await self.stop()
            self._server = await asyncio.start_server(
                self._handle, host="127.0.0.1", port=self.port,
            )
        return self

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:  # noqa: BLE001 - 关闭失败不影响主流程
                pass
            self._server = None

    async def wait(self, timeout_ms: int | None = None) -> dict | None:
        """等待回调；超时返回 None（调用方据此提示用户重试）。"""
        try:
            await asyncio.wait_for(
                self._event.wait(), timeout=(timeout_ms or self._timeout) / 1000,
            )
        except asyncio.TimeoutError:
            return None
        return self._result

    def cancel(self) -> None:
        """取消登录（用户主动放弃或发起新登录）。"""
        self._settle({"error": "登录已取消"})

    def _settle(self, result: dict) -> None:
        if not self._settled:
            self._settled = True
            self._result = result
            self._event.set()

    async def _handle(self, reader, writer) -> None:
        status, message = "400 Bad Request", "无效的登录回调，请返回应用重试。"
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
                status, message = "200 OK", "授权未完成，请返回应用重试。"
            elif not (query.get("code") or [""])[0]:
                message = "未收到授权码，请返回应用重新登录。"
            else:
                self._settle({"code": query["code"][0], "state": self.state})
                status, message = "200 OK", "登录完成，可关闭此页面返回应用。"
            body = message.encode("utf-8")
            head = (
                f"HTTP/1.1 {status}\r\nContent-Type: text/plain; charset=utf-8\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
            ).encode("latin-1")
            writer.write(head + body)
            await writer.drain()
        except Exception:  # noqa: BLE001 - 回调异常不应影响服务
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
