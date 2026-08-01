"""WebSocket 实时通道 + 连接管理（v2 协议）。

服务端 → 客户端事件见 packages/shared（turn/agent/thinking/token/tool/usage/approval/error 等）。
客户端 → 服务端：approval.response / terminal.input / browser.command / cancel。
"""
import asyncio
import json
import logging
import time
from collections import defaultdict

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

logger = logging.getLogger(__name__)

ws_router = APIRouter()


class ConnectionManager:
    """管理每个 session 的 WebSocket 连接。"""

    def __init__(self) -> None:
        self._conns: dict[int, set[WebSocket]] = defaultdict(set)

    async def connect(self, session_id: int, ws: WebSocket) -> None:
        await ws.accept()
        self._conns[session_id].add(ws)

    def disconnect(self, session_id: int, ws: WebSocket) -> None:
        self._conns[session_id].discard(ws)

    async def broadcast(self, session_id: int, event: dict) -> None:
        dead: list[WebSocket] = []
        for ws in self._conns.get(session_id, set()):
            try:
                await asyncio.wait_for(
                    ws.send_text(json.dumps(event, ensure_ascii=False)), timeout=5.0,
                )
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._conns[session_id].discard(ws)


manager = ConnectionManager()


@ws_router.websocket("/ws/sessions/{session_id}")
async def ws_endpoint(ws: WebSocket, session_id: int) -> None:
    await manager.connect(session_id, ws)
    try:
        while True:
            raw = await ws.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_text(json.dumps({"event": "error", "payload": {"code": "bad_json", "message": "invalid json"}}))
                continue

            event = data.get("event")
            payload = data.get("payload", {})

            if event == "approval.response":
                approval_id = payload.get("approval_id")
                approved = bool(payload.get("approved"))
                resolved = False
                if approval_id:
                    from app.orchestration.approval import approval_manager
                    resolved = approval_manager.resolve(approval_id, approved)
                await ws.send_text(json.dumps({
                    "event": "ack", "payload": {"ref": approval_id, "resolved": resolved},
                }))
                await manager.broadcast(session_id, {"event": "approval.response", "payload": payload})

            elif event == "cancel":
                turn_id = payload.get("turn_id")
                if turn_id:
                    # 延迟导入，避免 ws <-> engine <-> agent_loop 顶层循环导入
                    from app.orchestration import engine
                    ok = await engine.cancel_turn(int(turn_id))
                    await ws.send_text(json.dumps({"event": "ack", "payload": {"ref": f"cancel:{turn_id}", "ok": ok}}))

            elif event in ("terminal.input", "browser.command"):
                # 转发给同会话其他客户端（面板与 agent 协作预留）
                await manager.broadcast(session_id, data)

    except WebSocketDisconnect:
        manager.disconnect(session_id, ws)
