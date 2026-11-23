"""plan-20261123-ux-fixes：供应商更新防陈旧缓存回弹 + 模型同步保留已有配置与新模型默认多模态测试。"""
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.gateway.routers import providers as providers_router
from app.gateway.schemas import ProviderUpdate
from app.persistence.database import Base
from app.persistence.models.model_reg import Model, Provider
from app.persistence.models.ta3_auth import Ta3Auth
from app.persistence.models.trae_auth import TraeAuth
from app.persistence.models.workbuddy_auth import WorkBuddyAuth
from app.services import model_service, provider_service


@pytest.fixture
async def db(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path}/provider_sync.db"
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


async def test_update_provider_returns_fresh_state_on_first_call(db):
    """同一 AsyncSession 内先读后写再读（PATCH /providers/{id} 路径），首次即返回新值，不再回弹。"""
    pid = await provider_service.create_provider(
        db,
        name="ta3",
        base_url="https://newcoder-inner.yinhaiyun.com/ta3api",
        api_format="ta3",
        proxy_mode="inherit",
        proxy_url="http://127.0.0.1:7897",
    )

    out = await providers_router.update_provider(
        pid, ProviderUpdate(proxy_mode="direct", is_active=False), db,
    )
    assert out.proxy_mode == "direct"
    assert out.is_active is False

    cleared = await providers_router.update_provider(
        pid, ProviderUpdate(proxy_mode="inherit", proxy_url=None), db,
    )
    assert cleared.proxy_mode == "inherit"
    assert cleared.proxy_url is None


async def test_bulk_upsert_preserve_existing_and_default_multimodal(db):
    """antigravity 等目录同步走 bulk_upsert_models：新模型默认开启多模态，已有模型不覆盖自定义配置。"""
    pid = await provider_service.create_provider(
        db, name="ag", base_url="https://daily-cloudcode-pa.googleapis.com", api_format="antigravity",
    )
    await provider_service.bulk_upsert_models(
        db,
        pid,
        [{"name": "custom-ag-model"}, {"name": "gemini-3-flash"}],
        preserve_existing_config=True,
        default_multimodal=True,
    )
    rows = (await db.execute(select(Model).where(Model.provider_id == pid))).scalars().all()
    by_name = {m.name: m for m in rows}
    assert by_name["custom-ag-model"].is_multimodal is True
    assert by_name["gemini-3-flash"].is_multimodal is True

    # 用户手动修改已有模型配置（上下文窗口、思考深度、多模态、停用）
    target = by_name["custom-ag-model"]
    await model_service.update_model(
        db,
        target.id,
        context_window=512000,
        reasoning_efforts=["low", "max"],
        is_multimodal=False,
        is_active=False,
    )

    # 再次同步：已有模型配置必须全部保留，新同步的模型默认开启多模态
    await provider_service.bulk_upsert_models(
        db,
        pid,
        [{"name": "custom-ag-model"}, {"name": "brand-new-model"}],
        preserve_existing_config=True,
        default_multimodal=True,
    )
    rows2 = (
        await db.execute(
            select(Model).where(Model.provider_id == pid).execution_options(populate_existing=True)
        )
    ).scalars().all()
    by_name2 = {m.name: m for m in rows2}
    assert by_name2["custom-ag-model"].context_window == 512000
    assert by_name2["custom-ag-model"].reasoning_efforts == ["low", "max"]
    assert by_name2["custom-ag-model"].is_multimodal is False
    assert by_name2["custom-ag-model"].is_active is False
    assert by_name2["brand-new-model"].is_multimodal is True


async def test_scan_bulk_save_keeps_existing_reasoning_efforts(db):
    """扫描模型弹窗保存时不传 reasoning_efforts，不得覆盖用户已配置的思考深度。"""
    pid = await provider_service.create_provider(
        db, name="oai", base_url="https://api.example.com/v1", api_format="openai",
    )
    mid = await model_service.create_model(
        db,
        name="gpt-4o",
        provider_id=pid,
        source_type="byok",
        context_window=300000,
        reasoning_efforts=["high", "max"],
        is_multimodal=True,
        is_active=True,
    )
    await provider_service.bulk_upsert_models(
        db,
        pid,
        [{"name": "gpt-4o", "is_active": True, "context_window": 300000, "is_multimodal": False}],
        preserve_existing_config=False,
    )
    m = await model_service.get_model(db, mid)
    assert m.reasoning_efforts == ["high", "max"]
    assert m.is_multimodal is False


