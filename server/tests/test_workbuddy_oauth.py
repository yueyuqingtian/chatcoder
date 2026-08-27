"""workbuddy 登录协议单测：auth/state 头、业务信封解包、token 轮询。"""
import httpx
import pytest

from app.auth.workbuddy.oauth import (
    _no_auth_headers,
    _unwrap,
    fetch_auth_state,
    poll_auth_token,
)
from app.auth.workbuddy.session import WorkBuddyAuthError, refresh_access_token


class TestEnvelope:
    def test_unwrap_nested_data(self):
        assert _unwrap({"code": 200, "data": {"accessToken": "t"}}) == {"accessToken": "t"}
        assert _unwrap({"data": None}) == {}
        assert _unwrap(None) == {}
        assert _unwrap({"data": [1, 2]}) == {}  # 非 dict 数据视为空

    def test_no_auth_headers(self):
        h = _no_auth_headers("https://copilot.tencent.com")
        assert h["X-No-Authorization"] == "true"
        assert h["X-No-User-Id"] == "true"
        assert h["X-Domain"] == "copilot.tencent.com"


class TestAuthState:
    def test_fetch_auth_state_parses_payload(self, monkeypatch):
        async def fake_post(self, url, **kw):
            body = {"code": 200, "data": {"authUrl": "https://login", "state": "s1"}}
            return httpx.Response(200, json=body)

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
        result = asyncio_run(fetch_auth_state("https://copilot.tencent.com"))
        assert result["auth_url"] == "https://login"
        assert result["state"] == "s1"

    def test_fetch_auth_state_error(self, monkeypatch):
        async def fake_post(self, url, **kw):
            return httpx.Response(500, json={"msg": "boom"})

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
        with pytest.raises(RuntimeError):
            asyncio_run(fetch_auth_state("https://copilot.tencent.com"))


class TestPollToken:
    def test_poll_returns_payload_when_token_present(self, monkeypatch):
        async def fake_get(self, url, **kw):
            return httpx.Response(200, json={"data": {"accessToken": "at", "refreshToken": "rt"}})

        monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
        payload = asyncio_run(poll_auth_token("https://copilot.tencent.com", "s1"))
        assert payload["accessToken"] == "at"

    def test_poll_returns_none_when_not_ready(self, monkeypatch):
        # RetryFetchToken 场景：业务失败但无 token → 返回 None 继续轮询
        async def fake_get(self, url, **kw):
            return httpx.Response(200, json={"code": 500103, "msg": "retry"})

        monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
        assert asyncio_run(poll_auth_token("https://copilot.tencent.com", "s1")) is None


class TestRefresh:
    def test_refresh_ok(self, monkeypatch):
        async def fake_post(self, url, **kw):
            assert kw["headers"]["X-Refresh-Token"] == "rt"
            assert kw["headers"]["Authorization"] == "Bearer at"
            assert kw["headers"]["X-Auth-Refresh-Source"] == "plugin"
            return httpx.Response(200, json={"data": {"accessToken": "at2", "refreshToken": "rt2"}})

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
        result = asyncio_run(refresh_access_token("https://copilot.tencent.com", "at", "rt"))
        assert result["accessToken"] == "at2"
        assert result["refreshToken"] == "rt2"

    def test_refresh_invalid_grant(self, monkeypatch):
        async def fake_post(self, url, **kw):
            return httpx.Response(401, json={"code": 401})

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
        with pytest.raises(WorkBuddyAuthError) as exc:
            asyncio_run(refresh_access_token("https://copilot.tencent.com", "at", "rt"))
        assert exc.value.kind == "login_required"


def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)
