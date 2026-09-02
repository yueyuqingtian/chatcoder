"""WS 事件广播辅助（v2）。"""
import logging

logger = logging.getLogger(__name__)


async def broadcast(session_id: int, event: dict) -> None:
    """向会话广播事件，失败静默。"""
    try:
        from app.gateway.ws import manager as ws_manager
        await ws_manager.broadcast(session_id, event)
    except Exception:
        logger.debug("事件广播失败(可能无连接): %s", event.get("event"), exc_info=True)


async def broadcast_agent_updated(session_id: int, agent_id: int, status: str,
                                  tool: str | None = None, step: int | None = None) -> None:
    payload: dict = {"agent_id": agent_id, "status": status}
    if tool:
        payload["tool"] = tool
    if step is not None:
        payload["step"] = step
    await broadcast(session_id, {"event": "agent.updated", "payload": payload})


async def broadcast_turn_updated(session_id: int, turn_id: int, status: str) -> None:
    await broadcast(session_id, {
        "event": "turn.updated",
        # v37: 携带 session_id——全局通道转发后前端据此定位会话，
        # 不再依赖「当前聚焦会话」（后台会话的迟到事件会误清当前会话状态）。
        "payload": {"turn_id": turn_id, "status": status, "session_id": session_id},
    })


async def broadcast_session_completed(session_id: int, db=None) -> None:
    """v37: 广播会话完成——摘除侧栏运行标记并同步最新活动时间。

    last_activity_at 由调用方传入的 db 现算（无 db 时为 None）：
    侧栏「按最近活动」排序依赖该值，此前只在整表刷新时才更新，
    表现为「后台会话结束也不上移」。
    """
    last_activity: str | None = None
    if db is not None:
        try:
            from app.services import session_service
            last_activity = await session_service.last_activity_at(db, session_id)
        except Exception:
            logger.debug("[events] last_activity_at 读取失败(非阻塞)", exc_info=True)
    await broadcast(session_id, {
        "event": "session.completed",
        "payload": {"session_id": session_id, "last_activity_at": last_activity},
    })
