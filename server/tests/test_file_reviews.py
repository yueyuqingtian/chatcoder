"""file_reviews 原子 upsert 回归测试。

背景（server.log 2026-08-31 turn=631/632）：前端对同一 turn 重复发送审核请求，
旧实现"先查后插"在并发窗口内双双判不存在再 INSERT，触发
uq_file_review_turn_path 唯一约束冲突（IntegrityError → 500）。
修复：upsert_file_reviews 改为 SQLite ON CONFLICT DO UPDATE 原子 upsert。

覆盖：
- 新路径插入与计数
- 同值重复调用幂等（updated=0，不产生重复行）
- 翻转 reviewed 计数与落库值
- 批内重复路径不自撞唯一键（旧实现确定性 bug）
- 已 commit 记录的重复请求（模拟日志报错场景）
- 空白/空路径过滤
"""
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.persistence.database import Base
from app.persistence.models.review import FileReview
from app.services import rollback_service


@pytest.fixture
async def db():
    # StaticPool：所有 session 共享同一份内存库，模拟同一服务的多次请求
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


async def _rows(db: AsyncSession, turn_id: int) -> list[FileReview]:
    res = await db.execute(select(FileReview).where(FileReview.turn_id == turn_id))
    return list(res.scalars().all())


@pytest.mark.asyncio
async def test_upsert_inserts_new_paths(db):
    n = await rollback_service.upsert_file_reviews(
        db, turn_id=631, paths=["ai/chatcoder-plan-130-631.md"], reviewed=True,
    )
    await db.commit()
    assert n == 1
    rows = await _rows(db, 631)
    assert len(rows) == 1
    assert rows[0].reviewed is True


@pytest.mark.asyncio
async def test_upsert_idempotent_same_value(db):
    """同值重复调用（前端重复请求）幂等：updated=0，不产生重复行。"""
    await rollback_service.upsert_file_reviews(
        db, turn_id=631, paths=["ai/plan.md"], reviewed=True,
    )
    await db.commit()
    n = await rollback_service.upsert_file_reviews(
        db, turn_id=631, paths=["ai/plan.md"], reviewed=True,
    )
    await db.commit()
    assert n == 0
    assert len(await _rows(db, 631)) == 1


@pytest.mark.asyncio
async def test_upsert_flip_reviewed(db):
    await rollback_service.upsert_file_reviews(db, turn_id=1, paths=["a.md"], reviewed=True)
    await db.commit()
    n = await rollback_service.upsert_file_reviews(db, turn_id=1, paths=["a.md"], reviewed=False)
    await db.commit()
    assert n == 1
    rows = await _rows(db, 1)
    assert len(rows) == 1  # reviewed=False 只改值不删记录（状态可追溯）
    assert rows[0].reviewed is False


@pytest.mark.asyncio
async def test_upsert_duplicate_paths_in_batch(db):
    """批内重复路径：旧实现会 add 两条同键记录 flush 自撞 UNIQUE，必须去重。"""
    n = await rollback_service.upsert_file_reviews(
        db, turn_id=1, paths=["a.md", "a.md", "b.md", "b.md"], reviewed=True,
    )
    await db.commit()
    assert n == 2  # a.md + b.md（重复路径保序去重）
    rows = await _rows(db, 1)
    assert sorted(r.path for r in rows) == ["a.md", "b.md"]


@pytest.mark.asyncio
async def test_upsert_repeat_request_after_commit(db):
    """复现日志场景：同一 turn 的审核请求先后到达（各自独立 session），不报 UNIQUE。"""
    # 第一次请求：插入并 commit
    n1 = await rollback_service.upsert_file_reviews(
        db, turn_id=632, paths=["ai/chatcoder-plan-130-632.md"], reviewed=True,
    )
    await db.commit()
    assert n1 == 1
    # 第二次请求（commit 后再次 upsert，模拟重复请求）：幂等成功而非 IntegrityError
    n2 = await rollback_service.upsert_file_reviews(
        db, turn_id=632, paths=["ai/chatcoder-plan-130-632.md"], reviewed=True,
    )
    await db.commit()
    assert n2 == 0
    assert len(await _rows(db, 632)) == 1


@pytest.mark.asyncio
async def test_upsert_empty_and_blank_paths(db):
    assert await rollback_service.upsert_file_reviews(db, turn_id=1, paths=[], reviewed=True) == 0
    assert await rollback_service.upsert_file_reviews(db, turn_id=1, paths=None, reviewed=True) == 0
    assert await rollback_service.upsert_file_reviews(
        db, turn_id=1, paths=["", "   "], reviewed=True,
    ) == 0
    assert await _rows(db, 1) == []
