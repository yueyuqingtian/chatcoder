"""antigravity 鉴权单测：请求头 / 枚举 / 本机登录态识别 / 档位解析 / 模型目录分类。

方案 docs/plan-antigravity-provider-integration.md §1.3-1.5、§2.3。
不发网络请求，全部走纯函数与 monkeypatch。
"""
import json

import pytest

from app.auth.antigravity import catalog, token
from app.auth.antigravity.oauth import (
    _expiry_from_ms,
    _is_expired,
    _pick_project,
    load_local_cred,
    load_local_profile,
)


class TestHeaders:
    def test_headers_include_required_spoofing(self):
        """实测缺 X-Goog-Api-Client 或 Client-Metadata 会被拒。"""
        headers = token.build_headers("tok-abc", stream=True)
        assert headers["Authorization"] == "Bearer tok-abc"
        assert headers["Accept"] == "text/event-stream"
        assert headers["X-Goog-Api-Client"]
        assert "ANTIGRAVITY" in headers["Client-Metadata"]
        assert headers["User-Agent"].startswith("antigravity/")

    def test_non_stream_has_no_accept(self):
        headers = token.build_headers("tok")
        assert "Accept" not in headers

    def test_client_metadata_platform_is_arch_qualified(self):
        """坑点 2：platform 必须是 WINDOWS_AMD64 这类带架构的值。"""
        meta = token.client_metadata()
        assert meta["platform"] == "WINDOWS_AMD64"
        assert meta["ideType"] == "ANTIGRAVITY"
        assert meta["pluginType"] == "GEMINI"


class TestEndpoints:
    def test_default_endpoints(self):
        endpoints = token.resolve_endpoints(None)
        assert endpoints.gateway.startswith("https://")
        assert endpoints.token.endswith("/token")

    def test_provider_base_url_overrides_gateway_only(self):
        """供应商 base_url 只覆盖网关，OAuth 端点始终走官方。"""
        endpoints = token.resolve_endpoints("https://sandbox.example.com/")
        assert endpoints.gateway == "https://sandbox.example.com"
        assert endpoints.token == token.resolve_endpoints(None).token


class TestIdToken:
    def test_decode_and_account(self):
        import base64

        payload = {"email": "a@b.com", "sub": "123", "name": "Yu"}
        seg = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        id_token = f"header.{seg}.sig"
        parsed = token.decode_id_token(id_token)
        assert parsed["email"] == "a@b.com"
        account = token.build_account(id_token)
        assert account["label"] == "a@b.com"
        assert account["sub"] == "123"

    def test_malformed_token_returns_empty(self):
        assert token.decode_id_token("not-a-jwt") == {}
        assert token.decode_id_token(None) == {}
        assert token.build_account(None)["label"] == "Antigravity 账号"


class TestLocalCred:
    def test_missing_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        assert load_local_cred() is None

    def test_reads_oauth_creds(self, tmp_path, monkeypatch):
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        gemini = tmp_path / ".gemini"
        gemini.mkdir()
        (gemini / "oauth_creds.json").write_text(json.dumps({
            "access_token": "at", "refresh_token": "rt", "expiry_date": 1795243715000,
            "scope": "openid", "id_token": "x.y.z",
        }), encoding="utf-8")
        cred = load_local_cred()
        assert cred is not None
        assert cred["access_token"] == "at"
        assert cred["_source"].endswith("oauth_creds.json")

    def test_incomplete_file_skipped(self, tmp_path, monkeypatch):
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        gemini = tmp_path / ".gemini"
        gemini.mkdir()
        (gemini / "oauth_creds.json").write_text('{"refresh_token": "rt"}', encoding="utf-8")
        assert load_local_cred() is None

    def test_broken_json_skipped(self, tmp_path, monkeypatch):
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        gemini = tmp_path / ".gemini"
        gemini.mkdir()
        (gemini / "oauth_creds.json").write_text("{not json", encoding="utf-8")
        assert load_local_cred() is None

    def test_local_profile_reads_active(self, tmp_path, monkeypatch):
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        gemini = tmp_path / ".gemini"
        gemini.mkdir()
        (gemini / "google_accounts.json").write_text(
            json.dumps({"active": "u@gmail.com", "old": []}), encoding="utf-8")
        assert load_local_profile()["active"] == "u@gmail.com"


