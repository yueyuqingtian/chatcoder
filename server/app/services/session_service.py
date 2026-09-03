"""会话 CRUD（v2：项目下多会话，支持 fork/重命名/置顶/归档）。"""
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.message import Message, Session
from app.persistence.models.turn import Turn


async def create_session(db: AsyncSession, *, project_id: int, title: str | None = None,
                         model_id: int | None = None, fork_parent_id: int | None = None,
                         permission_mode: str | None = None,
                         goal_text: str | None = None) -> Session:
    from datetime import datetime, timezone

    session = Session(
        project_id=project_id, title=title or None,
        model_id=model_id, fork_parent_id=fork_parent_id,
        # plan-547: 首页所选模式随创建落库，会话输入框立即显示与实际运行一致
        permission_mode=permission_mode or "default",
        # plan-676: 首页目标随创建一次落准（对齐 set_goal 端点写法；空时维持默认 none）
        **(
            {
                "goal_text": goal_text.strip()[:2000],
                "goal_status": "active",
                "goal_created_at": datetime.now(timezone.utc).isoformat(),
            }
            if goal_text and goal_text.strip() else {}
        ),
    )
    db.add(session)
    await db.flush()
    return session


async def get_session(db: AsyncSession, session_id: int) -> Session | None:
    return await db.get(Session, session_id)


async def auto_title_session(db: AsyncSession, session: Session, first_text: str) -> str | None:
    """为尚未命名的会话生成首条用户消息标题。"""
    if session.title:
        return None
    title = first_text.strip().replace("\n", " ")[:30]
    if not title:
        return None
    session.title = title
    await db.flush()
    return title


async def list_sessions(db: AsyncSession, project_id: int | None = None,
                        include_archived: bool = False) -> list[Session]:
    stmt = select(Session)
    if project_id is not None:
        stmt = stmt.where(Session.project_id == project_id)
    if not include_archived:
        stmt = stmt.where(Session.status != "archived")
    res = await db.execute(stmt.order_by(Session.pinned.desc(), Session.updated_at.desc()))
    return list(res.scalars().all())


async def update_session(db: AsyncSession, session_id: int, **kwargs) -> Session | None:
    session = await db.get(Session, session_id)
    if session is None:
        return None
    for k, v in kwargs.items():
        if v is not None:
            setattr(session, k, v)
    await db.flush()
    return session


async def fork_session(db: AsyncSession, session_id: int, title: str | None = None) -> Session:
    """复制会话（仅复制元数据与消息，任务/子代理不复制）。"""
    src = await db.get(Session, session_id)
    if src is None:
        raise ValueError("session not found")
    new_session = Session(
        project_id=src.project_id,
        title=title or f"{src.title or '会话'} · 分支",
        model_id=src.model_id,
        fork_parent_id=session_id,
        status="active",
    )
    db.add(new_session)
    await db.flush()
    # 复制消息
    from app.persistence.models.message import Message
    res = await db.execute(
        select(Message).where(Message.session_id == session_id, Message.deleted == False)  # noqa: E712
    )
    for m in res.scalars().all():
        db.add(Message(
            session_id=new_session.id,
            turn_id=None,
            thread_id=None,
            sender_type=m.sender_type,
            sender_id=m.sender_id,
            msg_type=m.msg_type,
            content=m.content,
            token_usage=m.token_usage,
        ))
    await db.flush()
    return new_session


async def create_system_message(db: AsyncSession, *, session_id: int, content: dict) -> None:
    """v2.2 (对齐 zcode 3.11): 写一条系统消息（模型切换 divider 等）。"""
    from app.core.enums import MsgType, SenderType

    from app.persistence.models.message import Message
    db.add(Message(
        session_id=session_id, turn_id=None, thread_id=None,
        sender_type=SenderType.SYSTEM.value, sender_id=None,
        msg_type=MsgType.SYSTEM.value, content=content,
    ))
    await db.flush()


async def list_main_messages(db: AsyncSession, session_id: int, limit: int | None = None) -> list[Message]:
    """主线程消息（thread_id IS NULL），默认过滤已回滚软删消息（问题14）。

    返回最近 limit 条，保持时间正序（最旧在前）；供全局摘要拼接。
    """
    stmt = (
        select(Message)
        .where(
            Message.session_id == session_id,
            Message.thread_id.is_(None),
            Message.deleted == False,  # noqa: E712
        )
        .order_by(Message.id.desc())
    )
    if limit:
        stmt = stmt.limit(limit)
    res = await db.execute(stmt)
    rows = list(res.scalars().all())
    rows.reverse()  # 恢复时间正序
    return rows


async def list_thread_messages(db: AsyncSession, session_id: int, thread_id: int,
                               limit: int | None = None) -> list[Message]:
    """指定子代理线程消息，默认过滤已回滚软删消息（问题14）。

    保持时间正序（最旧在前），供 build_thread_context_with_window 的 token 预算择优。
    """
    stmt = (
        select(Message)
        .where(
            Message.session_id == session_id,
            Message.thread_id == thread_id,
            Message.deleted == False,  # noqa: E712
        )
        .order_by(Message.id.asc())
    )
    if limit:
        stmt = stmt.limit(limit)
    res = await db.execute(stmt)
    return list(res.scalars().all())


async def last_activity_at(db: AsyncSession, session_id: int) -> str | None:
    """最近一条未删除消息时间；无消息回退会话创建时间。"""
    res = await db.execute(
        select(func.max(Message.created_at)).where(
            Message.session_id == session_id,
            Message.deleted.is_(False),
        )
    )
    ts = res.scalar_one_or_none()
    if ts is None:
        s = await db.get(Session, session_id)
        ts = s.created_at if s else None
    return ts


async def has_running_turn(db: AsyncSession, session_id: int) -> bool:
    """会话是否存在运行中的 turn。"""
    res = await db.execute(
        select(Turn.id).where(
            Turn.session_id == session_id,
            Turn.status == "running",
        ).limit(1)
    )
    return res.scalars().first() is not None


async def has_interrupted_turn(db: AsyncSession, session_id: int) -> bool:
    res = await db.execute(
        select(Turn.id).where(
            Turn.session_id == session_id,
            Turn.status == "interrupted",
        ).limit(1)
    )
    return res.scalars().first() is not None
