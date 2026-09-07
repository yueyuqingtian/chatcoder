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
    buffered: bool = False,
) -> Message:
    """创建消息并广播 message.created（可选）。

    SQLite 并发写冲突（database is locked / 前一失败导致的 PendingRollbackError）
    是瞬时性的：回滚后短暂退避重试可显著降低「消息落库失败 → 整个 turn 中断」的概率。

    `buffered=True`：进入 write-behind 缓冲（热路径），落库与广播由 Flusher 批量完成——
    调用方不应依赖返回值的 id（未落库，id=None）；`turn_id` 必须非空，否则回退直写。
    依赖即时 id 的路径（如 turn 创建的用户消息）必须保持 buffered=False（直写）。
    """
    content = content or {}
    if buffered and turn_id is not None:
        from app.persistence.write_behind import WriteItem, write_behind
        buf = write_behind.get(session_id, turn_id)
        buf.enqueue(WriteItem("message", {
            "session_id": session_id, "turn_id": turn_id, "thread_id": thread_id,
            "sender_type": sender_type, "sender_id": sender_id, "msg_type": msg_type,
            "content": content, "token_usage": token_usage,
        }))
        return Message(
            session_id=session_id, turn_id=turn_id, thread_id=thread_id,
            sender_type=sender_type, sender_id=sender_id, msg_type=msg_type,
            content=content, token_usage=token_usage,
        )

    msg = Message(
        session_id=session_id, turn_id=turn_id, thread_id=thread_id,
        sender_type=sender_type, sender_id=sender_id,
        msg_type=msg_type, content=content, token_usage=token_usage,
    )
    # 直写路径：经 WriteEngine 单写线程（无锁单写者）；返回"展示对象"（id/created_at
    # 已在写线程内确定，属性可读；跨层不绑定 async 会话）。
    from app.persistence.database import run_write_locked

    def _persist(s):
        m = Message(
            session_id=session_id, turn_id=turn_id, thread_id=thread_id,
            sender_type=sender_type, sender_id=sender_id,
            msg_type=msg_type, content=content, token_usage=token_usage,
        )
        s.add(m)
        s.flush()
        mid = m.id
        created = str(m.created_at) if m.created_at else None
        s.commit()
        return mid, created

    try:
        mid, created = await run_write_locked(_persist, label="message.create")
    except Exception:
        logger.error("[message] 直写落库失败（无锁单写者）", exc_info=True)
        raise
    msg.id = mid
    msg.created_at = created
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


async def patch_message(message_id: int, **fields) -> None:
    """写引擎单写线程提交 Message 字段补丁（无锁单写者；如 turn_id 回填）。"""
    from app.persistence.database import run_write_locked

    def patch(s):
        m = s.get(Message, message_id)
        if m is None:
            return
        for k, v in fields.items():
            setattr(m, k, v)
        s.commit()

    await run_write_locked(patch, label=f"message.patch.{message_id}")
