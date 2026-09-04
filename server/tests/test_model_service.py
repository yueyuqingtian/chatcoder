"""model_service 手动多模态配置测试（plan-156-739）。

覆盖：
- ta3 模型 update_model 修改 is_multimodal → 写 ta3_meta.multimodal_override，
  防止目录同步（catalog.py 仅在 override 存在时保留用户设置）覆盖手动开启的多模态。
- 非 ta3 模型修改 is_multimodal → 不写 override（避免无关写入）。
- create_model 新建 ta3 多模态模型 → 同样打 override 标记。
"""
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.persistence.database import Base
from app.persistence.models.model_reg import Model
from app.services import model_service


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


async def test_update_ta3_model_writes_override(db):
    m = Model(tenant_id=1, name="glm-5.2", provider_id=1, source_type="byok",
              api_format="ta3", is_multimodal=False, ta3_meta={"provider": "ta3"})
    db.add(m)
    await db.flush()
    updated = await model_service.update_model(db, m.id, is_multimodal=True)
    assert updated.is_multimodal is True
    assert (updated.ta3_meta or {}).get("multimodal_override") is True


async def test_update_non_ta3_model_does_not_write_override(db):
    m = Model(tenant_id=1, name="gpt-4o", source_type="byok",
              api_format="openai", is_multimodal=False)
    db.add(m)
    await db.flush()
    updated = await model_service.update_model(db, m.id, is_multimodal=True)
    assert updated.is_multimodal is True
    assert (updated.ta3_meta or {}).get("multimodal_override") is not True


async def test_create_ta3_multimodal_model_marks_override(db):
    created = await model_service.create_model(
        db, name="minimax-m3", provider_id=1, source_type="byok",
        api_format="ta3", is_multimodal=True,
    )
    assert created.is_multimodal is True
    assert (created.ta3_meta or {}).get("multimodal_override") is True
