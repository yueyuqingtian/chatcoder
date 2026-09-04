"""plan-152-704：/api/usage/stats 统计端点单测。

覆盖：供应商分组不合并、自定义区间过滤、逐日聚合、峰值/连续天数、旧数据供应商名回填。
使用 sqlite 内存库 + 直接调用端点函数（绕过 FastAPI 依赖）。
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.gateway.routers.usage import usage_stats
from app.persistence.database import Base
from app.persistence.models.model_reg import Model, Provider
from app.persistence.models.usage_record import UsageRecord


@pytest.fixture
async def db():
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


def _add(db, *, model_id, model_name, provider_name="", prompt=0, completion=0, created_at):
    db.add(UsageRecord(
        model_id=model_id, model_name=model_name, provider_name=provider_name,
        prompt_tokens=prompt, completion_tokens=completion,
        reasoning_tokens=0, cached_tokens=0, created_at=created_at,
    ))


async def test_same_name_different_provider_not_merged(db):
    """同名模型分属不同供应商，必须分成两条且显示名带供应商前缀。"""
    p1 = Provider(name="jyld")
    p2 = Provider(name="deepseek")
    db.add_all([p1, p2])
    await db.flush()
    m1 = Model(name="glm-5.3-flash", provider_id=p1.id, source_type="openai")
    m2 = Model(name="glm-5.3-flash", provider_id=p2.id, source_type="openai")
    db.add_all([m1, m2])
    await db.flush()

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    _add(db, model_id=m1.id, model_name="glm-5.3-flash", provider_name="jyld", prompt=100, completion=0, created_at=now)
    _add(db, model_id=m2.id, model_name="glm-5.3-flash", provider_name="deepseek", prompt=200, completion=0, created_at=now)
    await db.commit()

    result = await usage_stats(start=None, end=None, days=30, db=db)
    assert result["total"]["calls"] == 2
    assert len(result["by_model"]) == 2
    displays = {x["display_name"] for x in result["by_model"]}
    assert displays == {"jyld/glm-5.3-flash", "deepseek/glm-5.3-flash"}


async def test_custom_range_filters(db):
    """自定义 start/end 区间只统计区间内的流水。"""
    base = datetime(2026, 8, 15, 10, 0)
    _add(db, model_id=None, model_name="m", prompt=10, completion=0, created_at=base)            # 8-15
    _add(db, model_id=None, model_name="m", prompt=20, completion=0, created_at=base + timedelta(days=3))  # 8-18
    _add(db, model_id=None, model_name="m", prompt=40, completion=0, created_at=base + timedelta(days=10)) # 8-25
    await db.commit()

    result = await usage_stats(start="2026-08-16", end="2026-08-19", days=30, db=db)
    assert result["total"]["calls"] == 1
    assert result["total"]["prompt"] == 20
    # daily 只含 8-18
    assert [d["date"] for d in result["daily"]] == ["2026-08-18"]


async def test_daily_aggregation_and_peak_streak(db):
    """逐日聚合与峰值/连续天数（基于全历史 day_all）。"""
    today = datetime.now().astimezone().date()
    def at(days_ago, hour=10):
        d = today - timedelta(days=days_ago)
        return datetime(d.year, d.month, d.day, hour, 0)

    # 连续 4 天（today..today-3）+ 一天空档（today-6）
    _add(db, model_id=None, model_name="m", prompt=100, completion=0, created_at=at(0))   # today
    _add(db, model_id=None, model_name="m", prompt=500, completion=0, created_at=at(1))   # today-1
    _add(db, model_id=None, model_name="m", prompt=300, completion=0, created_at=at(2))   # today-2
    _add(db, model_id=None, model_name="m", prompt=10, completion=0, created_at=at(3))    # today-3
    _add(db, model_id=None, model_name="m", prompt=7, completion=0, created_at=at(6))     # today-6 空档
    await db.commit()

    result = await usage_stats(start=None, end=None, days=30, db=db)
    assert result["peak_tokens"] == 500
    assert result["streak_current"] == 4
    assert result["streak_longest"] == 4
    # 全历史 daily_all 含 5 个活跃日
    assert len(result["daily_all"]) == 5


async def test_historical_row_provider_backfill(db):
    """历史流水 provider_name 为空时，经 model_id 回填供应商名。"""
    p = Provider(name="cmc")
    db.add(p)
    await db.flush()
    m = Model(name="glm-5.3-flash", provider_id=p.id, source_type="openai")
    db.add(m)
    await db.flush()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    # 存储时未写入 provider_name（历史数据），仅 model_id + model_name
    _add(db, model_id=m.id, model_name="glm-5.3-flash", provider_name="", prompt=10, completion=0, created_at=now)
    await db.commit()

    result = await usage_stats(start=None, end=None, days=30, db=db)
    assert result["total"]["calls"] == 1
    assert result["by_model"][0]["display_name"] == "cmc/glm-5.3-flash"


async def test_no_data_returns_empty_shapes(db):
    """无任何流水时返回空结构且不报错。"""
    result = await usage_stats(start=None, end=None, days=30, db=db)
    assert result["total"]["calls"] == 0
    assert result["by_model"] == []
    assert result["daily"] == []
    assert result["peak_tokens"] == 0
    assert result["streak_current"] == 0
    assert result["streak_longest"] == 0
