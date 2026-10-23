"""v47 回归：ta3 额度耗尽把凭据打进冷却后，不能卡在本地直接报「无可用凭据」。

现场现象：ta3 + kimi-k3 这类高消耗模型很快打满日额度 → 凭据被置 cooldown →
ta3 的选取路径对冷却行是**硬过滤**，于是每条消息都在本地返回
provider_credential_unavailable（请求根本没发出去）；即便去设置里刷新额度也无效，
因为刷新只清了额度展示缓存，凭据行的 status=cooldown 原样保留。

本用例锁定两条修复语义：
1. 冷却只是"暂缓"：全部凭据都在冷却时仍会取出一条去真正调远端（由远端判定额度），
   但已经试过的凭据（exclude）不会再被兜底选中，避免死循环撞同一个 Key；
2. 额度恢复动作（刷新 / 透支 / 重置）成功后，该供应商的凭据冷却被一并解除。
"""
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.persistence.database import Base
from app.persistence.models.model_reg import Provider, ProviderCredential
from app.persistence.models.ta3_auth import Ta3Auth
from app.services import credential_service as cs


@pytest.fixture
async def db(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path}/ta3_cool.db"
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


async def _make_ta3_provider(db, n_creds: int) -> tuple[Provider, list[int]]:
    provider = Provider(tenant_id=1, name="ta3-测试", api_format="ta3",
                        base_url="https://lc.example/newcoder", is_active=True)
    db.add(provider)
    await db.flush()
    ids = []
    for i in range(n_creds):
        cred = ProviderCredential(provider_id=provider.id, label=f"acct{i}",
                                  priority=i, is_active=True, status="ok")
        db.add(cred)
        await db.flush()
        # ta3 的 key 来自 ta3_auth.access_token（凭据本身没有 api_key），
        # 每个账号各挂一行登录态，才能走通 _pick_credential 的取值分支。
        db.add(Ta3Auth(provider_id=provider.id, credential_id=cred.id,
                       access_token=f"llm-test-{i}", account={"id": f"acct{i}"}))
        ids.append(cred.id)
    await db.commit()
    return provider, ids


async def _force_cooldown(db, credential_id: int) -> None:
    """把凭据强制置为冷却（模拟额度耗尽后的上报结果）。"""
    from datetime import datetime, timedelta, timezone

    until = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()

    def patch(s):
        cred = s.get(ProviderCredential, credential_id)
        cred.status = "cooldown"
        cred.cooldown_until = until
        cred.fail_count = 3
        s.commit()

    from app.persistence.database import run_write_locked

    await run_write_locked(patch, label="test.ta3.force_cooldown")


async def _reload(db, cid: int) -> ProviderCredential:
    row = await cs.get_credential(db, cid)
    assert row is not None
    return row


async def test_all_credentials_cooled_still_picks_one_for_ta3(db):
    """ta3：全部凭据冷却时仍返回一条去真正调用，不再本地直接拒绝。"""
    from app.models.registry import _pick_credential

    provider, ids = await _make_ta3_provider(db, 2)
    for cid in ids:
        await _force_cooldown(db, cid)

    chosen, key = await _pick_credential(db, provider, exclude=set())
    assert chosen is not None, "冷却不应等同于永久不可用：仍要发出请求由远端判定额度"
    assert chosen.id in ids
    # ta3 的 key 来自 ta3_auth 行（本用例未建账号），因此这里 key 允许为空
    assert key is None or isinstance(key, str)


async def test_excluded_credentials_are_not_used_as_fallback(db):
    """兜底不能把 exclude 里的凭据重新捡回来，否则重试会反复撞同一个 Key。"""
    from app.models.registry import _pick_credential

    provider, ids = await _make_ta3_provider(db, 2)
    for cid in ids:
        await _force_cooldown(db, cid)

    chosen, _ = await _pick_credential(db, provider, exclude=set(ids[:1]))
    assert chosen is not None and chosen.id == ids[1]

    chosen_all, _ = await _pick_credential(db, provider, exclude=set(ids))
    assert chosen_all is None, "全部凭据都已尝试过时应结束轮询"


async def test_refresh_revives_cooled_credentials(db):
    """刷新额度（额度恢复动作）后，凭据冷却必须一并解除。"""
    provider, ids = await _make_ta3_provider(db, 2)
    for cid in ids:
        await _force_cooldown(db, cid)

    revived = await cs.revive_provider_credentials_by_id(db, provider.id)
    assert revived == len(ids)

    for cid in ids:
        cred = await _reload(db, cid)
        assert cred.status == "ok"
        assert cred.cooldown_until is None
        assert cred.fail_count == 0


async def test_revive_does_not_touch_healthy_credentials(db):
    """本来就正常的凭据不应被计入「解除了几条冷却」。"""
    provider, ids = await _make_ta3_provider(db, 2)
    await _force_cooldown(db, ids[0])

    revived = await cs.revive_provider_credentials_by_id(db, provider.id)
    assert revived == 1

    healthy = await _reload(db, ids[1])
    assert healthy.status == "ok"
    # 正常凭据的连续失败计数不该被顺手改写
    assert healthy.fail_count == 0


async def test_inactive_credentials_stay_out_of_fallback(db):
    """兜底只针对"冷却"，停用的凭据依旧不可用（用户停用是永久意图）。"""
    from app.models.registry import _pick_credential

    provider, ids = await _make_ta3_provider(db, 2)
    for cid in ids:
        await _force_cooldown(db, cid)

    def disable(s):
        cred = s.get(ProviderCredential, ids[0])
        cred.is_active = False
        s.commit()

    from app.persistence.database import run_write_locked

    await run_write_locked(disable, label="test.ta3.disable")

    chosen, _ = await _pick_credential(db, provider, exclude=set())
    assert chosen is not None and chosen.id == ids[1]
