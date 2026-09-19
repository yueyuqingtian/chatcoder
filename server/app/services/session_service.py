"""会话 CRUD（v2：项目下多会话，支持 fork/重命名/置顶/归档）。

plan-206-975：写操作经 WriteEngine 单写线程（无锁单写者）；读保留 async 会话。
写函数返回标量（id/title/status 等），不返回跨层 ORM 对象。
"""
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.message import Message, Session
from app.persistence.models.turn import Turn


def _legacy_default_for_type(type_name: str) -> object:
    """按列类型推断兜底默认值（仅为兼容旧库遗留 NOT NULL 列的显式插入路径）。"""
    t = (type_name or "").upper()
    if any(k in t for k in ("INT", "BOOL", "REAL", "FLOAT", "DOUBLE", "NUMERIC", "DECIMAL")):
        return 0
    return ""


async def create_session(db: AsyncSession, *, project_id: int, title: str | None = None,
                         model_id: int | None = None, fork_parent_id: int | None = None,
                         permission_mode: str | None = None,
                         goal_text: str | None = None) -> int:
    from datetime import datetime, timezone

    from app.persistence.database import run_write_locked

    def patch(s):
        # plan-278-1391: 旧库可能残留「模型已删除但 NOT NULL 无默认值」的列
        # （典型：sessions.plan_restore_after_turn）。此类列不在 ORM 映射中，
        # SQLAlchemy 生成的 INSERT 不带它 → NOT NULL 直接失败。启动迁移多数情况下
        # 已删列/补默认值，这里作为最后一道保险：显式列出遗留列并填默认值。
        from app.persistence.migrations import legacy_notnull_defaults
        legacy = legacy_notnull_defaults("sessions")
        goal_active = bool(goal_text and goal_text.strip())
        # 只登记「明确有值」的字段；其余交给下方补齐逻辑（NOT NULL 列填默认值，
        # 可空列保持 NULL）。避免 goal_status 等 NOT NULL 列被显式写成 NULL。
        values: dict[str, object] = {
            "project_id": project_id,
            # plan-547: 首页所选模式随创建落库，会话输入框立即显示与实际运行一致
            "permission_mode": permission_mode or "default",
        }
        for name, val in (
            ("title", title),
            ("model_id", model_id),
            ("fork_parent_id", fork_parent_id),
            # plan-676: 首页目标随创建一次落准（对齐 set_goal 端点写法；空时维持默认 none）
            ("goal_text", goal_text.strip()[:2000] if goal_active else None),
            ("goal_status", "active" if goal_active else None),
            ("goal_created_at", datetime.now(timezone.utc).isoformat() if goal_active else None),
        ):
            if val is not None:
                values[name] = val
        if legacy:
            # 显式插入（含遗留列），绕开 ORM 的 INSERT 列裁剪。
            # 关键：显式 INSERT 不再经过 ORM 的 Python 侧默认值填充，必须自行补齐
            # 所有「NOT NULL 且无 server_default」的映射列（status/pinned/
            # goal_status/last_prompt_tokens/goal_turns_used 等），否则会以 NULL 失败。
            values.update(legacy)
            for col in Session.__table__.columns:
                name = col.name
                if name in values or col.server_default is not None or col.nullable:
                    continue
                # 主键/自增列必须交由数据库生成，不能显式填默认值
                if col.primary_key or col.autoincrement is True:
                    continue
                default = col.default
                if default is not None:
                    arg = getattr(default, "arg", None)
                    values[name] = arg() if callable(arg) else arg
                else:
                    values[name] = _legacy_default_for_type(str(col.type))
            cols = list(values.keys())
            sql = (
                f"INSERT INTO sessions ({', '.join(cols)}) "
                f"VALUES ({', '.join(':' + c for c in cols)})"
            )
            bind = s.get_bind()
            dialect = bind.dialect.name if bind is not None else "sqlite"
            if dialect.startswith("sqlite"):
                cur = s.execute(text(sql), values)
                sid = int(cur.lastrowid)
            else:
                sid = int(s.execute(text(sql + " RETURNING id"), values).fetchone()[0])
            s.commit()
            return sid

        session = Session(
            project_id=project_id, title=title or None,
            model_id=model_id, fork_parent_id=fork_parent_id,
            permission_mode=permission_mode or "default",
            **(
                {
                    "goal_text": goal_text.strip()[:2000],
                    "goal_status": "active",
                    "goal_created_at": datetime.now(timezone.utc).isoformat(),
                }
                if goal_active else {}
            ),
        )
        s.add(session)
        s.flush()
        sid = session.id
        s.commit()
        return sid

    return await run_write_locked(patch, label="session.create")


