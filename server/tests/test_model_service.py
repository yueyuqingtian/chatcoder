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
async def db(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path}/model.db"
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    from app.persistence import write_engine as _we
    _we.configure(db_url, foreign_keys=False)  # 写引擎（单写线程）与测试库同源
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()
    _we.configure(None)


async def test_update_ta3_model_writes_override(db):
    m = Model(tenant_id=1, name="glm-5.2", provider_id=1, source_type="byok",
              api_format="ta3", is_multimodal=False, ta3_meta={"provider": "ta3"})
    db.add(m)
    await db.flush()
    await db.commit()
    ok = await model_service.update_model(db, m.id, is_multimodal=True)
    assert ok is True
    ref = await db.get(Model, m.id)
    await db.refresh(ref)  # 写引擎独立连接提交，重读最新
    assert ref.is_multimodal is True
    assert (ref.ta3_meta or {}).get("multimodal_override") is True


async def test_update_non_ta3_model_does_not_write_override(db):
    m = Model(tenant_id=1, name="gpt-4o", source_type="byok",
              api_format="openai", is_multimodal=False)
    db.add(m)
    await db.flush()
    await db.commit()
    ok = await model_service.update_model(db, m.id, is_multimodal=True)
    assert ok is True
    ref = await db.get(Model, m.id)
    await db.refresh(ref)  # 写引擎独立连接提交，重读最新
    assert ref.is_multimodal is True
    assert (ref.ta3_meta or {}).get("multimodal_override") is not True


async def test_create_ta3_multimodal_model_marks_override(db):
    created_id = await model_service.create_model(
        db, name="minimax-m3", provider_id=1, source_type="byok",
        api_format="ta3", is_multimodal=True,
    )
    ref = await db.get(Model, created_id)
    assert ref.is_multimodal is True
    assert (ref.ta3_meta or {}).get("multimodal_override") is True


# ── 删除模型/供应商时的 FK 约束（sqlite 外键开启，对齐生产）──


@pytest.fixture
async def db_fk(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path}/model_fk.db"
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    from app.persistence import write_engine as _we
    _we.configure(db_url, foreign_keys=True)  # 生产口径：开启外键约束
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()
    _we.configure(None)


async def test_delete_model_nulls_references(db_fk):
    """模型被 sessions/agents/subagent_profiles 引用时删除不再 FK 报错，引用置空。"""
    from sqlalchemy import select as _select

    from app.persistence.models.agent import Agent
    from app.persistence.models.message import Session
    from app.persistence.models.subagent_profile import SubagentProfile

    m = Model(tenant_id=1, name="ta3-x", provider_id=1, source_type="byok", api_format="ta3")
    db_fk.add(m)
    await db_fk.flush()
    sess = Session(title="s", model_id=m.id)
    agent = Agent(kind="main", name="main", model_id=m.id)
    profile = SubagentProfile(name="explore", model_id=m.id)
    db_fk.add_all([sess, agent, profile])
    await db_fk.commit()

    ok = await model_service.delete_model(db_fk, m.id)
    assert ok is True
    # 写引擎独立连接提交：重读验证
    await db_fk.refresh(sess)
    await db_fk.refresh(agent)
    await db_fk.refresh(profile)
    assert sess.model_id is None
    assert agent.model_id is None
    assert profile.model_id is None
    gone = (await db_fk.execute(_select(Model).where(Model.id == m.id))).scalars().first()
    assert gone is None


async def test_delete_provider_cascade_nulls_references(db_fk):
    """删除供应商级联删模型时同样置空会话引用（此前同 FK 报错路径）。"""
    from app.persistence.models.message import Session
    from app.persistence.models.model_reg import Provider
    from app.services import provider_service

    p = Provider(tenant_id=1, name="ta3", api_format="ta3")
    db_fk.add(p)
    await db_fk.flush()
    m = Model(tenant_id=1, name="glm-x", provider_id=p.id, source_type="byok", api_format="ta3")
    db_fk.add(m)
    await db_fk.flush()
    sess = Session(title="s", model_id=m.id)
    db_fk.add(sess)
    await db_fk.commit()

    ok = await provider_service.delete_provider(db_fk, p.id)
    assert ok is True
    await db_fk.refresh(sess)
    assert sess.model_id is None