class TestExpiry:
    def test_expiry_from_ms(self):
        iso = _expiry_from_ms(1795243715000)
        assert iso is not None and iso.startswith("2026-")
        assert _expiry_from_ms(None) is None
        assert _expiry_from_ms("bad") is None
        # 超范围时间戳返回 None，不抛异常
        assert _expiry_from_ms(99999999999999) is None

    def test_expired_detection(self):
        assert _is_expired(1000) is True  # 1970 年，必然过期
        assert _is_expired(None) is True   # 无法判定按过期处理
        assert _is_expired("bad") is True
        # 超出可表示范围的时间戳不能抛异常，按过期处理（防损坏数据中断登录）
        assert _is_expired(99999999999999) is True
        assert _is_expired(-1) is True

    def test_future_timestamp_not_expired(self):
        from datetime import datetime, timedelta, timezone

        future = datetime.now(timezone.utc) + timedelta(hours=1)
        assert _is_expired(int(future.timestamp() * 1000)) is False


class TestPickProject:
    def test_uses_cloudaicompanion_project(self):
        project, tier = _pick_project({
            "currentTier": {"id": "free-tier"},
            "cloudaicompanionProject": "aicode-consumers",
        })
        assert project == "aicode-consumers"
        assert tier == "free-tier"

    def test_falls_back_to_default_and_default_tier(self):
        project, tier = _pick_project({
            "allowedTiers": [{"id": "free-tier", "isDefault": True}],
        })
        assert project
        assert tier == "free-tier"

    def test_empty_payload(self):
        project, tier = _pick_project(None)
        assert project
        assert tier == ""


class TestCatalog:
    def test_builtin_models_excludes_completion(self):
        assert not any(m.startswith("tab_") for m in catalog.BUILTIN_MODELS)

    def test_parse_classifies_by_response_fields(self):
        """坑点 7：分类依据响应自带字段，不靠名字猜。"""
        parsed = catalog.parse_available_models({
            "models": {
                "gemini-3-flash": {},
                "claude-sonnet-5-5-medium": {},
                "gemini-3.1-flash-image": {},
                "tab_flash_lite_preview": {},
                "tab_jump_flash_lite_preview": {},
            },
            "imageGenerationModelIds": ["gemini-3.1-flash-image"],
        })
        assert "gemini-3-flash" in parsed["chat"]
        assert "claude-sonnet-5-5-medium" in parsed["chat"]
        assert parsed["image"] == ["gemini-3.1-flash-image"]
        assert "tab_flash_lite_preview" in parsed["completion"]
        # 图像与补全模型不得进对话列表
        assert "gemini-3.1-flash-image" not in parsed["chat"]
        assert "tab_flash_lite_preview" not in parsed["chat"]

    def test_parse_handles_list_shape(self):
        parsed = catalog.parse_available_models({
            "models": [{"modelId": "gemini-3-flash"}, {"modelId": "tab_x"}],
        })
        assert parsed["chat"] == ["gemini-3-flash"]
        assert parsed["completion"] == ["tab_x"]

    def test_parse_empty(self):
        parsed = catalog.parse_available_models(None)
        assert parsed == {"chat": [], "image": [], "completion": [], "all": []}

    def test_catalog_rows_only_chat_and_image(self):
        rows = catalog.catalog_model_rows({
            "chat": ["gemini-3-flash"], "image": ["gemini-3.1-flash-image"],
            "completion": ["tab_x"], "all": [],
        })
        names = [r["name"] for r in rows]
        assert "tab_x" not in names
        assert set(names) == {"gemini-3-flash", "gemini-3.1-flash-image"}
        assert all(r["kind"] in ("chat", "image") for r in rows)

    def test_display_name_fallback(self):
        assert catalog.display_name("gemini-3-flash") == "Gemini 3 Flash"
        assert catalog.display_name("unknown-model") == "unknown-model"


class TestCallbackServer:
    async def test_state_generated_and_redirect_uri(self):
        from app.auth.antigravity.callback import CallbackServer

        server = CallbackServer()
        assert len(server.state) > 20
        await server.start()
        try:
            assert server.port > 0
            assert server.redirect_uri.startswith("http://127.0.0.1:")
            assert server.redirect_uri.endswith("/callback")
        finally:
            await server.stop()

    async def test_cancel_settles(self):
        from app.auth.antigravity.callback import CallbackServer

        server = CallbackServer()
        server.cancel()
        result = await server.wait(timeout_ms=500)
        assert result == {"error": "登录已取消"}


class TestPkce:
    def test_verifier_and_challenge_differ(self):
        from app.auth.antigravity.oauth import _pkce_pair

        verifier, challenge = _pkce_pair()
        assert verifier != challenge
        assert len(verifier) > 20
        assert len(challenge) > 20
        # base64url 无填充
        assert "=" not in verifier and "=" not in challenge


@pytest.mark.parametrize("kind", ["login_required", "refresh_failed"])
def test_auth_error_kind(kind):
    from app.auth.antigravity.token import AntigravityAuthError

    err = AntigravityAuthError("x", kind)
    assert err.kind == kind
    assert err.message == "x"
