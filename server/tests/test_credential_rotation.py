"""plan-248-1258 单测：供应商凭据（多 Key/多账号）与代理、WorkBuddy 积分/签到解析。"""
import asyncio

from app.auth.workbuddy.credits import _extract_credits, _root_base
from app.services import credential_service


# ── 凭据服务纯逻辑（不需 DB）──

def test_is_retryable_error():
    assert credential_service.is_retryable_error("model gateway error (HTTP 401): unauthorized")
    assert credential_service.is_retryable_error("rate limit exceeded 429")
    assert credential_service.is_retryable_error("APIConnectionError")
    assert not credential_service.is_retryable_error("invalid request: unknown field")


class _Cred:
    """轻量凭据替身（只覆盖排序/过滤所需字段）。"""

    def __init__(self, cid, priority=0, is_active=True, status="ok",
                 cooldown_until=None, last_ok_at=None, api_key="k"):
        self.id = cid
        self.priority = priority
        self.is_active = is_active
        self.status = status
        self.cooldown_until = cooldown_until
        self.last_ok_at = last_ok_at
        self.api_key = api_key


def test_available_credentials_filters_inactive_and_cooldown():
    from datetime import datetime, timedelta, timezone

    future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    creds = [
        _Cred(1, priority=0, is_active=False),                       # 禁用 → 剔除
        _Cred(2, priority=1, status="cooldown", cooldown_until=future),  # 冷却中 → 剔除
        _Cred(3, priority=2, status="cooldown", cooldown_until=past),    # 冷却已过 → 保留
        _Cred(4, priority=3),                                        # 正常 → 保留
    ]
    out = credential_service.available_credentials(creds)
    assert [c.id for c in out] == [3, 4]


def test_available_credentials_sticky_prefers_last_ok():
    # 粘性：上次成功过的凭据优先，即使 priority 更大
    creds = [
        _Cred(1, priority=0),
        _Cred(2, priority=5, last_ok_at="2026-01-01T00:00:00+00:00"),
    ]
    out = credential_service.available_credentials(creds)
    assert out[0].id == 2


# ── 代理解析 ──

class _Provider:
    def __init__(self, mode, url=None, pid=1):
        self.id = pid
        self.proxy_mode = mode
        self.proxy_url = url


def test_resolve_proxy_modes(monkeypatch):
    from app.core import http_client

    monkeypatch.setattr(http_client, "get_proxy_url", lambda: "http://127.0.0.1:7897")

    # inherit / global → 全局代理
    assert credential_service.resolve_proxy(_Provider("inherit")) == "http://127.0.0.1:7897"
    assert credential_service.resolve_proxy(_Provider("global")) == "http://127.0.0.1:7897"
    # custom → 独立地址
    assert credential_service.resolve_proxy(_Provider("custom", "http://10.0.0.1:8080")) == "http://10.0.0.1:8080"
    # custom 但地址为空 → 回落全局（防误配直连失败）
    assert credential_service.resolve_proxy(_Provider("custom", "")) == "http://127.0.0.1:7897"
    # direct → 明确不走代理
    assert credential_service.resolve_proxy(_Provider("direct")) is None
    assert credential_service.proxy_disabled(_Provider("direct")) is True


# ── WorkBuddy 积分解析（对齐客户端 resources[].left 累加口径）──

def test_extract_credits_from_resources():
    body = {"code": 0, "data": {"resources": [
        {"packageCode": "a", "left": "1000.5"},
        {"packageCode": "b", "left": 2000},
    ]}}
    assert _extract_credits(body) == 3000.5


def test_extract_credits_fallback_fields():
    assert _extract_credits({"data": {"usageLeft": "5782.11"}}) == 5782.11
    assert _extract_credits({"data": {"credits": 42}}) == 42.0


def test_extract_credits_missing():
    assert _extract_credits({}) is None
    assert _extract_credits({"data": {"resources": []}}) is None
    assert _extract_credits({"data": {"resources": [{"left": "abc"}]}}) is None


