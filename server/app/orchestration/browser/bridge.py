"""桌面端浏览器宿主桥（plan-20261015-browser-agent）。

桌面端渲染进程连上 `/ws/global` 后发送 `browser.host.hello` 登记为浏览器宿主；
服务端经同一条连接下发 `browser.request`，宿主执行后回 `browser.response`。

为什么走全局通道而不是会话通道：会话通道只为「当前聚焦会话」建立，后台会话里的 AI
同样要能操作浏览器（用户在看别的会话时 AI 照常工作），而全局通道在应用存活期间常驻。

选宿主口径：最近一次登记且协议版本兼容的桌面端优先；没有可用宿主时由调用方回退到
Playwright 兜底宿主（见 host.py）。宿主断线时其名下在途请求立即失败，不等超时。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# 宿主协议版本：前端 host.ts 的 PROTOCOL 与之一致；不一致的宿主不被选用（回退兜底宿主）
PROTOCOL_VERSION = 1


class HostUnavailable(RuntimeError):
    """没有可用的桌面端宿主（或请求途中宿主断开）。"""


class HostCallError(RuntimeError):
    """宿主执行失败（动作语义层面的失败，如元素不存在、超时）。"""


@dataclass
class HostConn:
    ws: Any
    host_id: str
    version: int
    capabilities: list[str]
    connected_at: float
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class BrowserBridge:
    def __init__(self) -> None:
        self._hosts: dict[int, HostConn] = {}
        # request_id -> (future, 发往的宿主 key)
        self._pending: dict[str, tuple[asyncio.Future, int]] = {}

    # ── 宿主登记 ──────────────────────────────────────────
    async def register(self, ws: Any, payload: dict) -> HostConn:
        from app.orchestration.browser.kit import KIT_SOURCE, KIT_VERSION

        conn = HostConn(
            ws=ws,
            host_id=str(payload.get("host_id") or uuid.uuid4().hex[:8]),
            version=int(payload.get("protocol") or 0),
            capabilities=[str(c) for c in (payload.get("capabilities") or [])],
            connected_at=time.monotonic(),
        )
        self._hosts[id(ws)] = conn
        logger.info("[browser] 桌面端宿主登记 id=%s protocol=%s", conn.host_id, conn.version)
        await self._send(conn, {
            "event": "browser.host.welcome",
            "payload": {"protocol": PROTOCOL_VERSION, "kit_version": KIT_VERSION, "kit_source": KIT_SOURCE},
        })
        return conn

    def unregister(self, ws: Any) -> None:
        conn = self._hosts.pop(id(ws), None)
        if conn is None:
            return
        logger.info("[browser] 桌面端宿主断开 id=%s", conn.host_id)
        for rid, (fut, key) in list(self._pending.items()):
            if key == id(ws) and not fut.done():
                fut.set_exception(HostUnavailable("桌面端连接已断开"))
                self._pending.pop(rid, None)

    def active(self) -> HostConn | None:
        cands = [c for c in self._hosts.values() if c.version == PROTOCOL_VERSION]
        if not cands:
            return None
        return max(cands, key=lambda c: c.connected_at)

    def has_host(self) -> bool:
        return self.active() is not None

    # ── 请求 / 响应 ───────────────────────────────────────
    async def request(self, session_id: int, op: str, args: dict, timeout: float,
                      cancel_event: asyncio.Event | None = None) -> dict:
        conn = self.active()
        if conn is None:
            raise HostUnavailable("没有已连接的桌面端浏览器")
        rid = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[rid] = (fut, id(conn.ws))
        try:
            await self._send(conn, {
                "event": "browser.request",
                "payload": {"request_id": rid, "session_id": session_id, "op": op, "args": args},
            })
        except Exception as e:  # noqa: BLE001
            self._pending.pop(rid, None)
            self.unregister(conn.ws)
            raise HostUnavailable(f"发送到桌面端失败：{e}") from e
        waiters: set[asyncio.Future] = {fut}
        cancel_task = None
        if cancel_event is not None:
            cancel_task = asyncio.ensure_future(cancel_event.wait())
            waiters.add(cancel_task)
        try:
            done, _ = await asyncio.wait(waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            if fut in done:
                return fut.result()
            if cancel_task is not None and cancel_task in done:
                raise asyncio.CancelledError()
            raise HostCallError(f"浏览器操作超时（{int(timeout)} 秒）")
        finally:
            self._pending.pop(rid, None)
            if cancel_task is not None:
                cancel_task.cancel()

    def resolve(self, payload: dict) -> None:
        rid = str(payload.get("request_id") or "")
        entry = self._pending.get(rid)
        if not entry:
            return
        fut, _ = entry
        if fut.done():
            return
        if payload.get("ok"):
            result = payload.get("result")
            fut.set_result(result if isinstance(result, dict) else {"value": result})
        else:
            fut.set_exception(HostCallError(str(payload.get("error") or "浏览器操作失败")))

    async def _send(self, conn: HostConn, event: dict) -> None:
        raw = json.dumps(event, ensure_ascii=False)
        async with conn.send_lock:
            await asyncio.wait_for(conn.ws.send_text(raw), timeout=10.0)


bridge = BrowserBridge()
