"""消息创建/列表服务（v2，含 WS 广播）。"""
import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.exc import OperationalError, PendingRollbackError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import MsgType, SenderType
from app.gateway.schemas import MessageOut
from app.persistence.models.message import Message

logger = logging.getLogger(__name__)

# 消息落库重试次数（SQLite database is locked 为瞬时写竞争，重试通常一次即成功）
_MESSAGE_WRITE_RETRIES = 4


def _is_db_lock_error(exc: BaseException) -> bool:
    """是否 SQLite 写锁冲突类错误（应退避重试而非直接失败）。"""
    if isinstance(exc, PendingRollbackError):
        return True
    if isinstance(exc, OperationalError):
        msg = str(getattr(exc, "orig", None) or exc).lower()
        return "locked" in msg
    return False


def enrich_content_abs_path(content: dict | None) -> dict | None:
    """问题15: 为消息附件增加服务器绝对路径 abs_path。

    附件 path 是相对路径（{file_id}/{filename}），复制到新上下文后 AI 无法定位；
    补上 abs_path（基于 settings.uploads_dir 的绝对地址），前端复制与 read_attachment 均可使用。
    深拷贝避免污染 ORM 对象。read_attachment 保持相对/绝对兼容。
    """
    if not isinstance(content, dict):
        return content
    atts = content.get("attachments")
    if not isinstance(atts, list):
        return content
    from pathlib import Path
    from app.core.config import settings
    try:
        uploads_root = Path(settings.uploads_dir).resolve()
    except (OSError, ValueError):
        return content
    new_atts: list = []
    for a in atts:
        if not isinstance(a, dict):
            new_atts.append(a)
            continue
        na = dict(a)
        p = na.get("path")
        if p and not na.get("abs_path"):
            try:
                na["abs_path"] = str(uploads_root / str(p))
            except Exception:
                pass
        new_atts.append(na)
    out = dict(content)
    out["attachments"] = new_atts
    return out


def _to_out(m: Message) -> MessageOut:
    return MessageOut(
        id=m.id, session_id=m.session_id, turn_id=m.turn_id, thread_id=m.thread_id,
        sender_type=m.sender_type, sender_id=m.sender_id, msg_type=m.msg_type,
        content=enrich_content_abs_path(m.content), token_usage=m.token_usage,
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
    """创建消息并广播 message.created（可选）。

    SQLite 并发写冲突（database is locked / 前一失败导致的 PendingRollbackError）
    是瞬时性的：回滚后短暂退避重试可显著降低「消息落库失败 → 整个 turn 中断」的概率。
    """
    msg = Message(
        session_id=session_id, turn_id=turn_id, thread_id=thread_id,
        sender_type=sender_type, sender_id=sender_id,
        msg_type=msg_type, content=content or {}, token_usage=token_usage,
    )
    _tries_used = 1
    for _attempt in range(_MESSAGE_WRITE_RETRIES):
        _tries_used = _attempt + 1
        try:
            db.add(msg)
            await db.flush()
            await db.commit()
            break
        except Exception as exc:  # noqa: BLE001
            await db.rollback()  # 无论何种异常先恢复 session，避免残留 rollback-only 状态
            if _attempt >= _MESSAGE_WRITE_RETRIES - 1 or not _is_db_lock_error(exc):
                raise
            # 锁竞争通常为毫秒级，退避后重试（同一 msg 实例 rollback 后回到 transient 可再次 add）
            await asyncio.sleep(0.1 * (1 << _attempt))
    if _tries_used > 1:
        logger.debug("message 落库重试 %d 次后成功", _tries_used)
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