def test_workbuddy_root_base_and_nested_resource_fields():
    assert _root_base("https://copilot.tencent.com/v2") == "https://copilot.tencent.com"
    assert _root_base("https://copilot.tencent.com/") == "https://copilot.tencent.com"
    body = {"code": 0, "data": {"data": {"Resources": [{"Remaining": "123.45"}]}}}
    assert _extract_credits(body) == 123.45


# ── 数据库集成：迁移建凭据 + 轮询选序 ──

def test_migrate_credentials_and_pick_order(tmp_path):
    """迁移把 provider.api_key 拆入首条凭据；轮询按 priority 取序。"""
    import os

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.persistence.database import Base
    from app.persistence.models.model_reg import Provider, ProviderCredential
    import app.persistence.models  # noqa: F401 - 触发全部模型注册

    db_file = tmp_path / "cred.db"
    url = f"sqlite+aiosqlite:///{db_file.as_posix()}"
    os.environ["DATABASE_URL"] = url  # 供 migrations 判断 sqlite
    from app.persistence import write_engine
    write_engine.configure(url)  # 写引擎指向同一临时库

    async def _run():
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with sm() as db:
            p = Provider(tenant_id=1, name="t", base_url="https://x/v1", api_key="k1",
                         api_format="openai", is_active=True, proxy_mode="custom",
                         proxy_url="http://127.0.0.1:7897")
            db.add(p)
            await db.commit()
            pid = p.id

            from app.persistence.migrations import _migrate_provider_credentials
            n = await _migrate_provider_credentials(db)
            assert n >= 1

            creds = await credential_service.list_credentials(db, pid)
            assert len(creds) == 1
            assert creds[0].api_key == "k1"

            # 再加一条低优先级 key，验证选序（priority 小的先用）
            await credential_service.create_credential(
                db, pid, label="备用", api_key="k2", priority=0, is_active=True,
            )
            from app.models.registry import _pick_credential
            chosen, key = await _pick_credential(db, p, exclude=set())
            assert chosen is not None
            assert key in ("k1", "k2")

            # 排除两条首选项后应无更多凭据
            await credential_service.create_credential(
                db, pid, label="备2", api_key="k3", priority=2, is_active=True,
            )
            allcreds = await credential_service.list_credentials(db, pid)
            ids = {c.id for c in allcreds}
            chosen2, key2 = await _pick_credential(db, p, exclude=ids)
            assert chosen2 is None and key2 is None
        await engine.dispose()

    asyncio.get_event_loop().run_until_complete(_run())


def test_mark_failed_sets_cooldown(tmp_path):
    """失败上报 → status=cooldown 且带冷却截止；成功上报 → 清冷却。"""
    import os

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.persistence.database import Base
    from app.persistence.models.model_reg import Provider, ProviderCredential
    import app.persistence.models  # noqa: F401

    db_file = tmp_path / "cred2.db"
    url = f"sqlite+aiosqlite:///{db_file.as_posix()}"
    os.environ["DATABASE_URL"] = url
    from app.persistence import write_engine
    write_engine.configure(url)

    async def _run():
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with sm() as db:
            p = Provider(tenant_id=1, name="t2", base_url="https://y/v1", api_key="kb",
                         api_format="openai", is_active=True)
            db.add(p)
            await db.commit()
            cid = await credential_service.create_credential(
                db, p.id, label="k", api_key="kb", priority=0, is_active=True,
            )
            await credential_service.mark_failed(db, cid, "HTTP 401 unauthorized")
            cred = await credential_service.get_credential(db, cid)
            assert cred.status == "cooldown"
            assert cred.cooldown_until
            assert "401" in (cred.last_error or "")

            # 冷却中的凭据被轮询跳过
            creds = await credential_service.list_credentials(db, p.id)
            assert credential_service.available_credentials(creds) == []

            await credential_service.mark_ok(db, cid)
            cred = await credential_service.get_credential(db, cid)
            assert cred.status == "ok"
            assert cred.cooldown_until is None
            assert cred.last_ok_at
        await engine.dispose()

    asyncio.get_event_loop().run_until_complete(_run())


