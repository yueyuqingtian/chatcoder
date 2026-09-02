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

# v37: 全局通道转发的事件白名单——仅转发「跨会话可见」的状态类事件。
# 高频流式事件（token/thinking/tool）绝不转发，避免全局连接被长任务淹没。
_GLOBAL_FORWARD_EVENTS = frozenset({
    "session.completed",
    "session.updated",
    "turn.completed",
    "turn.updated",
    "message.created",
})


class ConnectionManager:
    """管理每个 session 的 WebSocket 连接。"""

    def __init__(self) -> None:
        self._conns: dict[int, set[WebSocket]] = defaultdict(set)
        # v37: 全局连接（不分会话）——侧栏需要感知「非当前会话」的运行状态与时间
        self._global_conns: set[WebSocket] = set()
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

    def connect_global(self, ws: WebSocket) -> None:
        """v37: 登记全局连接（侧栏跨会话状态通道）。"""
        self._global_conns.add(ws)

    def disconnect_global(self, ws: WebSocket) -> None:
        self._global_conns.discard(ws)

    async def broadcast_global(self, event: dict) -> None:
        """v37: 向全部全局连接广播；发送失败的连接直接剔除。

        失败静默——全局通道只服务侧栏展示，不得影响业务主流程。
        """
        if not self._global_conns:
            return
        try:
            raw = json.dumps(event, ensure_ascii=False)
        except (TypeError, ValueError):
            logger.warning("[ws] 全局事件序列化失败: %r", event.get("event"))
            return
        dead: list[WebSocket] = []
        for ws in list(self._global_conns):
            try:
                await asyncio.wait_for(ws.send_text(raw), timeout=5.0)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._global_conns.discard(ws)

    async def _forward_global(self, session_id: int, event: dict) -> None:
        """v37: 白名单事件转发到全局通道，payload 注入 session_id 供前端定位会话。"""
        if event.get("event") not in _GLOBAL_FORWARD_EVENTS:
            return
        payload = event.get("payload")
        forwarded = {
            "event": event["event"],
            "payload": {**(payload if isinstance(payload, dict) else {}), "session_id": session_id},
        }
        try:
            await self.broadcast_global(forwarded)
        except Exception:
            logger.debug("[ws] 全局通道转发失败(非阻塞): %s", event.get("event"), exc_info=True)

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
        # v37: 白名单事件同步转发到全局通道（侧栏感知非当前会话的状态变化）
        await self._forward_global(session_id, event)

    def replay_since(self, session_id: int, last_seq: int) -> list[dict]:
        """取 seq > last_seq 的缓冲事件（断线补偿）。"""
        return [ev for ev in self._buffers[session_id] if ev.get("seq", 0) > last_seq]


manager = ConnectionManager()


@ws_router.websocket("/ws/global")
async def ws_global_endpoint(ws: WebSocket) -> None:
    """v37: 全局状态通道——侧向栏感知「非当前会话」的运行状态与最新活动时间。

    会话级通道只给聚焦会话派发事件，后台会话结束后侧栏无从得知，
    运行标记只能靠整表刷新修正。本通道转发白名单内的状态类事件
    （session.completed / session.updated / turn.* / message.created），
    payload 统一注入 session_id；不接收任何业务指令，仅维持心跳。
    """
    await ws.accept()
    manager.connect_global(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect_global(ws)
    except Exception:
        logger.debug("[ws] 全局连接异常关闭", exc_info=True)
        manager.disconnect_global(ws)


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
                    # v3.0 (plan-88): remember_scope 区分会话级/全局（global 规则 session_id=None）
                    if approved and payload.get("remember"):
                        scope = "global" if payload.get("remember_scope") == "global" else "session"
                        await _remember_approval(approval_id, session_id, scope)
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


async def _remember_approval(approval_id: str, session_id: int,
                             scope: str = "session") -> None:
    """v2.2 (对齐 zcode 3.12): 审批卡"始终允许"→ 生成工具级 exec_policy 规则。

    从 approval_manager 取 pending 的 detail（含 tool/session_id），
    写 allow 规则：scope=session → 会话级（session_id 绑定）；
    scope=global → 全局规则（session_id=None）。失败仅告警不阻断。
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
                session_id=None if scope == "global" else (detail.get("session_id") or session_id),
                justification="审批卡“始终允许”自动生成" if scope == "global" else "审批卡“当前会话允许”自动生成",
                tool_name=tool_name,
            )
            await db.commit()
        logger.info("[ws] 审批始终允许已生成规则: tool=%s scope=%s", tool_name, scope)
    except Exception:
        logger.warning("[ws] 始终允许规则生成失败(非阻塞)", exc_info=True)
