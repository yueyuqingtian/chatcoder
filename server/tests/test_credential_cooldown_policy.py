"""plan-290: 凭据冷却策略单测。

两条规则：
1. 连续失败达阈值才冷却——单次抖动只累计 fail_count，凭据仍可用；
2. 供应商只有一个凭据时永不冷却（冷却等于全局不可用，没有备选可切）。
"""
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.persistence.database import Base
from app.persistence.models.model_reg import Provider, ProviderCredential
from app.services import credential_service as cs


@pytest.fixture
async def db(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path}/cred.db"
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


async def _make_creds(db, provider_name: str, n: int) -> tuple[int, list[int]]:
    provider = Provider(tenant_id=1, name=provider_name, api_format="openai",
                        base_url="https://x.example/v1")
    db.add(provider)
    await db.flush()
    ids = []
    for i in range(n):
        c = ProviderCredential(provider_id=provider.id, label=f"k{i}", api_key=f"sk-{i}",
                               priority=i, is_active=True, status="ok")
        db.add(c)
        await db.flush()
        ids.append(c.id)
    await db.commit()
    return provider.id, ids


async def _reload(db, cid: int) -> ProviderCredential:
    res = await db.execute(select(ProviderCredential).where(
        ProviderCredential.id == cid).execution_options(populate_existing=True))
    return res.scalars().one()


async def test_single_credential_never_cools_down(db):
    """规则 2：唯一凭据反复失败也不进入冷却。"""
    _, ids = await _make_creds(db, "单凭据", 1)
    cid = ids[0]

    for i in range(1, 7):
        cooled = await cs.mark_failed(db, cid, "模型请求失败 500：boom")
        assert cooled is False, f"第 {i} 次失败不应冷却（唯一凭据）"
        c = await _reload(db, cid)
        assert c.status == "ok"
        assert c.cooldown_until is None
        assert c.fail_count == i          # 计数照常累计（留痕）
        assert c.last_error is not None

    # 且仍可被选中使用
    creds = await cs.list_credentials(db, await _provider_id(db, cid))
    assert [c.id for c in cs.available_credentials(creds)] == [cid]


async def test_multi_credentials_cool_only_after_threshold(db):
    """规则 1：多凭据时，未达阈值不冷却，达到阈值才冷却。"""
    threshold = cs.fail_threshold()
    assert threshold >= 2, "本用例要求阈值 > 1"
    _, ids = await _make_creds(db, "多凭据", 2)
    cid = ids[0]

    for i in range(1, threshold):
        cooled = await cs.mark_failed(db, cid, "模型请求失败 503：bad gateway")
        assert cooled is False, f"第 {i} 次失败未达阈值，不应冷却"
        c = await _reload(db, cid)
        assert c.status == "ok"
        assert c.cooldown_until is None
        assert c.fail_count == i

    cooled = await cs.mark_failed(db, cid, "模型请求失败 503：bad gateway")
    assert cooled is True, "达到阈值应冷却"
    c = await _reload(db, cid)
    assert c.status == "cooldown"
    assert c.cooldown_until is not None


async def test_success_resets_fail_count(db):
    """成功即清零计数，避免"隔很久的零星失败"累积成冷却。"""
    _, ids = await _make_creds(db, "清零", 2)
    cid = ids[0]
    await cs.mark_failed(db, cid, "500")
    await cs.mark_failed(db, cid, "500")
    assert (await _reload(db, cid)).fail_count == 2

    await cs.mark_ok(db, cid)
    c = await _reload(db, cid)
    assert c.fail_count == 0
    assert c.status == "ok"
    assert c.last_error is None


async def test_populated_single_credential_cool_state_is_ignored(db):
    """历史遗留：库里唯一凭据已被旧版本置为 cooldown → 选取层仍应放行。"""
    _, ids = await _make_creds(db, "遗留冷却", 1)
    cid = ids[0]

    def _force(s):
        c = s.get(ProviderCredential, cid)
        c.status = "cooldown"
        c.cooldown_until = "2099-01-01T00:00:00+00:00"
        s.commit()

    from app.persistence.database import run_write_locked
    await run_write_locked(_force, label="test.force_cooldown")

    creds = await cs.list_credentials(db, await _provider_id(db, cid))
    assert [c.id for c in cs.available_credentials(creds)] == [cid], \
        "唯一凭据的历史冷却状态应被忽略"


async def test_multi_credentials_cooled_one_is_skipped(db):
    """多凭据时冷却中的那条确实被跳过（不受单凭据豁免影响）。"""
    _, ids = await _make_creds(db, "跳过冷却", 2)
    first, second = ids

    def _force(s):
        c = s.get(ProviderCredential, first)
        c.status = "cooldown"
        c.cooldown_until = "2099-01-01T00:00:00+00:00"
        s.commit()

    from app.persistence.database import run_write_locked
    await run_write_locked(_force, label="test.force_cooldown_multi")

    creds = await cs.list_credentials(db, await _provider_id(db, first))
    assert [c.id for c in cs.available_credentials(creds)] == [second]


async def _provider_id(db, credential_id: int) -> int:
    c = await _reload(db, credential_id)
    return c.provider_id
