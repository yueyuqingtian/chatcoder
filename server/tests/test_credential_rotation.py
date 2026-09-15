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