# ── plan-271-1364：轮转策略（纯逻辑）──

def test_order_credentials_sticky_vs_round_robin():
    """轮转策略：sticky 粘性优先；round_robin 严格按 priority，不因粘性提前。"""
    creds = [
        _Cred(1, priority=0),
        _Cred(2, priority=1, last_ok_at="2026-01-01T00:00:00+00:00"),
        _Cred(3, priority=2),
    ]
    sticky = credential_service.order_credentials(creds, strategy="sticky")
    assert [c.id for c in sticky] == [2, 1, 3]

    rr = credential_service.order_credentials(creds, strategy="round_robin")
    assert [c.id for c in rr] == [1, 2, 3]


def test_order_credentials_rotate_advances_cursor():
    """rotate=True 连续取用会换起点；不带 rotate 时顺序稳定（展示路径不推进游标）。"""
    creds = [_Cred(11, priority=0), _Cred(12, priority=1), _Cred(13, priority=2)]
    first = credential_service.order_credentials(
        creds, strategy="round_robin", rotate=True, provider_id=999,
    )
    second = credential_service.order_credentials(
        creds, strategy="round_robin", rotate=True, provider_id=999,
    )
    assert first[0].id != second[0].id
    # 起点在 3 条内循环，且集合始终完整
    assert {c.id for c in first} == {11, 12, 13}

    a = credential_service.order_credentials(creds, strategy="round_robin")
    b = credential_service.order_credentials(creds, strategy="round_robin")
    assert [c.id for c in a] == [c.id for c in b] == [11, 12, 13]


def test_available_credentials_default_is_non_rotating():
    """默认调用（展示/计数路径）不轮转 —— 行为与改造前一致。"""
    creds = [_Cred(21, priority=1), _Cred(22, priority=0)]
    assert [c.id for c in credential_service.available_credentials(creds)] == [22, 21]


# ── plan-271-1364：多账号绑定 / 刷新隔离 / 删除级联（数据库集成）──

def _wb_db_env(tmp_path, name):
    """建临时库并指向写引擎，返回数据库 URL 供各用例复用。"""
    import os

    db_file = tmp_path / name
    url = f"sqlite+aiosqlite:///{db_file.as_posix()}"
    os.environ["DATABASE_URL"] = url
    from app.persistence import write_engine
    write_engine.configure(url)
    return url


def test_workbuddy_multi_account_binding_and_isolation(tmp_path):
    """多账号：各自 auth 行独立；不传 credential_id 只读旧式 provider 级行。"""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.auth.workbuddy import session as wb_session
    from app.persistence.database import Base
    from app.persistence.models.model_reg import Provider
    from app.persistence.models.workbuddy_auth import WorkBuddyAuth
    import app.persistence.models  # noqa: F401 - 触发全部模型注册

    url = _wb_db_env(tmp_path, "wb_multi.db")

    async def _run():
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with sm() as db:
            p = Provider(tenant_id=1, name="wb", base_url="https://copilot.tencent.com",
                         api_format="workbuddy", is_active=True)
            db.add(p)
            await db.commit()

            cid_a = await credential_service.create_account_credential(
                db, p.id, label="账号A", account={"uid": "ua"})
            cid_b = await credential_service.create_account_credential(
                db, p.id, label="账号B", account={"uid": "ub"})
            assert cid_a != cid_b

            await wb_session.save_auth(db, p.id, access_token="tok-a",
                                       refresh_token="ref-a", credential_id=cid_a)
            await wb_session.save_auth(db, p.id, access_token="tok-b",
                                       refresh_token="ref-b", credential_id=cid_b)

            rows = (await db.execute(select(WorkBuddyAuth))).scalars().all()
            assert len(rows) == 2

            auth_a = await wb_session.load_auth(db, p.id, cid_a)
            auth_b = await wb_session.load_auth(db, p.id, cid_b)
            assert auth_a.access_token == "tok-a"
            assert auth_b.access_token == "tok-b"
            # 未指定账号时只命中旧式 provider 级行（本用例中没有）
            assert await wb_session.load_auth(db, p.id) is None

            # 账号凭据状态应为可用（而非 create_credential 的 disabled）
            creds = await credential_service.list_credentials(db, p.id)
            assert {c.status for c in creds} == {"ok"}
        await engine.dispose()

    asyncio.get_event_loop().run_until_complete(_run())


