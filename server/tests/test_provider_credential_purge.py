"""plan-81-345 回归测试：供应商删除/新建时的凭据级联清理。

用户场景：聚焦供应商 A → 删除 → 新增供应商 B，B 名下出现两个 Key
（A 残留的旧 Key + 新建时填写的 Key）。

根因链：
1. 删除供应商时漏删 provider_credentials（该表无外键约束，DB 层不级联）；
2. SQLite 会复用被删供应商的 id（rowid 复用），孤儿凭据被误挂到新供应商上。
"""
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.persistence.database import Base
from app.persistence.models.model_reg import ProviderCredential
from app.persistence.models.workbuddy_auth import WorkBuddyAuth
from app.services import credential_service, provider_service


@pytest.fixture
async def db(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path}/provider_cred.db"
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


async def test_delete_provider_purges_credentials_and_auth_rows(db):
    """删除供应商必须级联清掉其凭据与 OAuth 登录态行，不留孤儿。"""
    pid = await provider_service.create_provider(
        db, name="A", base_url="https://a.example/v1", api_format="workbuddy",
    )
    cid = await credential_service.create_account_credential(db, pid, label="账号1")
    db.add(WorkBuddyAuth(provider_id=pid, credential_id=cid, access_token="t", account={}))
    await db.commit()

    assert await provider_service.delete_provider(db, pid) is True

    creds = (await db.execute(
        select(ProviderCredential).where(ProviderCredential.provider_id == pid)
    )).scalars().all()
    assert creds == []
    auths = (await db.execute(
        select(WorkBuddyAuth).where(WorkBuddyAuth.provider_id == pid)
    )).scalars().all()
    assert auths == []


async def test_create_provider_purges_legacy_orphan_credentials(db):
    """历史脏数据自愈：新建供应商时清掉 provider_id 已不存在的孤儿凭据。"""
    orphan = ProviderCredential(
        provider_id=999, label="旧 Key", api_key="sk-old",
        priority=0, is_active=True, status="ok",
    )
    db.add(orphan)
    await db.flush()
    db.add(WorkBuddyAuth(provider_id=999, credential_id=orphan.id, access_token="t", account={}))
    await db.commit()

    pid = await provider_service.create_provider(
        db, name="B", base_url="https://b.example/v1", api_format="openai",
    )

    leftovers = (await db.execute(select(ProviderCredential))).scalars().all()
    assert leftovers == []
    auths = (await db.execute(select(WorkBuddyAuth))).scalars().all()
    assert auths == []
    # 新建的供应商名下不应出现任何残留 Key
    own = (await db.execute(
        select(ProviderCredential).where(ProviderCredential.provider_id == pid)
    )).scalars().all()
    assert own == []


async def test_delete_then_recreate_provider_keeps_single_key(db):
    """端到端复现用户操作序列：删除再新建（id 被 SQLite 复用）后只有一个 Key。"""
    a_id = await provider_service.create_provider(
        db, name="A", base_url="https://a.example/v1", api_format="openai",
    )
    await credential_service.create_credential(db, a_id, label="旧 Key", api_key="sk-old")
    assert await provider_service.delete_provider(db, a_id) is True

    b_id = await provider_service.create_provider(
        db, name="B", base_url="https://b.example/v1", api_format="openai",
    )
    # SQLite 复用被删供应商的 id（rowid）正是问题放大的条件，此处顺带断言
    assert b_id == a_id
    await credential_service.create_credential(db, b_id, label="新 Key", api_key="sk-new")

    creds = (await db.execute(
        select(ProviderCredential).where(ProviderCredential.provider_id == b_id)
    )).scalars().all()
    assert sorted(c.api_key for c in creds) == ["sk-new"]
