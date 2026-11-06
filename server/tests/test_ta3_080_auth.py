"""plan-20261106-ta3-080：新鉴权、入口切换与异步隔离的行为回归。"""
import asyncio
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from app.auth.ta3 import endpoints, session, oauth, catalog, quota
from app.auth.ta3.callback import CallbackServer
from app.auth.ta3.pkce import s256_challenge
from app.persistence.database import async_session_factory
from app.persistence.models.model_reg import Provider, Model, ProviderCredential
from app.persistence.models.ta3_auth import Ta3Auth
from app.persistence.migrations import _migrate_ta3_080


@pytest.fixture(autouse=True)
def clean_runtime():
    endpoints._states.clear()
    session._refresh_locks.clear()
    oauth._terminal.clear()
    quota._quota_cache.clear()
    quota._model_status_cache.clear()
    yield
    endpoints._states.clear()


async def seed(db, pid=31, base="https://example.test/ta3api", alternates=None):
    p = Provider(id=pid, name="测试 TA3", api_format="ta3", base_url=base,
                 ta3_alt_base_urls=alternates or [], proxy_mode="direct", is_active=True)
    db.add(p)
    await db.commit()
    return p


def network(monkeypatch, handler):
    real = httpx.AsyncClient
    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", factory)


@pytest.mark.parametrize("value", ["ftp://x/path", "https://user:pass@x/path", "https://x/path?q=1",
                                  "https://x/path#anchor", "http://", "https://x:invalid/a", "https://x/a b"])
def test_invalid_address(value):
    with pytest.raises(ValueError):
        endpoints.normalize_base(value)


def test_custom_address_and_front():
    p = SimpleNamespace(id=21, base_url="https://custom.test/special/", ta3_alt_base_urls=None)
    assert endpoints.candidates(p) == ["https://custom.test/special"]
    assert endpoints.front_url("https://custom.test/ta3api") == "https://custom.test/newcoder/index.html"
    assert endpoints.front_url("http://localhost:8081/custom") == "http://localhost:8080/newcoder/index.html"
    assert s256_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


async def test_fallback_and_config_isolation(monkeypatch):
    p = SimpleNamespace(id=1, base_url="https://inner.test/ta3api", ta3_alt_base_urls=["https://outer.test/api"], proxy_mode="direct")
    calls = []
    def handler(request):
        calls.append(str(request.url))
        assert "authorization" not in request.headers
        if request.url.host == "inner.test":
            raise httpx.ConnectError("DNS unavailable", request=request)
        return httpx.Response(401, json={"error": "missing_auth"})
    network(monkeypatch, handler)
    assert await endpoints.resolve(p) == "https://outer.test/api"
    assert await endpoints.resolve(p) == "https://outer.test/api"
    assert len(calls) == 2
    p.ta3_alt_base_urls = []
    assert endpoints.current_base(p) == p.base_url
    other = SimpleNamespace(id=2, base_url="https://different.test/api", ta3_alt_base_urls=[])
    assert endpoints.current_base(other) == other.base_url


@pytest.mark.parametrize("status", [401, 403, 429, 500])
async def test_http_does_not_switch(monkeypatch, status):
    p = SimpleNamespace(id=1, base_url="https://a.test/api", ta3_alt_base_urls=["https://b.test/api"], proxy_mode="direct")
    calls = []
    network(monkeypatch, lambda r: (calls.append(r.url.host) or httpx.Response(status, json={})))
    assert await endpoints.resolve(p) == p.base_url
    response = await endpoints.request(p, "GET", "/ai/v1/quota")
    assert response.status_code == status
    assert calls == ["a.test", "a.test"]


async def test_tls_error_does_not_fallback(monkeypatch):
    p = SimpleNamespace(id=1, base_url="https://a.test/api", ta3_alt_base_urls=["https://b.test/api"], proxy_mode="direct")
    calls = []
    def handler(r):
        calls.append(r.url.host)
        raise httpx.ConnectError("certificate verify failed", request=r)
    network(monkeypatch, handler)
    with pytest.raises(httpx.ConnectError):
        await endpoints.resolve(p)
    assert calls == ["a.test"]


