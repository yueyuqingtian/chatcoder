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
    await broadcast(session_id, {"event": "turn.updated", "payload": {"turn_id": turn_id, "status": status}})
