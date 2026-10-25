"""plan-565-2323 G1/G2/G4：凭据启停语义与列表口径回归。

事故背景（用户实测）：在模型管理里对 workbuddy 的账号点「停用」，界面弹 `credential not found`，
但账号其实已被停用——`update_credential` 把「本次是否触发启用复位」当成「记录是否存在」返回，
路由据此抛 404，而事务早已提交。随后账号在列表里消失（`rank_credentials` 剔除停用行），
左侧计数却仍显示「N 账号」，供应商整体不可用。

本文件守住四条：停用必须成功、启用必须复位、不存在必须报未找到、列表必须含停用行。
"""
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.persistence.database import Base
from app.persistence.models.model_reg import Model, Provider, ProviderCredential
from app.services import credential_service as cs


@pytest.fixture
async def db(tmp_path):
    """隔离库：WriteEngine（同步单写）与异步读引擎连同一个文件，与 conftest 的口径一致。"""
    db_url = f"sqlite+aiosqlite:///{tmp_path}/cred_toggle.db"
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


async def _make_account(db, n: int = 2, api_format: str = "openai"):
    """建一个供应商 + n 个凭据，并挂一个模型（供取用路径解析）。"""
    provider = Provider(tenant_id=1, name=f"p-{api_format}", api_format=api_format,
                        base_url="https://x.example/v1")
    db.add(provider)
    await db.flush()
    ids = []
    for i in range(n):
        cred = ProviderCredential(
            provider_id=provider.id, label=f"账号{i}", api_key=f"sk-{i}",
            priority=i, is_active=True, status="ok",
        )
        db.add(cred)
        await db.flush()
        ids.append(cred.id)
    model = Model(tenant_id=1, name=f"m-{api_format}", provider_id=provider.id,
                  source_type="byok", api_format=api_format, is_active=True)
    db.add(model)
    await db.commit()
    return provider.id, ids, model.id


async def _reload(db, cid: int) -> ProviderCredential:
    res = await db.execute(select(ProviderCredential).where(ProviderCredential.id == cid))
    return res.scalar_one()


# ── G1：返回值语义 ──

async def test_disable_credential_succeeds_and_does_not_report_missing(db):
    """停用：必须返回「找到」，且真的落库——不能再出现「报错但生效」的隐性写入。"""
    _pid, ids, _mid = await _make_account(db)
    cid = ids[0]

    found = await cs.update_credential(db, cid, is_active=False)

    assert found is True, "停用被误判成记录不存在（这正是 404 报错的根因）"
    cred = await _reload(db, cid)
    assert cred.is_active is False


async def test_enable_credential_resets_health(db):
    """启用：复位为 ok、清冷却与失败计数（用户「想对他们重新启用」的落点）。"""
    _pid, ids, _mid = await _make_account(db)
    cid = ids[0]
    await cs.update_credential(db, cid, is_active=False)
    # 先人为制造冷却，确认启用确实把健康态清干净
    await cs.update_credential(db, cid, status="cooldown",
                               cooldown_until="2999-01-01T00:00:00+00:00", fail_count=3)

    found = await cs.update_credential(db, cid, is_active=True)

    assert found is True
    cred = await _reload(db, cid)
    assert cred.is_active is True
    assert cred.status == "ok"
    assert cred.cooldown_until is None
    assert (cred.fail_count or 0) == 0


async def test_missing_credential_reports_missing(db):
    """记录真的不存在时仍要报「未找到」——路由据此抛 404，不能把这条也放行。"""
    await _make_account(db, n=1)
    found = await cs.update_credential(db, 999999, is_active=False)
    assert found is False


# ── G2：列表口径 ──

async def test_rank_credentials_includes_inactive_tail_only_when_asked(db):
    """展示口径带停用行（排队尾）；默认取用池口径不变（这正是「左列 2 账号 / 右侧暂无账号」的修法）。"""
    pid, ids, _mid = await _make_account(db)
    c1, c2 = ids
    await cs.update_credential(db, c1, is_active=False)

    rows = await cs.list_credentials(db, pid)
    assert len(rows) == 2, "底层列表本身就必须含停用行"

    default_view = cs.rank_credentials(rows)
    assert [c.id for c in default_view] == [c2], "默认口径（取用池）不得包含停用行"

    shown = cs.rank_credentials(rows, include_inactive=True)
    assert [c.id for c in shown] == [c2, c1], "展示口径：可用在前、停用行排队尾"
    assert shown[-1].is_active is False


# ── G4：错误原因可区分 ──

async def test_all_disabled_reason_is_distinct(db):
    """全部凭据被停用：原因码必须与「凭据取不到 token」区分开，用户才知道该去启用账号。"""
    pid, ids, mid = await _make_account(db)
    for cid in ids:
        await cs.update_credential(db, cid, is_active=False)

    from app.models.registry import get_model_registry

    model = await db.get(Model, mid)
    provider, reason = await get_model_registry().get_provider_for_model(db, model)

    assert provider is None
    assert reason == "provider_credentials_all_disabled"


async def test_available_credential_keeps_provider_usable(db):
    """对照：只停用一个账号时，另一个仍能被取到——停用单条不该让整个供应商不可用。"""
    pid, ids, mid = await _make_account(db)
    await cs.update_credential(db, ids[0], is_active=False)

    from app.models.registry import get_model_registry

    model = await db.get(Model, mid)
    provider, reason = await get_model_registry().get_provider_for_model(db, model)

    assert provider is not None, f"仍有可用账号却解析失败（reason={reason}）"