async def test_kiro_sync_preserves_existing_and_defaults_new_multimodal(db):
    from app.auth.kiro.catalog import sync_kiro_models

    pid = await provider_service.create_provider(
        db, name="kiro", base_url="https://q.us-east-1.amazonaws.com", api_format="kiro",
    )
    provider = await provider_service.get_provider(db, pid)
    await sync_kiro_models(db, provider, discovered=["custom-discovered-model"])
    rows = {
        m.name: m
        for m in (
            await db.execute(
                select(Model).where(Model.provider_id == pid).execution_options(populate_existing=True)
            )
        ).scalars().all()
    }
    assert rows["custom-discovered-model"].is_multimodal is True

    target = rows["claude-sonnet-4.6"]
    await model_service.update_model(
        db, target.id, context_window=777000, reasoning_efforts=["medium"], is_multimodal=False, is_active=False,
    )

    await sync_kiro_models(db, provider, discovered=["custom-discovered-model", "another-new-model"])
    rows2 = {
        m.name: m
        for m in (
            await db.execute(
                select(Model).where(Model.provider_id == pid).execution_options(populate_existing=True)
            )
        ).scalars().all()
    }
    assert rows2["claude-sonnet-4.6"].context_window == 777000
    assert rows2["claude-sonnet-4.6"].reasoning_efforts == ["medium"]
    assert rows2["claude-sonnet-4.6"].is_multimodal is False
    assert rows2["claude-sonnet-4.6"].is_active is False
    assert rows2["another-new-model"].is_multimodal is True


