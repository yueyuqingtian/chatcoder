"""SQLite 文件库并发回归测试。"""
import asyncio

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.persistence.database import Base, LockedAsyncSession
from app.persistence.models.message import Message, Session


@pytest.fixture
async def file_db(tmp_path):
    db_path = tmp_path / "concurrency.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 5},
        poolclass=NullPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    from app.persistence import write_engine as _we
    _we.configure(f"sqlite+aiosqlite:///{db_path}", foreign_keys=False)  # 写引擎与测试库同源
    factory = async_sessionmaker(
        engine, class_=LockedAsyncSession, autoflush=False, expire_on_commit=False,
    )
    yield engine, factory
    await engine.dispose()
    _we.configure(None)


@pytest.mark.asyncio
async def test_autoflush_is_disabled(file_db):
    _engine, factory = file_db
    async with factory() as db:
        session = Session(title="before")
        db.add(session)
        await db.flush()
        await db.commit()
        session.title = "after"
        rows = await db.execute(
            select(Session).where(Session.id == session.id).execution_options(populate_existing=True)
        )
        assert rows.scalar_one().title == "before"
        await db.rollback()


@pytest.mark.asyncio
async def test_concurrent_short_writes_finish_without_lock_error(file_db):
    _engine, factory = file_db

    async def write_one(index: int):
        async with factory() as db:
            db.add(Session(title=f"session-{index}"))
            await db.flush()
            await db.commit()

    await asyncio.gather(*(write_one(i) for i in range(12)))
    async with factory() as db:
        rows = await db.execute(select(Session).order_by(Session.id.asc()))
        assert len(rows.scalars().all()) == 12


@pytest.mark.asyncio
async def test_direct_dml_is_committed_after_short_lock(file_db):
    _engine, factory = file_db
    async with factory() as db:
        db.add(Session(title="one"))
        await db.flush()
        await db.commit()
        result = await db.execute(text("UPDATE sessions SET title='two' WHERE id=1"))
        assert result.rowcount == 1
        await db.commit()
        row = (await db.execute(select(Session).where(Session.id == 1))).scalar_one()
        assert row.title == "two"


@pytest.mark.asyncio
async def test_reentrant_same_session(file_db):
    """同会话内 execute(写)→flush→commit 可重入，不重复 acquire 写锁。"""
    _engine, factory = file_db
    async with factory() as db:
        db.add(Session(title="x"))
        await db.flush()  # acquire 写锁
        await db.execute(text("UPDATE sessions SET title='y'"))  # 复用锁，不重复 acquire
        await db.commit()
    async with factory() as db:
        row = (await db.execute(select(Session).where(Session.id == 1))).scalar_one()
        assert row.title == "y"


@pytest.mark.asyncio
async def test_transaction_lock_serializes_long_window(file_db):
    """A 事务（首写→commit 之间）持锁期间，B 写等待而非撞 SQLite 锁。"""
    _engine, factory = file_db
    order: list[str] = []
    a_done = asyncio.Event()

    async def slow_writer():
        async with factory() as db:
            db.add(Session(title="slow"))
            await db.flush()  # acquire 写锁 + 发 INSERT
            await a_done.wait()  # 长窗口：A 已持锁，等待 B 试图写
            await db.commit()
            order.append("A")

    async def fast_writer():
        async with factory() as db:
            db.add(Session(title="fast"))
            # B 在 A 未 commit 前尝试写 —— 应用层锁使其等待而非报错
            await db.flush()
            await db.commit()
            order.append("B")

    task_a = asyncio.create_task(slow_writer())
    await asyncio.sleep(0.05)  # A 已首写 + 持锁
    task_b = asyncio.create_task(fast_writer())
    await asyncio.sleep(0.05)  # B 已试图写（应等待中）
    assert order == []          # A 未 commit，B 未完成
    a_done.set()
    await asyncio.gather(task_a, task_b)
    assert order == ["A", "B"]  # 严格串行：A 先，B 后


@pytest.mark.asyncio
async def test_implicit_flush_locked(file_db):
    """仅 add 直接 commit（内隐 flush）也被事务级写锁覆盖，多连接并发无锁错。"""
    _engine, factory = file_db

    async def write_one(index: int):
        async with factory() as db:
            db.add(Session(title=f"implicit-{index}"))
            await db.commit()  # commit 内隐 flush，不经过显式 flush override

    await asyncio.gather(*(write_one(i) for i in range(10)))
    async with factory() as db:
        rows = await db.execute(select(Session).where(Session.title.like("implicit-%")))
        assert len(rows.scalars().all()) == 10


@pytest.mark.asyncio
async def test_cancel_releases_write_lock(file_db):
    """持锁协程被取消时释放写锁，后续写者正常完成。"""
    _engine, factory = file_db
    ev = asyncio.Event()

    async def writer():
        async with factory() as db:
            db.add(Session(title="cancelled"))
            await db.flush()  # acquire 写锁 + 发 INSERT
            await ev.wait()  # 持锁挂起，等取消

    task = asyncio.create_task(writer())
    await asyncio.sleep(0.05)  # writer 已持锁
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    # 锁应已释放：后续写者正常完成
    async with factory() as db:
        db.add(Session(title="ok"))
        await db.flush()
        await db.commit()
    async with factory() as db:
        rows = await db.execute(select(Session).where(Session.title == "ok"))
        assert len(rows.scalars().all()) == 1
