"""ta3 修复回归单测（plan-290：登录后 key 不保存 + 模型 URL 404）。

两个缺陷：
1. 目录同步 _persist 用异步会话查出的 ORM 对象、却在写引擎同步会话里 commit，
   属性变更不落库 → 模型的 api_key（目录下发的 llm- key）永远为 NULL；
2. registry 走凭据分支时一律用 provider.base_url（站点根 .../newcoder）+ 账号
   ide-session- token，而 ta3 的 LLM 网关在模型级 dispatch 路径
   （.../ai/dispatch/v2）且只认 per-model llm- key → 404 / 401。

对齐参考项目 modelClient.ts：apiBase / apiKey 都取自**模型行**。
"""
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.persistence.database import Base
from app.persistence.models.model_reg import Model, Provider, ProviderCredential
from app.persistence.models.ta3_auth import Ta3Auth

DISPATCH_BASE = "https://lc.yinhaiyun.com/newcoder/ai/dispatch/v2"
SITE_BASE = "https://lc.yinhaiyun.com/newcoder"
LLM_KEY = "llm-REGRESSION_TEST_KEY"


@pytest.fixture
async def db(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path}/ta3fix.db"
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    from app.persistence import write_engine as _we
    _we.configure(db_url, foreign_keys=False)  # 写引擎与测试库同源（真实落库路径）
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()
    _we.configure(None)


def _assistant_payload() -> dict:
    """远端目录最小结构：模型带 apiKey(llm-) 与模型级 apiBase(dispatch)。"""
    return {
        "organizations": [{"id": "org1", "name": "Org1"}],
        "assistants_by_org": {
            "org1": [{
                "id": "assistant1",
                "configResult": {"config": {"models": [{
                    "model": "deepseek-flash",
                    "apiKey": LLM_KEY,
                    "apiBase": DISPATCH_BASE,
                    "provider": "deepseek",
                    "chatOptions": {"baseSystemMessage": "sys"},
                    "completionOptions": {"temperature": 0.1, "contextLength": 512000},
                }]}},
            }],
        },
    }


async def test_catalog_sync_persists_model_key_and_base_url(db, monkeypatch):
    """缺陷 1：同步后模型行必须落库 llm- key 与模型级 dispatch base_url。

    关键：必须覆盖**已存在模型**的更新路径。真实现场正是如此——模型早在
    2026-08-26 就已入库（api_key=NULL），09-20 登录同步时走的是"更新已有行"分支，
    而该分支此前把异步会话的 ORM 对象拿到写引擎同步会话里 commit，变更丢失，
    于是 llm- key 永远写不进去。
    """
    from app.auth.ta3 import catalog, session as ta3_session

    provider = Provider(tenant_id=1, name="牛码", api_format="ta3", base_url=SITE_BASE)
    db.add(provider)
    await db.flush()
    # 预先存在的模型行（无 key、无 base_url），模拟升级前的历史数据
    db.add(Model(tenant_id=1, name="deepseek-flash", provider_id=provider.id,
                 source_type="byok", api_format="ta3", is_active=True))
    await db.commit()

    async def _fake_ensure_token(_db, _pid, _base):
        return "ide-session-fake"

    async def _fake_catalog(_base, _token):
        return _assistant_payload()

    monkeypatch.setattr(ta3_session, "ensure_token", _fake_ensure_token)
    monkeypatch.setattr(catalog, "fetch_catalog_raw", _fake_catalog)

    pid = provider.id  # expire_all 前先取标量，避免之后触发异步懒加载
    entries = await catalog.sync_ta3_models(db, provider, SITE_BASE)
    assert len(entries) == 1

    # 关键断言：已存在的模型行被写入了 key / dispatch base_url
    # （修复前此处 api_key 恒为 NULL —— 登录后"key 没保存"）
    # expire_all 强制从数据库重读：否则会命中内存身份映射里被改过的对象，出现
    # "改了内存但没落库"也通过的假阳性。
    db.expire_all()
    rows = (await db.execute(
        Model.__table__.select().where(Model.provider_id == pid)
    )).mappings().all()
    assert len(rows) == 1
    row = rows[0]
    assert row["api_key"] == LLM_KEY
    assert row["base_url"] == DISPATCH_BASE


async def test_provider_uses_model_level_base_url_and_key(db):
    """缺陷 2：凭据分支构造 ta3 provider 时必须用模型级 base_url 与 llm- key。

    回归前：base_url=SITE_BASE（站点根）→ 请求 .../newcoder/chat/completions 404；
    key=账号 ide-session- token → 401。
    """
    from app.models.registry import get_model_registry

    provider = Provider(tenant_id=1, name="牛码", api_format="ta3", base_url=SITE_BASE)
    db.add(provider)
    await db.flush()

    # 账号凭据（无 api_key，token 由 ta3_auth 提供）
    db.add(ProviderCredential(provider_id=provider.id, label="胡雷", priority=0,
                              is_active=True, status="ok", extra={"kind": "ta3_account"}))
    # 模型行：api_key / base_url 来自目录
    model = Model(tenant_id=1, name="deepseek-flash", provider_id=provider.id,
                  source_type="byok", api_format="ta3", is_active=True,
                  api_key=LLM_KEY, base_url=DISPATCH_BASE, ta3_meta={"anthropic": False})
    db.add(model)
    db.add(Ta3Auth(provider_id=provider.id, access_token="ide-session-fake"))
    await db.commit()

    p, reason = await get_model_registry().get_provider_for_model(db, model)
    assert p is not None, f"provider 构造失败: {reason}"
    assert p._base_url == DISPATCH_BASE, "必须走模型级 dispatch 路径，否则 404"
    assert p._api_key == LLM_KEY, "必须用 per-model llm- key，否则 401"
    assert p._anthropic is False
