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
    factory = async_sessionmaker(
        engine, class_=LockedAsyncSession, autoflush=False, expire_on_commit=False,
    )
    yield engine, factory
    await engine.dispose()


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
