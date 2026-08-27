"""turn 时间戳回归测试：completed_at 必须是真实时间戳，而非 SQL 表达式字面量 "now()"。

历史 bug：`str(func.now())` 把 SQL 表达式字符串化为 "now()"，前端 parseUtc 解析为 0，
导致任务完成后显示「已工作 0 秒」。
"""
from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.persistence.database import Base
from app.persistence.models import Turn  # noqa: F401  注册全部模型到 Base.metadata
from app.services.turn_service import create_turn, update_turn_status


@pytest.fixture
async def db():
    # 独立内存引擎 + StaticPool：所有会话共享同一份内存库（默认 QueuePool 会按连接分库）
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


async def test_completed_at_is_real_timestamp(db):
    turn = await create_turn(db, session_id=1)
    await update_turn_status(db, turn.id, "completed", completed=True)
    await db.commit()
    await db.refresh(turn)

    assert turn.started_at is not None
    started = datetime.fromisoformat(turn.started_at)
    assert started.tzinfo is not None  # 创建响应必须立即带可解析的开始时间
    assert turn.completed_at is not None
    assert turn.completed_at != "now()"
    parsed = datetime.fromisoformat(turn.completed_at)
    assert parsed.tzinfo is not None  # 必须是带时区的 ISO 时间戳


async def test_completed_false_keeps_completed_at_null(db):
    turn = await create_turn(db, session_id=1)
    await update_turn_status(db, turn.id, "running")
    await db.commit()
    await db.refresh(turn)
    assert turn.completed_at is None
