"""工作流写入冲突事件（设计 §5A.1 / §7.4）。

级别与处理阶段一一对应：
- L1 自动合并：同一文件的改动落在不同块，三方合并干净，直接完成，不打扰用户；
- L2 排队重做：硬冲突时当前写入不生效，节点基于最新内容重读后重做；
- L3 合并步骤：并入主工作区出现冲突，由合并节点解决；
- L4 用户决定：重做仍失败或合并失败，闸门三栏对照，由用户决定。

detected 在进入某一级别时发出，resolved 在该级别结束时发出；两者都携带 level 以及 path / paths。
事件与运行时的其它程序事件走同一条路径：先落事件日志，再推送给会话通道。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from app.orchestration.workflow import store

logger = logging.getLogger(__name__)

DETECTED = "workflow.conflict.detected"
RESOLVED = "workflow.conflict.resolved"

Broadcast = Callable[[int, dict[str, Any]], Awaitable[None]]


async def _default_broadcast(session_id: int, event: dict[str, Any]) -> None:
    from app.orchestration.agent_events import broadcast

    await broadcast(session_id, event)


async def emit(
    program_id: int,
    event_type: str,
    node_key: str | None,
    payload: dict[str, Any],
    *,
    broadcast: Broadcast | None = None,
) -> None:
    """落库并推送。失败只记日志：冲突事件只用于界面提示，不得影响写入与调度。"""
    try:
        emitted = await store.emit_event(program_id, event_type, node_key, payload)
        await (broadcast or _default_broadcast)(emitted["session_id"], emitted["envelope"])
    except Exception:
        logger.warning(
            "[workflow] 冲突事件发送失败 type=%s program=%s", event_type, program_id, exc_info=True
        )


async def emit_for_node(node_id: int, event_type: str, payload: dict[str, Any]) -> None:
    """守卫侧使用：由节点上下文解析所属程序与节点键。"""
    ctx = await store.load_node_context(node_id)
    if ctx is None:
        return
    await emit(int(ctx["program_id"]), event_type, str(ctx["node"]["key"]), payload)
