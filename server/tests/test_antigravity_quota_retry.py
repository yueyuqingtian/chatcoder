"""antigravity 配额重试路径回归测试。

背景（实测故障）：配额刷新报
`'AntigravityQuotaError' object has no attribute 'message'`——
异常类只存了 kind，而重试判定读了 `e.message`。修复方式：异常类同时保存
message 与 status_code，重试判定改用 status_code（不再匹配错误文案字符串）。
"""
import types

import pytest

from app.auth.antigravity import quota
from app.auth.antigravity.quota import AntigravityQuotaError


class TestErrorAttributes:
    def test_message_is_available(self):
        """核心回归：访问 .message 不能抛 AttributeError。"""
        err = AntigravityQuotaError("配额查询失败（HTTP 500）：boom", "http", status_code=500)
        assert err.message == "配额查询失败（HTTP 500）：boom"
        assert err.kind == "http"
        assert err.status_code == 500
        # str() 与 .message 同源
        assert str(err) == err.message

    def test_status_code_defaults_to_none(self):
        err = AntigravityQuotaError("配额查询请求失败：timeout", "network")
        assert err.status_code is None
        assert err.message == "配额查询请求失败：timeout"

    def test_parse_error_has_no_status_code(self):
        err = AntigravityQuotaError("配额查询响应不是合法 JSON", "parse")
        assert err.kind == "parse"
        assert err.status_code is None


def _fake_row():
    return types.SimpleNamespace(
        access_token="at-old", project="proj-1", tier="free-tier",
        account={"email": "a@b.com"},
    )


@pytest.fixture
def patched(monkeypatch):
    """把 resolve_auth / force_refresh 换成可观察的假实现。

    `quota.get_quota` 内部 `from app.auth.antigravity import session` 是函数级导入，
    所以必须打在 session 模块自身的属性上，打在 quota 模块上不存在。
    """
    from app.auth.antigravity import session as ag_session

    calls = {"refresh": 0, "post": []}

    async def fake_resolve(db, provider_id, credential_id=None):
        return _fake_row()

    async def fake_force_refresh(db, provider_id, provider=None, credential_id=None):
        calls["refresh"] += 1
        return "at-new"

    monkeypatch.setattr(ag_session, "resolve_auth", fake_resolve)
    monkeypatch.setattr(ag_session, "force_refresh", fake_force_refresh)
    quota.clear_cache(1)
    return calls


class TestRetryOnUnauthorized:
    async def test_401_triggers_one_refresh_and_retry(self, patched, monkeypatch):
        """401 → 刷新令牌一次并用新 token 重试（故障前这里会抛 AttributeError）。"""
        seen_tokens = []

        async def fake_post(url, token, project, client_kwargs):
            seen_tokens.append(token)
            if token == "at-old":
                raise AntigravityQuotaError(
                    "配额查询失败（HTTP 401）：unauthorized", "http", status_code=401,
                )
            return {}

        monkeypatch.setattr(quota, "_post_rpc", fake_post)

        data = await quota.get_quota(None, 1, None, force=True)
        assert patched["refresh"] == 1
        assert data["tier"] == "free-tier"
        assert seen_tokens[0] == "at-old"
        assert seen_tokens[-1] == "at-new"

    async def test_403_also_retries(self, patched, monkeypatch):
        async def fake_post(url, token, project, client_kwargs):
            if token == "at-old":
                raise AntigravityQuotaError(
                    "配额查询失败（HTTP 403）：forbidden", "http", status_code=403,
                )
            return {}

        monkeypatch.setattr(quota, "_post_rpc", fake_post)

        await quota.get_quota(None, 1, None, force=True)
        assert patched["refresh"] == 1

    async def test_500_does_not_retry(self, patched, monkeypatch):
        """服务端 5xx 不是鉴权问题，不该白刷一次令牌。"""
        async def fake_post(url, token, project, client_kwargs):
            raise AntigravityQuotaError(
                "配额查询失败（HTTP 500）：boom", "http", status_code=500,
            )

        monkeypatch.setattr(quota, "_post_rpc", fake_post)

        with pytest.raises(AntigravityQuotaError) as err:
            await quota.get_quota(None, 1, None, force=True)
        assert err.value.status_code == 500
        assert patched["refresh"] == 0

    async def test_network_error_does_not_retry(self, patched, monkeypatch):
        async def fake_post(url, token, project, client_kwargs):
            raise AntigravityQuotaError("配额查询请求失败：timeout", "network")

        monkeypatch.setattr(quota, "_post_rpc", fake_post)

        with pytest.raises(AntigravityQuotaError):
            await quota.get_quota(None, 1, None, force=True)
        assert patched["refresh"] == 0

    async def test_error_text_mentioning_401_but_not_401_does_not_retry(self, patched, monkeypatch):
        """判别依据是状态码而非文案：文案里出现 401 但状态码是 400 时不重试。"""
        async def fake_post(url, token, project, client_kwargs):
            raise AntigravityQuotaError(
                "配额查询失败（HTTP 400）：quote contains HTTP 401 text", "http",
                status_code=400,
            )

        monkeypatch.setattr(quota, "_post_rpc", fake_post)

        with pytest.raises(AntigravityQuotaError):
            await quota.get_quota(None, 1, None, force=True)
        assert patched["refresh"] == 0

    async def test_success_does_not_refresh(self, patched, monkeypatch):
        async def fake_post(url, token, project, client_kwargs):
            return {}

        monkeypatch.setattr(quota, "_post_rpc", fake_post)

        await quota.get_quota(None, 1, None, force=True)
        assert patched["refresh"] == 0
