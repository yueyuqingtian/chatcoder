"""TRAE 登录协议单测：授权 URL 构造、ExchangeToken 信封解包、错误码、GetUserInfo。"""
import httpx
import pytest

from app.auth.trae import callback as trae_callback
from app.auth.trae import device as trae_device
from app.auth.trae.oauth import (
    TraeAuthError,
    _unwrap_result,
    build_authorize_url,
    check_login,
    exchange_auth_code,
    fetch_user_info,
    revoke_refresh_token,
)


def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)


class TestAuthorizeUrl:
    def _params(self, url: str) -> dict:
        from urllib.parse import parse_qs, urlparse

        return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}

    def test_build_authorize_url(self):
        server = trae_callback.CallbackServer()
        server.port = 45678
        url, verifier = asyncio_run(build_authorize_url(
            server, client_id="en1oxy7wnw8j9n", console_host="https://www.trae.cn",
            ide_version="0.1.51", machine_id="m" * 64, device_id="12345"))
        assert url.startswith("https://www.trae.cn/authorization?")
        params = self._params(url)
        assert params["auth_from"] == "solo"
        assert params["client_id"] == "en1oxy7wnw8j9n"
        assert params["auth_callback_url"] == server.authorize_url
        assert params["code_challenge_method"] == "S256"
        assert params["hide_saas_login"] == "true"
        assert params["code_challenge"]  # 与 verifier 配套（一致性由 pkce 单测覆盖）
        assert verifier

    def test_authorize_url_callback_host(self):
        server = trae_callback.CallbackServer()
        server.port = 40000
        url, _ = asyncio_run(build_authorize_url(
            server, client_id="c", console_host="https://www.trae.cn",
            ide_version="0.1.51", machine_id="m", device_id="0"))
        assert self._params(url)["auth_callback_url"] == "http://127.0.0.1:40000/authorize"


class TestExchange:
    def test_exchange_auth_code_parses_result(self, monkeypatch):
        # TokenExpireAt 为毫秒级（真实 TRAE 响应），含 token 过期时间正确解析
        body = {
            "ResponseMetadata": {"Error": {}},
            "Result": {
                "Token": "eyJhbGciOiJIUzI1NiJ9.abc",
                "RefreshToken": "rt-1",
                "TokenExpireAt": 1786500000000,
                "RefreshExpireAt": 1787000000000,
                "UserID": "3704722231404809",
                "ScreenName": "用户7828272569",
                "AIRegion": "cn",
                "StoreCountry": "CN",
            },
        }

        async def fake_post(self, url, **kw):
            # 首次 ExchangeToken（AuthCode 模式）x-cloudide-token 必须为空
            assert kw["headers"]["x-cloudide-token"] == ""
            assert kw["json"]["ClientID"] == "en1oxy7wnw8j9n"
            assert "AuthCode" in kw["json"] and kw["json"]["AuthCode"] == "code-1"
            assert "CodeVerifier" in kw["json"]
            assert "DeviceInfo" in kw["json"]
            return httpx.Response(200, json=body)

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
        kp = trae_device.gen_device_keypair()
        result = asyncio_run(exchange_auth_code(
            "https://api.trae.cn", "en1oxy7wnw8j9n", "code-1", "verifier", kp,
            device_id="123", machine_id="m", ide_version="0.1.51"))
        assert result.access_token.startswith("eyJ")
        assert result.refresh_token == "rt-1"
        assert result.account["user_id"] == "3704722231404809"
        assert result.account["region"] == "cn"
        # 毫秒时间戳解析为 ISO，不抛 errno 22
        assert result.token_expires_at.startswith("2026-08-12T02:00:00")

    def test_exchange_only_duration_falls_back_relative(self, monkeypatch):
        """无 TokenExpireAt 时用 TokenExpireDuration（相对毫秒时长）。"""
        body = {
            "ResponseMetadata": {"Error": {}},
            "Result": {
                "Token": "jwt-1",
                "RefreshToken": "rt-2",
                "TokenExpireDuration": 604800000,  # 7 天
            },
        }

        async def fake_post(self, url, **kw):
            return httpx.Response(200, json=body)

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
        kp = trae_device.gen_device_keypair()
        result = asyncio_run(exchange_auth_code(
            "https://api.trae.cn", "c", "code", "v", kp,
            device_id="d", machine_id="m", ide_version="0.1.51"))
        import datetime as _dt

        exp = _dt.datetime.fromisoformat(result.token_expires_at)
        diff = exp - _dt.datetime.now(_dt.timezone.utc)
        assert _dt.timedelta(days=6.9) < diff < _dt.timedelta(days=7.1)

    def test_exchange_missing_token_raises(self, monkeypatch):
        async def fake_post(self, url, **kw):
            return httpx.Response(200, json={"ResponseMetadata": {"Error": {}}, "Result": {}})

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
        kp = trae_device.gen_device_keypair()
        with pytest.raises(TraeAuthError):
            asyncio_run(exchange_auth_code(
                "https://api.trae.cn", "c", "code", "v", kp,
                device_id="d", machine_id="m", ide_version="0.1.51"))


