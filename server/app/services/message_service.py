"""消息创建/列表服务（v2，含 WS 广播）。"""
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import MsgType, SenderType
from app.gateway.schemas import MessageOut
from app.persistence.models.message import Message

logger = logging.getLogger(__name__)


def _to_out(m: Message) -> MessageOut:
    return MessageOut(
        id=m.id, session_id=m.session_id, turn_id=m.turn_id, thread_id=m.thread_id,
        sender_type=m.sender_type, sender_id=m.sender_id, msg_type=m.msg_type,
        content=m.content, token_usage=m.token_usage,
        created_at=str(m.created_at) if m.created_at else None,
    )


async def create_message(
    db: AsyncSession, *, session_id: int,
    sender_type: str = SenderType.SYSTEM.value,
    sender_id: int | None = None,
    msg_type: str = MsgType.TEXT.value,
    content: dict | None = None,
    turn_id: int | None = None,
    thread_id: int | None = None,
    token_usage: int = 0,
    broadcast: bool = True,
) -> Message:
    """创建消息并广播 message.created（可选）。"""
    msg = Message(
        session_id=session_id, turn_id=turn_id, thread_id=thread_id,
        sender_type=sender_type, sender_id=sender_id,
        msg_type=msg_type, content=content or {}, token_usage=token_usage,
    )
    db.add(msg)
    await db.flush()
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    if broadcast:
        try:
            from app.gateway.ws import manager as ws_manager
            await ws_manager.broadcast(session_id, {
                "event": "message.created",
                "payload": {"msg": _to_out(msg).model_dump()},
            })
        except Exception:
            logger.debug("message.created 广播失败(可能无连接)", exc_info=True)
    return msg


async def list_messages(
    db: AsyncSession, session_id: int, thread_id: int | None = None,
    include_deleted: bool = False, limit: int | None = None,
) -> list[Message]:
    """列出会话消息（默认过滤已回滚软删消息）。"""
    stmt = select(Message).where(Message.session_id == session_id)
    if thread_id is not None:
        stmt = stmt.where(Message.thread_id == thread_id)
    elif thread_id is None:
        # 仅主线程消息需显式排除子代理线程；None 参数 = 不限
        pass
    if not include_deleted:
        stmt = stmt.where(Message.deleted == False)  # noqa: E712
    stmt = stmt.order_by(Message.id.asc())
    if limit:
        stmt = stmt.limit(limit)
    res = await db.execute(stmt)
    return list(res.scalars().all())


async def get_message(db: AsyncSession, message_id: int) -> Message | None:
    return await db.get(Message, message_id)