async def test_read_fallback_but_unknown_write_never_replayed(monkeypatch):
    p = SimpleNamespace(id=1, base_url="https://a.test/api", ta3_alt_base_urls=["https://b.test/api"], proxy_mode="direct")
    endpoints.state_for(p).resolved = p.base_url
    calls = []
    def handler(r):
        calls.append((r.method, r.url.host))
        if r.url.host == "a.test":
            raise httpx.ReadTimeout("response lost", request=r)
        return httpx.Response(200, json={})
    network(monkeypatch, handler)
    with pytest.raises(httpx.ReadTimeout):
        await endpoints.request(p, "POST", "/ai/v1/quota/reset", content="windowType=WEEKLY")
    assert calls == [("POST", "a.test")]
    calls.clear()
    endpoints._states.clear()
    endpoints.state_for(p).resolved = p.base_url
    assert (await endpoints.request(p, "GET", "/ai/v1/quota")).is_success
    assert calls == [("GET", "a.test"), ("GET", "b.test")]


async def test_migration_idempotent_preserves_custom_models():
    async with async_session_factory() as db:
        p = await seed(db, base=endpoints.LEGACY_API_BASE)
        q = await seed(db, pid=32, base="https://custom.test/special")
        p.ta3_alt_base_urls = None
        db.add(Ta3Auth(id=3, provider_id=p.id, access_token="old", refresh_token="old-refresh", auth_version=None))
        db.add(Model(id=81, provider_id=p.id, name="kept-model", api_key="llm-old", source_type="byok"))
        db.add(ProviderCredential(id=9, provider_id=p.id, extra={"kind": "ta3_account"}, status="ok"))
        await db.commit()
        await _migrate_ta3_080(db)
        row = await session.load_auth(db, p.id)
        gen = row.session_generation
        await _migrate_ta3_080(db)
        assert (await session.load_auth(db, p.id)).session_generation == gen
        assert p.base_url == endpoints.DEFAULT_TA3_API_BASE
        assert p.ta3_alt_base_urls == endpoints.DEFAULT_ALT_BASES
        assert q.base_url == "https://custom.test/special"
        assert row.access_token is None and p.auth_status == "login_required"
        assert (await db.get(Model, 81, populate_existing=True)).api_key is None
        assert (await db.get(ProviderCredential, 9, populate_existing=True)).status == "error"


async def test_distinct_auth_primary_key_and_logout_blocks_late_save():
    async with async_session_factory() as db:
        p = await seed(db)
        q = await seed(db, pid=32)
        db.add(Ta3Auth(id=4, provider_id=p.id, session_generation=3, auth_version=2))
        await db.commit()
        assert await session.save_auth(db, p.id, access_token="first", refresh_token="r", expected_generation=3)
        assert await session.save_auth(db, q.id, access_token="other")
        await session.clear_auth(db, p.id)
        assert not await session.save_auth(db, p.id, access_token="late", expected_generation=3)
        assert await session.get_access_token(db, p.id) is None
        assert await session.get_access_token(db, q.id) == "other"
        assert (await session.load_auth(db, p.id)).id == 4


async def test_refresh_concurrent_401_once(monkeypatch):
    async with async_session_factory() as db:
        p = await seed(db)
        await session.save_auth(db, p.id, access_token="old", refresh_token="rotate", expires_in=3600)
    refresh = AsyncMock(return_value={"accessToken": "new", "refreshToken": "rotated", "expiresIn": 3600})
    monkeypatch.setattr(session, "refresh_access_token", refresh)
    async def attempt():
        async with async_session_factory() as own:
            return await session.ensure_token(own, 31, "https://example.test/ta3api", failed_token="old")
    assert await asyncio.gather(*[attempt() for _ in range(5)]) == ["new"] * 5
    assert refresh.await_count == 1


@pytest.mark.parametrize("kind, kept", [("network", True), ("refresh_failed", True), ("login_required", False)])
async def test_refresh_failure_semantics(monkeypatch, kind, kept):
    async with async_session_factory() as db:
        p = await seed(db)
        await session.save_auth(db, p.id, access_token="old", refresh_token="r", expires_in=60)
        monkeypatch.setattr(session, "refresh_access_token", AsyncMock(side_effect=session.Ta3AuthError("failed", kind)))
        with pytest.raises(session.Ta3AuthError):
            await session.ensure_token(db, p.id, p.base_url)
        assert bool(await session.get_access_token(db, p.id)) is kept


