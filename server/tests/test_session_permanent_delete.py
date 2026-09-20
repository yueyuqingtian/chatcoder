"""会话永久删除的级联清理测试（plan-282-1441 #4）。

背景：归档页的「批量删除」需要**物理删除**，而旧接口 `DELETE /sessions/{id}`
只有归档语义（置 status="archived"），用户在归档页删了仍然看得见。
新增 `delete_session_permanent` 必须把该会话的关联数据一并清干净，
否则会留下孤儿 messages/turns/tasks 并污染后续统计。
"""
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.persistence.database import Base
from app.persistence.models import Message, Session, Task, Turn  # noqa: F401 注册全部模型
from app.services import session_service


@pytest.fixture
async def db(tmp_path):
    """独立临时文件库：`delete_session_permanent` 走写引擎（单写线程），
    必须与该库同源，否则写引擎操作用于另一份库。"""
    db_url = f"sqlite+aiosqlite:///{tmp_path}/perm.db"
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    from app.persistence import write_engine as _we
    _we.configure(db_url, foreign_keys=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()
    _we.configure(None)


async def _seed(db) -> int:
    """造一个带消息/轮次/任务的会话，返回 session_id。"""
    session = Session(project_id=None, title="待删会话", status="archived")
    db.add(session)
    await db.flush()
    sid = session.id

    turn = Turn(session_id=sid, status="completed")
    db.add(turn)
    await db.flush()

    db.add(Message(session_id=sid, turn_id=turn.id, sender_type="user",
                   msg_type="text", content={"text": "hi"}))
    db.add(Task(session_id=sid, turn_id=turn.id, kind="step", title="步骤 1", status="done"))
    await db.commit()
    return sid


async def _count(db, model, sid: int) -> int:
    res = await db.execute(select(func.count()).select_from(model).where(model.session_id == sid))
    return int(res.scalar() or 0)


@pytest.mark.asyncio
async def test_permanent_delete_cascades(db):
    """永久删除后：会话与其 messages/turns/tasks 全部消失（不留孤儿）。"""
    sid = await _seed(db)
    assert await _count(db, Message, sid) == 1
    assert await _count(db, Turn, sid) == 1
    assert await _count(db, Task, sid) == 1

    result = await session_service.delete_session_permanent(db, sid)
    assert result is not None and result["ok"] is True
    # 计数回执里应体现各表删除行数
    assert result["deleted"].get("messages") == 1
    assert result["deleted"].get("turns") == 1
    assert result["deleted"].get("tasks") == 1

    assert await _count(db, Message, sid) == 0
    assert await _count(db, Turn, sid) == 0
    assert await _count(db, Task, sid) == 0
    assert await db.get(Session, sid) is None


@pytest.mark.asyncio
async def test_permanent_delete_missing_returns_none(db):
    """会话不存在时返回 None（路由据此返回 404，而不是静默成功）。"""
    assert await session_service.delete_session_permanent(db, 999999) is None


@pytest.mark.asyncio
async def test_archive_still_keeps_session(db):
    """对照：归档语义（默认删除）必须**保留**数据，只改状态。"""
    sid = await _seed(db)
    status = await session_service.update_session(db, sid, status="archived")
    assert status == "archived"
    assert await _count(db, Message, sid) == 1
    assert await db.get(Session, sid) is not None