async def test_ta3_sync_preserves_existing_and_defaults_new_multimodal(db, monkeypatch):
    from app.auth.ta3 import catalog as ta3_catalog
    from app.auth.ta3 import session as ta3_session

    pid = await provider_service.create_provider(
        db, name="ta3", base_url="https://newcoder-inner.yinhaiyun.com/ta3api", api_format="ta3",
    )
    provider = await provider_service.get_provider(db, pid)
    db.add(Ta3Auth(provider_id=pid, access_token="tok", refresh_token="ref", session_generation=1))
    await db.commit()

    async def fake_ensure_token(*_a, **_kw):
        return "tok"

    async def fake_fetch(*_a, **_kw):
        return {
            "organizations": [{"id": "org1", "name": "Org"}],
            "assistants_by_org": {
                "org1": [
                    {
                        "id": "asst1",
                        "configResult": {
                            "config": {
                                "models": [
                                    {
                                        "model": "deepseek-flash",
                                        "apiKey": "k-1",
                                        "apiBase": "https://ta3.example/v1",
                                        "contextLength": 128000,
                                        "isMultimodal": False,
                                    }
                                ]
                            }
                        },
                    }
                ]
            },
        }

    monkeypatch.setattr(ta3_session, "ensure_token", fake_ensure_token)
    monkeypatch.setattr(ta3_catalog, "fetch_catalog_raw", fake_fetch)

    await ta3_catalog.sync_ta3_models(db, provider, "https://newcoder-inner.yinhaiyun.com/ta3api")
    m = (
        await db.execute(
            select(Model)
            .where(Model.provider_id == pid, Model.name == "deepseek-flash")
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert m.is_multimodal is True
    assert m.context_window == 128000

    await model_service.update_model(
        db, m.id, context_window=512000, reasoning_efforts=["none", "high", "max"], is_multimodal=False,
    )

    await ta3_catalog.sync_ta3_models(db, provider, "https://newcoder-inner.yinhaiyun.com/ta3api")
    m2 = await model_service.get_model(db, m.id)
    assert m2.context_window == 512000
    assert m2.reasoning_efforts == ["none", "high", "max"]
    assert m2.is_multimodal is False


async def test_workbuddy_and_trae_sync_preserve_existing_and_default_new_multimodal(db, monkeypatch):
    from app.auth.trae import catalog as trae_catalog
    from app.auth.trae import session as trae_session
    from app.auth.workbuddy import catalog as wb_catalog
    from app.auth.workbuddy import session as wb_session

    wb_pid = await provider_service.create_provider(
        db, name="wb", base_url="https://copilot.tencent.com", api_format="workbuddy",
    )
    wb_provider = await provider_service.get_provider(db, wb_pid)
    db.add(WorkBuddyAuth(provider_id=wb_pid, access_token="tok", account={}))
    await db.commit()

    async def fake_wb_token(*_a, **_kw):
        return "tok"

    async def fake_wb_config(*_a, **_kw):
        return {
            "models": [
                {
                    "id": "wb-text-model",
                    "name": "WB Text",
                    "maxInputTokens": 128000,
                    "supportsImages": False,
                    "supportsReasoning": True,
                    "tags": ["craft"],
                }
            ],
            "agents": [{"name": "cli", "models": ["wb-text-model"]}],
        }

    monkeypatch.setattr(wb_session, "ensure_token", fake_wb_token)
    monkeypatch.setattr(wb_catalog, "fetch_config_raw", fake_wb_config)

    await wb_catalog.sync_workbuddy_models(db, wb_provider, "https://copilot.tencent.com")
    wm = (
        await db.execute(
            select(Model)
            .where(Model.provider_id == wb_pid, Model.name == "wb-text-model")
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert wm.is_multimodal is True

    await model_service.update_model(
        db, wm.id, context_window=640000, reasoning_efforts=["low"], is_multimodal=False,
    )
    await wb_catalog.sync_workbuddy_models(db, wb_provider, "https://copilot.tencent.com")
    wm2 = await model_service.get_model(db, wm.id)
    assert wm2.context_window == 640000
    assert wm2.reasoning_efforts == ["low"]
    assert wm2.is_multimodal is False

    # TRAE
    tr_pid = await provider_service.create_provider(
        db, name="trae", base_url="https://trae-api-cn.mchost.guru", api_format="trae",
    )
    tr_provider = await provider_service.get_provider(db, tr_pid)
    db.add(TraeAuth(provider_id=tr_pid, access_token="tok", account={}))
    await db.commit()

    async def fake_tr_token(*_a, **_kw):
        return "tok"

    async def fake_tr_catalog(*_a, **_kw):
        return {
            "function_configs": [
                {
                    "function": "solo_agent_lite",
                    "config_info_list": [
                        {
                            "config_name": "DeepSeek-V4-Flash",
                            "display_config": {"display_name": "DeepSeek", "multimodal": False},
                            "reasoning_effort_config": {"options": ["light", "high"], "support_thinking": True},
                            "model_detail_list": [{"prompt_max_tokens": 128000, "max_tokens": 16000}],
                        }
                    ],
                }
            ]
        }

    monkeypatch.setattr(trae_session, "ensure_token", fake_tr_token)
    monkeypatch.setattr(trae_catalog, "fetch_catalog_raw", fake_tr_catalog)

    await trae_catalog.sync_trae_models(db, tr_provider, "https://trae-api-cn.mchost.guru")
    tm = (
        await db.execute(
            select(Model)
            .where(Model.provider_id == tr_pid, Model.name == "DeepSeek-V4-Flash")
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert tm.is_multimodal is True

    await model_service.update_model(
        db, tm.id, context_window=999000, reasoning_efforts=["max"], is_multimodal=False,
    )
    await trae_catalog.sync_trae_models(db, tr_provider, "https://trae-api-cn.mchost.guru")
    tm2 = await model_service.get_model(db, tm.id)
    assert tm2.context_window == 999000
    assert tm2.reasoning_efforts == ["max"]
    assert tm2.is_multimodal is False


async def test_batch_update_and_delete_provider_models(db):
    """批量修改只覆盖传入字段、保留未传字段，且不越权影响其它供应商模型；批量删除正常清理。"""
    from app.gateway.schemas import ProviderModelsBatchDeleteIn, ProviderModelsBatchUpdateIn

    pid = await provider_service.create_provider(
        db, name="ag", base_url="https://daily-cloudcode-pa.googleapis.com", api_format="antigravity",
    )
    other_pid = await provider_service.create_provider(
        db, name="other", base_url="https://other.example/v1", api_format="openai",
    )
    m1 = await model_service.create_model(
        db, name="claude-opus-5-5-high", provider_id=pid, source_type="byok",
        context_window=200000, is_active=False, is_multimodal=False, reasoning_efforts=["low"],
    )
    m2 = await model_service.create_model(
        db, name="claude-sonnet-5-5-high", provider_id=pid, source_type="byok",
        context_window=200000, is_active=True, is_multimodal=True, reasoning_efforts=["high"],
    )
    m_other = await model_service.create_model(
        db, name="other-model", provider_id=other_pid, source_type="byok",
        context_window=128000, is_active=True, is_multimodal=False, reasoning_efforts=["medium"],
    )

    # 仅修改启用状态 + 上下文窗口（多模态与思考深度未传 → 保持各自原值）
    updated = await providers_router.batch_update_provider_models(
        pid,
        ProviderModelsBatchUpdateIn(model_ids=[m1, m2, m_other], is_active=True, context_window=1000000),
        db,
    )
    by_id = {m.id: m for m in updated}
    assert by_id[m1].is_active is True
    assert by_id[m1].context_window == 1000000
    assert by_id[m1].is_multimodal is False
    assert by_id[m1].reasoning_efforts == ["low"]
    assert by_id[m2].is_multimodal is True
    assert by_id[m2].reasoning_efforts == ["high"]
    # 其他供应商的模型不受影响
    other_row = await model_service.get_model(db, m_other)
    assert other_row.context_window == 128000

    # 统一设置思考深度与多模态，再清空思考深度
    await providers_router.batch_update_provider_models(
        pid,
        ProviderModelsBatchUpdateIn(model_ids=[m1, m2], is_multimodal=True, reasoning_efforts=["low", "high", "max"]),
        db,
    )
    assert (await model_service.get_model(db, m1)).reasoning_efforts == ["low", "high", "max"]
    assert (await model_service.get_model(db, m1)).is_multimodal is True

    await providers_router.batch_update_provider_models(
        pid,
        ProviderModelsBatchUpdateIn(model_ids=[m1], reasoning_efforts=[]),
        db,
    )
    assert (await model_service.get_model(db, m1)).reasoning_efforts == []

    # 批量删除
    del_res = await providers_router.batch_delete_provider_models(
        pid, ProviderModelsBatchDeleteIn(model_ids=[m1, m_other]), db,
    )
    assert del_res == {"ok": True, "deleted": 1}
    assert await model_service.get_model(db, m1) is None
    assert await model_service.get_model(db, m2) is not None
    assert await model_service.get_model(db, m_other) is not None