async def test_refresh_late_result_cannot_resurrect(monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    async def refresh(*args, **kwargs):
        started.set()
        await release.wait()
        return {"accessToken": "late", "refreshToken": "late-r", "expiresIn": 3600}
    monkeypatch.setattr(session, "refresh_access_token", refresh)
    async with async_session_factory() as db:
        p = await seed(db)
        await session.save_auth(db, p.id, access_token="old", refresh_token="r", expires_in=60)
        task = asyncio.create_task(session.ensure_token(db, p.id, p.base_url))
        await started.wait()
        async with async_session_factory() as other:
            await session.clear_auth(other, p.id)
        release.set()
        with pytest.raises(session.Ta3AuthError):
            await task
        assert await session.get_access_token(db, p.id) is None


async def test_callback_state_path_and_duplicate():
    server = await CallbackServer().start()
    server.success_base = "https://example.test/ta3api"
    try:
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            assert (await client.get(server.callback_base_url + "/wrong?code=x&state=" + server.state)).status_code == 404
            assert (await client.get(server.callback_url + "?code=x&state=wrong")).status_code == 400
            assert not server._settled
            response = await client.get(server.callback_url, params={"code": "ok", "state": server.state})
            assert response.status_code == 303
            assert response.headers["location"] == "https://example.test/ta3api/api/oauth/login-success?client=desktop&status=success"
            assert (await server.wait_result())["code"] == "ok"
            assert (await client.get(server.callback_url, params={"code": "again", "state": server.state})).status_code == 410
    finally:
        await server.stop()


async def test_browser_login_independent_db_and_cancel(monkeypatch):
    monkeypatch.setattr(oauth, "browser_entry", AsyncMock(side_effect=lambda url, *_: url))
    monkeypatch.setattr(oauth, "exchange_authorization_code", AsyncMock(return_value=oauth.LoginResult("new", "r", {"id": "user"}, 3600)))
    async with async_session_factory() as db:
        p = await seed(db)
        result = await oauth.start_login(db, p.id, p.base_url)
        duplicate = await oauth.start_login(db, p.id, p.base_url)
        assert duplicate == result
    parsed = parse_qs(urlsplit(result["authorize_url"]).query)
    assert parsed["client_id"] == ["ta3-desktop"] and parsed["code_challenge_method"] == ["S256"]
    assert parsed["redirect_uri"][0].endswith("/callback")
    attempt = oauth._attempts[31]
    attempt.server._settle({"code": "fake-code", "state": attempt.server.state})
    await attempt.task
    async with async_session_factory() as db:
        assert (await oauth.get_login_status(db, 31))["status"] == "logged_in"
        assert await session.get_access_token(db, 31) == "new"
        await oauth.start_login(db, 31, "https://example.test/ta3api")
        await oauth.cancel_login(db, 31)
        assert (await oauth.get_login_status(db, 31))["status"] == "cancelled"


async def test_catalog_post_bearer_default_fallback(monkeypatch):
    p = SimpleNamespace(id=1, base_url="https://example.test/api", ta3_alt_base_urls=[], proxy_mode="direct")
    calls = []
    def handler(r):
        calls.append(r)
        assert r.method == "POST" and r.headers["authorization"] == "Bearer session"
        assert r.headers["x-client-type"] == "app"
        if "list-organizations" in r.url.path:
            return httpx.Response(200, json=[{"id": "app-1"}])
        return httpx.Response(200, json=[] if "appId" in r.url.params else [{"id": "default"}])
    network(monkeypatch, handler)
    data = await catalog.fetch_catalog_raw(p.base_url, "session", provider=p)
    assert data["assistants_by_org"]["app-1"] == [{"id": "default"}]
    assert len(calls) == 3


async def test_quota_401_rebuild_headers_and_admin_no_sso(monkeypatch):
    async with async_session_factory() as db:
        p = await seed(db)
        await session.save_auth(db, p.id, access_token="old", refresh_token="r", account={"id": "u"})
        calls = []
        def handler(r):
            calls.append(r)
            if r.url.path.endswith("/api/oauth/token"):
                assert b"client_id=ta3-desktop" in r.content
                return httpx.Response(200, json={"access_token": "new", "refresh_token": "r2", "expires_in": 3600})
            return httpx.Response(401, json={}) if r.headers["authorization"] == "Bearer old" else httpx.Response(200, json={"ok": True})
        network(monkeypatch, handler)
        assert await quota.get_quota(db, p.id, p.base_url) == {"ok": True}
        assert [r.headers.get("authorization") for r in calls] == ["Bearer old", None, "Bearer new"]
        assert await quota.build_admin_web_url(db, p.id, p.base_url) == "https://example.test/newcoder/index.html"
        assert len(calls) == 3