async def get_session(db: AsyncSession, session_id: int) -> Session | None:
    return await db.get(Session, session_id)


async def auto_title_session(db: AsyncSession, session: Session, first_text: str) -> str | None:
    """为尚未命名的会话生成首条用户消息标题（写引擎单写线程）。"""
    if session.title:
        return None
    title = first_text.strip().replace("\n", " ")[:30]
    if not title:
        return None
    await patch_session(session.id, title=title)
    return title


async def patch_session(session_id: int, **fields) -> None:
    """写引擎单写线程提交 Session 字段补丁（无锁单写者；None 跳过）。"""
    from app.persistence.database import run_write_locked

    def patch(s):
        row = s.get(Session, session_id)
        if row is None:
            return
        for k, v in fields.items():
            if v is not None:
                setattr(row, k, v)
        s.commit()

    await run_write_locked(patch, label=f"session.patch.{session_id}")


async def list_sessions(db: AsyncSession, project_id: int | None = None,
                        include_archived: bool = False) -> list[Session]:
    stmt = select(Session)
    if project_id is not None:
        stmt = stmt.where(Session.project_id == project_id)
    if not include_archived:
        stmt = stmt.where(Session.status != "archived")
    res = await db.execute(stmt.order_by(Session.pinned.desc(), Session.pinned_at.desc(), Session.updated_at.desc()))
    return list(res.scalars().all())


async def update_session(db: AsyncSession, session_id: int, **kwargs) -> str | None:
    """更新会话字段（写引擎单写线程）。返回 None（无对象）/ 任意标量（成功）。

    v7: pinned 状态变化时同步维护 pinned_at——置顶写当前时间（"后置顶在上"依据），
    取消置顶清空。仅在 pinned 有值且与旧值不同时才触碰（避免无关更新刷新置顶序）。
    """
    from datetime import datetime, timezone
    from app.persistence.database import run_write_locked

    def patch(s):
        session = s.get(Session, session_id)
        if session is None:
            return None
        new_pinned = kwargs.get("pinned")
        if new_pinned is not None and new_pinned != session.pinned:
            if new_pinned:
                session.pinned_at = datetime.now(timezone.utc).isoformat()
            else:
                session.pinned_at = None
        for k, v in kwargs.items():
            if v is not None:
                setattr(session, k, v)
        s.commit()
        return session.status

    return await run_write_locked(patch, label=f"session.update.{session_id}")


async def fork_session(db: AsyncSession, session_id: int, title: str | None = None) -> int:
    """复制会话（仅复制元数据与消息，任务/子代理不复制；写引擎单写线程）。"""
    from app.persistence.database import run_write_locked

    def patch(s):
        src = s.get(Session, session_id)
        if src is None:
            raise ValueError("session not found")
        new_session = Session(
            project_id=src.project_id,
            title=title or f"{src.title or '会话'} · 分支",
            model_id=src.model_id,
            fork_parent_id=session_id,
            status="active",
        )
        s.add(new_session)
        s.flush()
        # 复制消息
        rows = s.execute(
            select(Message).where(Message.session_id == session_id, Message.deleted == False)  # noqa: E712
        ).scalars().all()
        for m in rows:
            s.add(Message(
                session_id=new_session.id,
                turn_id=None,
                thread_id=None,
                sender_type=m.sender_type,
                sender_id=m.sender_id,
                msg_type=m.msg_type,
                content=m.content,
                token_usage=m.token_usage,
            ))
        s.commit()
        return new_session.id

    return await run_write_locked(patch, label="session.fork")


async def create_system_message(db: AsyncSession, *, session_id: int, content: dict) -> int:
    """v2.2 (对齐 zcode 3.11): 写一条系统消息（模型切换 divider 等，写引擎单写线程）。"""
    from app.core.enums import MsgType, SenderType

    from app.persistence.database import run_write_locked

    def patch(s):
        from app.persistence.models.message import Message as _Msg
        m = _Msg(
            session_id=session_id, turn_id=None, thread_id=None,
            sender_type=SenderType.SYSTEM.value, sender_id=None,
            msg_type=MsgType.SYSTEM.value, content=content,
        )
        s.add(m)
        s.flush()
        mid = m.id
        s.commit()
        return mid

    return await run_write_locked(patch, label="session.sysmsg")


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
        .order_by(Message.id.asc())
    )
    if limit:
        stmt = stmt.limit(limit)
    res = await db.execute(stmt)
    return list(res.scalars().all())


async def list_thread_messages(db: AsyncSession, session_id: int, thread_id: int,
                               limit: int | None = None) -> list[Message]:
    """指定子代理线程消息，默认过滤已回滚软删消息（问题14）。

    保持时间正序（最旧在前），供 build_thread_context_with_window 按 token 预算择优。
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
