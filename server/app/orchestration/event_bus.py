"""v1.0: Agent 间事件总线 — session 级 asyncio.Queue 实现。

支持:
- publish(session_id, event): 发布事件到 session 总线
- poll(session_id, agent_id): 非阻塞获取指定 agent 的新消息
- subscribe(session_id, agent_id): 订阅（创建 queue）
- unsubscribe(session_id, agent_id): 取消订阅

事件类型:
- AgentEvent(kind, sender_id, sender_name, data)
  kind: "info" | "warning" | "handoff" | "question"
"""
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class AgentEvent:
    """Agent 间通信事件。"""
    kind: str  # info / warning / handoff / question
    sender_id: int
    sender_name: str
    data: dict[str, Any] = field(default_factory=dict)
    target_agent_id: int | None = None  # None = 广播给所有 agent


class SessionEventBus:
    """单个 session 的事件总线。"""

    def __init__(self, session_id: int) -> None:
        self.session_id = session_id
        # agent_id -> Queue
        self._subscribers: dict[int, asyncio.Queue[AgentEvent]] = {}

    def subscribe(self, agent_id: int) -> None:
        if agent_id not in self._subscribers:
            self._subscribers[agent_id] = asyncio.Queue(maxsize=50)

    def unsubscribe(self, agent_id: int) -> None:
        self._subscribers.pop(agent_id, None)

    async def publish(self, event: AgentEvent) -> None:
        """发布事件。若 target_agent_id 指定则单播，否则广播。"""
        if event.target_agent_id is not None:
            q = self._subscribers.get(event.target_agent_id)
            if q:
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    logger.warning("[EventBus] agent %d 队列满，丢弃事件", event.target_agent_id)
        else:
            # 广播给所有订阅者（排除发送者自己）
            for aid, q in self._subscribers.items():
                if aid == event.sender_id:
                    continue
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    pass

    def poll(self, agent_id: int) -> list[AgentEvent]:
        """非阻塞获取指定 agent 的所有待处理事件。"""
        q = self._subscribers.get(agent_id)
        if not q:
            return []
        events = []
        while not q.empty():
            try:
                events.append(q.get_nowait())
            except asyncio.QueueEmpty:
                break
        return events


# ── 全局注册表: session_id -> SessionEventBus ──
_buses: dict[int, SessionEventBus] = {}


def get_event_bus(session_id: int) -> SessionEventBus:
    """获取 session 级事件总线。"""
    if session_id not in _buses:
        _buses[session_id] = SessionEventBus(session_id)
    return _buses[session_id]


def cleanup_session_bus(session_id: int) -> None:
    """会话结束时清理总线。"""
    _buses.pop(session_id, None)