class TestEnvelope:
    def test_unwrap_result_ok(self):
        resp = httpx.Response(200, json={"ResponseMetadata": {"Error": {}},
                                         "Result": {"Token": "t"}})
        assert _unwrap_result(resp, "u")["Token"] == "t"

    def test_unwrap_result_business_error_code(self):
        resp = httpx.Response(200, json={"ResponseMetadata": {"Error": {"Code": "20324"}}})
        with pytest.raises(TraeAuthError) as exc:
            _unwrap_result(resp, "u")
        assert exc.value.kind == "login_required"
        assert exc.value.code == "20324"

    def test_unwrap_result_unknown_code(self):
        resp = httpx.Response(200, json={"ResponseMetadata": {"Error": {"Code": "99999"}}})
        with pytest.raises(TraeAuthError) as exc:
            _unwrap_result(resp, "u")
        assert exc.value.kind == "refresh_failed"

    def test_unwrap_result_missing_result(self):
        resp = httpx.Response(200, json={"ResponseMetadata": {"Error": {}}})
        with pytest.raises(TraeAuthError):
            _unwrap_result(resp, "u")


class TestUserInfoAndLogin:
    def test_fetch_user_info_headers(self, monkeypatch):
        async def fake_post(self, url, **kw):
            assert kw["headers"]["x-cloudide-token"] == "jwt-token"
            assert kw["json"] == {"ReqSource": "Lite", "IDEVersion": "0.1.51"}
            return httpx.Response(200, json={"ResponseMetadata": {"Error": {}},
                                             "Result": {"UserID": "u1"}})

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
        result = asyncio_run(fetch_user_info("https://api.trae.cn", "jwt-token", "0.1.51"))
        assert result["UserID"] == "u1"

    def test_check_login_true(self, monkeypatch):
        async def fake_post(self, url, **kw):
            return httpx.Response(200, json={"ResponseMetadata": {"Error": {}},
                                             "Result": {"IsLogin": True}})

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
        assert asyncio_run(check_login("https://api.trae.cn", "t", "0.1.51")) is True

    def test_check_login_false(self, monkeypatch):
        async def fake_post(self, url, **kw):
            return httpx.Response(200, json={"ResponseMetadata": {"Error": {}},
                                             "Result": {"IsLogin": False}})

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
        assert asyncio_run(check_login("https://api.trae.cn", "t", "0.1.51")) is False

    def test_revoke_refresh_token(self, monkeypatch):
        captured = {}

        async def fake_post(self, url, **kw):
            captured["url"] = url
            captured["json"] = kw["json"]
            return httpx.Response(200, json={})

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
        asyncio_run(revoke_refresh_token(
            "https://api.trae.cn", "jwt", client_id="c", device_id="d", machine_id="m"))
        assert captured["url"].endswith("/cloudide/api/v3/trae/oauth/ClearRefreshToken")
        assert captured["json"]["ClientID"] == "c"
        assert captured["json"]["PlatformCode"] == "SOLO_PC"
