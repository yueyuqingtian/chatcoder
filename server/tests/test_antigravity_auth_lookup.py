"""antigravity 登录态查找回落回归测试。

背景（实测故障）：多账号改造后登录态行都带 `credential_id`，而按 provider 级
（`credential_id IS NULL`）查的路径会读不到——表现为「明明已登录，配额刷新与
扫描模型都报『请先导入 Antigravity 登录态』」。

`load_auth` 保持精确语义（写路径依赖它定位同一行），读取路径一律走
`resolve_auth`：未指定凭据时回落到该供应商下第一条有 access_token 的行。
"""
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.auth.antigravity import session as ag_session
from app.persistence.database import Base
from app.persistence.models.antigravity_auth import AntigravityAuth
from app.persistence.models.model_reg import Provider, ProviderCredential


@pytest.fixture
async def db(tmp_path):
    """隔离库：与 test_credential_toggle 同一口径（写引擎 + 异步读引擎同库）。"""
    db_url = f"sqlite+aiosqlite:///{tmp_path}/ag_lookup.db"
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


async def _make_provider(db) -> int:
    provider = Provider(
        tenant_id=1, name="ag", api_format="antigravity",
        base_url="https://daily-cloudcode-pa.googleapis.com",
    )
    db.add(provider)
    await db.flush()
    return provider.id


async def _make_credential(db, provider_id: int, label: str = "a@b.com") -> int:
    cred = ProviderCredential(
        provider_id=provider_id, label=label, priority=0, is_active=True,
        status="ok", extra={"kind": "antigravity_account", "account_key": label},
    )
    db.add(cred)
    await db.flush()
    return cred.id


async def _make_auth(db, provider_id: int, credential_id: int | None, *,
                     token: str = "at-1", refresh: str | None = "rt-1",
                     account: dict | None = None) -> int:
    row = AntigravityAuth(
        provider_id=provider_id,
        credential_id=credential_id,
        access_token=token,
        refresh_token=refresh,
        auth_kind="oauth",
        project="aicode-consumers",
        tier="free-tier",
        account=account or {"email": "a@b.com", "sub": "u1", "label": "a@b.com"},
        expires_at="2099-01-01T00:00:00+00:00",
    )
    db.add(row)
    await db.commit()
    return row.id


class TestResolveAuth:
    async def test_credential_scoped_row_is_found(self, db):
        """核心回归：行带 credential_id 时，未指定凭据也必须能读到。"""
        pid = await _make_provider(db)
        cid = await _make_credential(db, pid)
        await _make_auth(db, pid, cid)

        row = await ag_session.resolve_auth(db, pid)
        assert row is not None
        assert row.access_token == "at-1"
        assert row.credential_id == cid

    async def test_load_auth_stays_exact(self, db):
        """load_auth 保持精确语义：只查 provider 级行，不回落。

        这正是故障的成因——所以读取路径必须走 resolve_auth 而不是 load_auth。
        """
        pid = await _make_provider(db)
        cid = await _make_credential(db, pid)
        await _make_auth(db, pid, cid)

        assert await ag_session.load_auth(db, pid) is None
        assert await ag_session.load_auth(db, pid, cid) is not None

    async def test_legacy_provider_row_still_wins(self, db):
        """迁移前的 provider 级旧行优先（不改变老数据行为）。"""
        pid = await _make_provider(db)
        cid = await _make_credential(db, pid, "new@b.com")
        await _make_auth(db, pid, None, token="legacy-at",
                         account={"email": "old@b.com", "sub": "u0"})
        await _make_auth(db, pid, cid, token="new-at")

        row = await ag_session.resolve_auth(db, pid)
        assert row is not None
        assert row.access_token == "legacy-at"

    async def test_explicit_credential_is_respected(self, db):
        """指定凭据时只认该行，不回落——否则多账号会串号。"""
        pid = await _make_provider(db)
        c1 = await _make_credential(db, pid, "one@b.com")
        c2 = await _make_credential(db, pid, "two@b.com")
        await _make_auth(db, pid, c1, token="at-one")
        await _make_auth(db, pid, c2, token="at-two")

        row = await ag_session.resolve_auth(db, pid, c2)
        assert row is not None and row.access_token == "at-two"
        # 不存在的凭据 id 不得回落到别人的行
        assert await ag_session.resolve_auth(db, pid, 99999) is None

    async def test_row_without_token_skipped(self, db):
        """无 access_token 的残行不能当作「已登录」。"""
        pid = await _make_provider(db)
        c1 = await _make_credential(db, pid, "empty@b.com")
        c2 = await _make_credential(db, pid, "ok@b.com")
        await _make_auth(db, pid, c1, token="")
        await _make_auth(db, pid, c2, token="at-ok")

        row = await ag_session.resolve_auth(db, pid)
        assert row is not None and row.access_token == "at-ok"

    async def test_no_auth_row_returns_none(self, db):
        pid = await _make_provider(db)
        assert await ag_session.resolve_auth(db, pid) is None

    async def test_other_provider_isolated(self, db):
        """不同供应商的登录态互不可见。"""
        pid = await _make_provider(db)
        other = Provider(tenant_id=1, name="ag2", api_format="antigravity")
        db.add(other)
        await db.flush()
        cid = await _make_credential(db, other.id)
        await _make_auth(db, other.id, cid)

        assert await ag_session.resolve_auth(db, pid) is None
        assert await ag_session.resolve_auth(db, other.id) is not None


class TestEnsureTokenFallback:
    async def test_ensure_token_reads_credential_scoped_row(self, db):
        """故障复现点：配额/同步走 ensure_token(credential_id=None) 必须成功。"""
        pid = await _make_provider(db)
        cid = await _make_credential(db, pid)
        await _make_auth(db, pid, cid)

        token = await ag_session.ensure_token(db, pid)
        assert token == "at-1"

    async def test_ensure_token_without_auth_raises_login_required(self, db):
        pid = await _make_provider(db)
        with pytest.raises(ag_session.AntigravityAuthError) as err:
            await ag_session.ensure_token(db, pid)
        assert err.value.kind == "login_required"

    async def test_ensure_token_expired_refreshes_same_row(self, db, monkeypatch):
        """过期行会刷新，且刷新结果写回它自己的 credential_id 行（不能落到 provider 级）。"""
        pid = await _make_provider(db)
        cid = await _make_credential(db, pid)
        row_id = await _make_auth(db, pid, cid)
        # 置为已过期
        row = await db.get(AntigravityAuth, row_id)
        row.expires_at = "2000-01-01T00:00:00+00:00"
        await db.commit()

        async def fake_refresh(endpoints, refresh_token, *, client_kwargs=None):
            assert refresh_token == "rt-1"
            return {"accessToken": "at-refreshed", "expiresIn": 3600}

        monkeypatch.setattr(ag_session, "refresh_access_token", fake_refresh)

        token = await ag_session.ensure_token(db, pid)
        assert token == "at-refreshed"

        # 写回同一行：行内 token 更新，且仍是该凭据
        fresh = await ag_session.load_auth(db, pid, cid)
        assert fresh is not None
        assert fresh.access_token == "at-refreshed"
        # provider 级行仍为空（没有被误建/误写）
        assert await ag_session.load_auth(db, pid) is None