def test_workbuddy_refresh_isolated_per_credential(tmp_path, monkeypatch):
    """刷新只改目标账号的 auth 行，另一账号不受影响（修 D3 串号）。"""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.auth.workbuddy import session as wb_session
    from app.persistence.database import Base
    from app.persistence.models.model_reg import Provider
    from app.persistence.models.workbuddy_auth import WorkBuddyAuth
    import app.persistence.models  # noqa: F401

    async def _fake_refresh(api_base, access_token, refresh_token):
        return {"accessToken": f"new-{access_token}", "refreshToken": f"new-{refresh_token}"}

    monkeypatch.setattr(wb_session, "refresh_access_token", _fake_refresh)

    url = _wb_db_env(tmp_path, "wb_refresh.db")

    async def _run():
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with sm() as db:
            p = Provider(tenant_id=1, name="wb", base_url="https://copilot.tencent.com",
                         api_format="workbuddy", is_active=True)
            db.add(p)
            await db.commit()

            cid_a = await credential_service.create_account_credential(db, p.id, label="A")
            cid_b = await credential_service.create_account_credential(db, p.id, label="B")
            await wb_session.save_auth(db, p.id, access_token="tok-a",
                                       refresh_token="ref-a", credential_id=cid_a)
            await wb_session.save_auth(db, p.id, access_token="tok-b",
                                       refresh_token="ref-b", credential_id=cid_b)

            new_token = await wb_session.refresh_session(
                db, p.id, "https://copilot.tencent.com", cid_b)
            assert new_token == "new-tok-b"

            async def _reload(cid):
                res = await db.execute(
                    select(WorkBuddyAuth)
                    .where(WorkBuddyAuth.credential_id == cid)
                    .execution_options(populate_existing=True)
                )
                return res.scalars().first()

            row_a = await _reload(cid_a)
            row_b = await _reload(cid_b)
            # A 未被刷新改动，B 已更新
            assert row_a.access_token == "tok-a"
            assert row_b.access_token == "new-tok-b"
            assert row_b.refresh_token == "new-ref-b"
        await engine.dispose()

    asyncio.get_event_loop().run_until_complete(_run())


def test_delete_credential_cascades_auth_rows(tmp_path):
    """删除凭据级联清理其 auth 行，其他账号的 auth 行保留（修 D5）。"""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.auth.workbuddy import session as wb_session
    from app.persistence.database import Base
    from app.persistence.models.model_reg import Provider
    from app.persistence.models.workbuddy_auth import WorkBuddyAuth
    import app.persistence.models  # noqa: F401

    url = _wb_db_env(tmp_path, "wb_del.db")

    async def _run():
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with sm() as db:
            p = Provider(tenant_id=1, name="wb", base_url="https://copilot.tencent.com",
                         api_format="workbuddy", is_active=True)
            db.add(p)
            await db.commit()

            cid_a = await credential_service.create_account_credential(db, p.id, label="A")
            cid_b = await credential_service.create_account_credential(db, p.id, label="B")
            await wb_session.save_auth(db, p.id, access_token="tok-a", credential_id=cid_a)
            await wb_session.save_auth(db, p.id, access_token="tok-b", credential_id=cid_b)

            ok = await credential_service.delete_credential(db, cid_a)
            assert ok is True

            rows = (await db.execute(
                select(WorkBuddyAuth).execution_options(populate_existing=True)
            )).scalars().all()
            assert [r.credential_id for r in rows] == [cid_b]
            assert rows[0].access_token == "tok-b"

            remaining = await credential_service.list_credentials(db, p.id)
            assert [c.id for c in remaining] == [cid_b]
        await engine.dispose()

    asyncio.get_event_loop().run_until_complete(_run())
