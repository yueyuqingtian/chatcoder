"""WebSocket 实时通道 + 连接管理（v2 协议）。

服务端 → 客户端事件见 packages/shared（turn/agent/thinking/token/tool/usage/approval/error 等）。
客户端 → 服务端：approval.response / terminal.input / browser.command / cancel / sync.request。

v2.1（对齐 zcode 方案 3.2）：每条广播事件带会话级单调 seq + 环形缓冲区，
客户端重连后发 sync.request(last_seq) 补发断线期间丢失的事件。
"""
import asyncio
import json
import logging
import time
from collections import defaultdict, deque

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

logger = logging.getLogger(__name__)

ws_router = APIRouter()

# 断线补偿缓冲区容量（per session）
_EVENT_BUFFER_SIZE = 500


class ConnectionManager:
    """管理每个 session 的 WebSocket 连接。"""

    def __init__(self) -> None:
        self._conns: dict[int, set[WebSocket]] = defaultdict(set)
        # v2.1: 会话级事件序号计数器（内存，进程重启归零——重启后客户端重建状态即可）
        self._seqs: dict[int, int] = defaultdict(int)
        # v2.1: 会话级事件环形缓冲区（断线补偿补发源）
        self._buffers: dict[int, deque] = defaultdict(lambda: deque(maxlen=_EVENT_BUFFER_SIZE))

    async def connect(self, session_id: int, ws: WebSocket) -> None:
        await ws.accept()
        self._conns[session_id].add(ws)

    def disconnect(self, session_id: int, ws: WebSocket) -> None:
        self._conns[session_id].discard(ws)
        if not self._conns[session_id]:
            self._conns.pop(session_id, None)

    def next_seq(self, session_id: int) -> int:
        self._seqs[session_id] += 1
        return self._seqs[session_id]

    async def broadcast(self, session_id: int, event: dict) -> None:
        # v2.1.1: 事件契约校验——事件名必须登记在 schemas 镜像表；
        # payload 宽松校验失败仅告警（不阻断广播，避免影响主流程）。
        from app.gateway.schemas import WS_EVENT_PAYLOAD_MODELS, WsEventPayload
        _name = event.get("event")
        _model = WS_EVENT_PAYLOAD_MODELS.get(_name)
        if _model is None:
            logger.warning("[ws] 未登记的事件名被广播: %r", _name)
        else:
            try:
                _model.model_validate(event.get("payload") or {})
            except Exception:
                logger.warning("[ws] 事件 %s payload 校验失败: %s", _name, exc_info=True)
        # v2.1: 注入会话级单调 seq 并写入补偿缓冲区（仅对可重放事件计数）
        if "seq" not in event:
            event["seq"] = self.next_seq(session_id)
            self._buffers[session_id].append(event)
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

    def replay_since(self, session_id: int, last_seq: int) -> list[dict]:
        """取 seq > last_seq 的缓冲事件（断线补偿）。"""
        return [ev for ev in self._buffers[session_id] if ev.get("seq", 0) > last_seq]


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
                    # v2.2: answer 为 ask_user_question 的结构化回答
                    _answer = payload.get("answer")
                    if isinstance(_answer, dict):
                        resolved = approval_manager.resolve(approval_id, approved, answer=_answer)
                    else:
                        resolved = approval_manager.resolve(approval_id, approved)
                    # v2.2 (对齐 zcode 3.12): 审批卡"始终允许"→ 生成工具级 exec_policy 规则
                    if approved and payload.get("remember"):
                        await _remember_approval(approval_id, session_id)
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

            elif event == "sync.request":
                # v2.1: 断线补偿——重放 seq > last_seq 的缓冲事件
                last_seq = int(payload.get("last_seq") or 0)
                missed = manager.replay_since(session_id, last_seq)
                for ev in missed:
                    await ws.send_text(json.dumps(ev, ensure_ascii=False))
                await ws.send_text(json.dumps({
                    "event": "sync.response",
                    "seq": 0,  # 不占序号：纯确认帧
                    "payload": {"last_seq": last_seq, "count": len(missed)},
                }))

            elif event in ("terminal.input", "browser.command"):
                # 转发给同会话其他客户端（面板与 agent 协作预留）
                await manager.broadcast(session_id, data)

    except WebSocketDisconnect:
        manager.disconnect(session_id, ws)


async def _remember_approval(approval_id: str, session_id: int) -> None:
    """v2.2 (对齐 zcode 3.12): 审批卡"始终允许"→ 生成工具级 exec_policy 规则。

    从 approval_manager 取 pending 的 detail（含 tool/session_id），
    写 allow 规则（session 级）。失败仅告警不阻断。
    """
    try:
        from app.orchestration.approval import approval_manager
        from app.persistence.database import async_session_factory
        from app.services import exec_policy_service

        detail = approval_manager.get_detail(approval_id)
        if not detail:
            return
        tool_name = detail.get("tool", "")
        if not tool_name:
            return
        async with async_session_factory() as db:
            await exec_policy_service.create_rule(
                db, command_pattern=f"(tool){tool_name}", decision="allow",
                session_id=detail.get("session_id") or session_id,
                justification="审批卡“始终允许”自动生成",
                tool_name=tool_name,
            )
            await db.commit()
        logger.info("[ws] 审批始终允许已生成规则: tool=%s", tool_name)
    except Exception:
        logger.warning("[ws] 始终允许规则生成失败(非阻塞)", exc_info=True)
