"""kiro 凭据两处硬编码元组遗漏的回归测试（历史遗漏，与本轮供应商接入无关）。

缺陷一：`credential_service._purge_auth_rows` 的清理元组漏了 KiroAuth——
删除 kiro 账号后 `kiro_auth` 行残留成孤儿，`_load_auth_for_credential` 的
legacy 回落可能把旧账号的 token 挂到新凭据上。

缺陷二：`provider_service._resolve_scan_key` 的 OAuth 分支漏了 "kiro"——
「测试连接」/「扫描模型」取不到登录态 access_token，只剩空的 provider.api_key。
"""
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.persistence.database import Base
from app.persistence.models.kiro_auth import KiroAuth
from app.persistence.models.model_reg import Provider, ProviderCredential
from app.services import credential_service, provider_service


@pytest.fixture
async def db(tmp_path):
    """隔离库：写引擎（同步单写线程）与异步读引擎连同一个文件，口径同 test_credential_toggle。"""
    db_url = f"sqlite+aiosqlite:///{tmp_path}/kiro_auth.db"
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


async def _kiro_provider(db, name: str) -> int:
    return await provider_service.create_provider(
        db, name=name, base_url="https://kiro.example/v1", api_format="kiro",
    )


# ── 缺陷一：删除凭据要清掉 kiro_auth 行 ──

async def test_delete_credential_purges_kiro_auth_row(db):
    """删 kiro 账号后该凭据的登录态行必须清掉，其它账号 / 其它供应商的行不受影响。"""
    pid_a = await _kiro_provider(db, "kiro-A")
    cid_a1 = await credential_service.create_account_credential(db, pid_a, label="账号1")
    cid_a2 = await credential_service.create_account_credential(db, pid_a, label="账号2")
    pid_b = await _kiro_provider(db, "kiro-B")
    cid_b1 = await credential_service.create_account_credential(db, pid_b, label="账号3")

    db.add_all([
        KiroAuth(provider_id=pid_a, credential_id=cid_a1, access_token="token-a1"),
        KiroAuth(provider_id=pid_a, credential_id=cid_a2, access_token="token-a2"),
        KiroAuth(provider_id=pid_b, credential_id=cid_b1, access_token="token-b1"),
    ])
    await db.commit()

    assert await credential_service.delete_credential(db, cid_a1) is True
    # 删除走的是写引擎的另一个会话，本会话要丢掉身份映射里的旧对象才能读到真相
    db.expire_all()

    left_auth = (await db.execute(select(KiroAuth.credential_id))).scalars().all()
    assert sorted(left_auth) == sorted([cid_a2, cid_b1]), "被删凭据的 kiro_auth 行必须消失"
    left_creds = (await db.execute(select(ProviderCredential.id))).scalars().all()
    assert sorted(left_creds) == sorted([cid_a2, cid_b1]), "只应删掉目标凭据本身"


# ── 缺陷二：测试连接要能从 kiro_auth 取到 access_token ──

async def test_resolve_scan_key_reads_kiro_auth_access_token(db):
    """凭据没有 api_key 时，kiro 供应商的扫描/测试 key 要来自 kiro_auth.access_token。"""
    pid = await _kiro_provider(db, "kiro-C")
    cid = await credential_service.create_account_credential(db, pid, label="账号1")
    db.add(KiroAuth(provider_id=pid, credential_id=cid, access_token="kiro-access-token"))
    await db.commit()

    provider = await db.get(Provider, pid)
    key = await provider_service._resolve_scan_key(db, provider)

    assert key == "kiro-access-token", "kiro 未纳入 OAuth 分支时这里只能拿到 provider.api_key（None）"


async def test_resolve_scan_key_prefers_credential_api_key(db):
    """对照：凭据自带 api_key 时仍优先用它，不受本次改动影响。"""
    pid = await _kiro_provider(db, "kiro-D")
    cid = await credential_service.create_credential(db, pid, label="Key", api_key="sk-kiro")
    db.add(KiroAuth(provider_id=pid, credential_id=cid, access_token="kiro-access-token"))
    await db.commit()

    provider = await db.get(Provider, pid)
    assert await provider_service._resolve_scan_key(db, provider) == "sk-kiro"
