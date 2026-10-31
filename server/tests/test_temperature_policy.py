"""Claude/Anthropic 系模型温度策略测试。

覆盖：统一判定（识别 / 温度选择 / 400 错误识别）、各 Provider 的请求体
（openai 兼容 / anthropic / workbuddy / ta3 / commandcode）以及网关拒绝
非默认温度后的自愈重试。
"""
import asyncio
from types import SimpleNamespace

import httpx
import pytest
from openai import APIStatusError

from app.models.providers.anthropic import AnthropicProvider
from app.models.providers.commandcode import CommandCodeProvider
from app.models.providers.openai_compatible import OpenAICompatibleProvider
from app.models.providers.ta3 import Ta3Provider
from app.models.providers.temperature_policy import (
    is_anthropic_family,
    is_claude_model,
    is_temperature_rejected,
    pick_temperature,
)
from app.models.providers.workbuddy import WorkBuddyProvider
from app.models.schemas import ChatMessage, ChatRequest


def _request(model: str, temperature: float | None = 0.3) -> ChatRequest:
    return ChatRequest(
        messages=[ChatMessage(role="user", content="hi")],
        model=model,
        temperature=temperature,
    )


class TestPolicy:
    @pytest.mark.parametrize("name,expected", [
        ("claude-haiku-5-5", True),
        ("Claude-Sonnet-5.5", True),
        ("kr/claude-opus-5.5", True),
        ("anthropic/claude-sonnet-4-5", True),
        ("claude-opus-5.5-thinking", True),
        ("my-claude-proxy-v1", True),
        ("gpt-4o", False),
        ("deepseek-v4-pro", False),
        ("", False),
        (None, False),
    ])
    def test_is_claude_model(self, name, expected):
        """模型名中携带 claude 字样即适配（不限前缀/后缀/大小写）。"""
        assert is_claude_model(name) is expected

    @pytest.mark.parametrize("name,base_url,expected", [
        ("claude-haiku-5-5", None, True),
        ("kr/claude-opus-5.5", None, True),
        ("anthropic/claude-sonnet-4-5", None, True),
        ("gpt-4o", None, False),
        ("custom-model", "https://api.anthropic.com", True),
        ("deepseek-v4-pro", "https://api.deepseek.com", False),
    ])
    def test_is_anthropic_family(self, name, base_url, expected):
        assert is_anthropic_family(name, base_url) is expected

    def test_extra_names_any_hit(self):
        """request.model 与配置模型名不一致时，任一来源含 claude 均命中。"""
        assert is_anthropic_family(
            "alias-model", extra_names=("kr/claude-opus-5.5",),
        ) is True
        assert is_anthropic_family(
            "alias-model", extra_names=("Claude Sonnet 5.5",),
        ) is True

    def test_extra_names_keeps_non_claude_behavior(self):
        assert pick_temperature(
            0.3, model_name="alias-model", extra_names=("deepseek-v4-pro",),
        ) == 0.3

    def test_claude_ignores_non_default_temperature(self):
        assert pick_temperature(0.3, model_name="claude-haiku-5-5") is None
        assert pick_temperature(0.7, model_name="kr/claude-opus-5.5") is None

    def test_claude_keeps_catalog_default_temperature(self):
        assert pick_temperature(0.7, model_name="claude-haiku-5-5",
                                catalog_default=1.0) == 1.0

    def test_other_models_keep_request_value(self):
        assert pick_temperature(0.7, model_name="deepseek-v4-pro") == 0.7
        assert pick_temperature(None, model_name="glm-5",
                                catalog_default=0.1) == 0.1
        assert pick_temperature(None, model_name="glm-5") is None

    def test_non_claude_default_applied(self):
        assert pick_temperature(
            None, model_name="metis-4", non_claude_default=0.1,
        ) == 0.1
        assert pick_temperature(
            None, model_name="claude-haiku-5-5", non_claude_default=0.1,
        ) is None

    def test_rejected_error_detection(self):
        msg = "claude-haiku-5-5 does not support non-default temperature"
        assert is_temperature_rejected(400, msg) is True
        assert is_temperature_rejected(500, msg) is False
        assert is_temperature_rejected(400, "invalid api key") is False


class TestOpenAICompatibleTemperature:
    def _provider(self, model: str = "claude-haiku-5-5") -> OpenAICompatibleProvider:
        return OpenAICompatibleProvider(
            api_key="test-key",
            base_url="https://service.guyueyu.asia/v1",
            model=model,
        )

    def test_claude_drops_temperature(self):
        kwargs: dict = {}
        self._provider()._apply_temperature(kwargs, _request("claude-haiku-5-5"))
        assert "temperature" not in kwargs

    def test_other_model_keeps_temperature(self):
        kwargs: dict = {}
        self._provider("gpt-4o")._apply_temperature(kwargs, _request("gpt-4o"))
        assert kwargs["temperature"] == 0.3

    def test_default_model_carries_claude_even_if_request_alias(self):
        """request.model 是不含 claude 的别名时，仍按配置模型名适配。"""
        kwargs: dict = {}
        self._provider("claude-haiku-5-5")._apply_temperature(
            kwargs, _request("alias-model"),
        )
        assert "temperature" not in kwargs

    def test_retries_without_temperature_on_400(self):
        """静态识别不到的模型别名：命中温度 400 时移除该字段重试。"""
        provider = self._provider()
        calls: list[dict] = []

        async def create(**kwargs):
            calls.append(dict(kwargs))
            if len(calls) == 1:
                response = httpx.Response(
                    400, request=httpx.Request("POST", "https://example.com"),
                )
                raise APIStatusError(
                    "claude-haiku-5-5 does not support non-default temperature",
                    response=response, body=None,
                )
            return "ok"

        provider._client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
        )
        result = asyncio.run(provider._create_compat(
            {"model": "claude-haiku-5-5", "temperature": 0.3},
        ))
        assert result == "ok"
        assert "temperature" not in calls[1]


class TestAnthropicTemperature:
    def _provider(self, model: str, base_url: str = "https://api.anthropic.com"):
        return AnthropicProvider(api_key="test-key", base_url=base_url, model=model)

    def test_claude_drops_non_default_temperature(self):
        payload = self._provider("claude-haiku-5-5")._build_payload(
            _request("claude-haiku-5-5"), stream=False,
        )
        assert "temperature" not in payload

    def test_non_claude_keeps_temperature(self):
        payload = self._provider("custom-model", "https://example.com")._build_payload(
            _request("custom-model", 0.5), stream=False,
        )
        assert payload["temperature"] == 0.5

    def test_default_model_carries_claude_even_if_request_alias(self):
        payload = self._provider("claude-haiku-5-5")._build_payload(
            _request("alias-model", 0.5), stream=False,
        )
        assert "temperature" not in payload


class TestWorkBuddyTemperature:
    def _provider(self, model: str, meta: dict) -> WorkBuddyProvider:
        return WorkBuddyProvider(
            api_key="token", base_url="https://copilot.tencent.com/v2",
            model=model, meta=meta,
        )

    def test_claude_uses_catalog_default(self):
        p = self._provider("claude-haiku-5-5", {"temperature": 1})
        body = p._build_body(_request("claude-haiku-5-5", 0.7))
        assert body["temperature"] == 1

    def test_claude_without_catalog_omits_temperature(self):
        p = self._provider("claude-haiku-5-5", {})
        body = p._build_body(_request("claude-haiku-5-5", 0.7))
        assert "temperature" not in body

    def test_other_model_keeps_request_temperature(self):
        p = self._provider("deepseek-v4-pro", {"temperature": 1})
        body = p._build_body(_request("deepseek-v4-pro", 0.3))
        assert body["temperature"] == 0.3

    def test_catalog_title_carries_claude(self):
        """目录标题带 Claude、模型 id 为别名时同样不下发非默认温度。"""
        p = self._provider("alias-model", {"title": "Claude Sonnet 5.5"})
        body = p._build_body(_request("alias-model", 0.7))
        assert "temperature" not in body


class TestTa3Temperature:
    """plan-601-2495：ta3 温度按官方口径——只取目录 completionOptions.temperature，缺省 0.1，
    无 claude 名称特判，忽略本项目的 0.3/0.7 请求温度。
    依据：tmp_ta3_ref/dist/main/chat/openAiAdapter.js:107、anthropicAdapter.js:297（均为
    `toNumber(runtimeOptions.temperature ?? options.temperature, 0.1)`，主聊天不传 runtime 温度，
    streamRequest.js:395 只透传 options.temperature，chatService.js:670-678 不设置该值）。
    """

    def _provider(self, model: str, opts: dict | None = None) -> Ta3Provider:
        return Ta3Provider(
            api_key="llm-test", base_url="https://lc.example.com/newcoder/ai/dispatch/v2",
            model=model, meta={"completionOptions": opts} if opts else {},
        )

    def test_openai_body_keeps_default_temperature_for_claude(self):
        # 官方 openAiAdapter.js:107 无 claude 特判：恒发目录值/0.1
        body = self._provider("claude-opus-5.5")._build_openai_body(
            _request("claude-opus-5.5"), [],
        )
        assert body["temperature"] == 0.1

    def test_openai_body_ignores_requested_temperature(self):
        """请求温度（0.3）不生效；目录温度生效（openAiAdapter.js:107）。"""
        body = self._provider("metis-4")._build_openai_body(_request("metis-4"), [])
        assert body["temperature"] == 0.1
        body = self._provider("metis-4", {"temperature": 0.6})._build_openai_body(
            _request("metis-4"), [])
        assert body["temperature"] == 0.6

    def test_openai_body_falls_back_to_default_without_requested_temperature(self):
        """请求未给出温度时，回落目录默认 0.1。"""
        req = _request("metis-4")
        req.temperature = None
        body = self._provider("metis-4")._build_openai_body(req, [])
        assert body["temperature"] == 0.1

    def test_anthropic_body_keeps_default_temperature_for_claude(self):
        # 官方 anthropicAdapter.js:297 无 claude 特判；只在思考开启时删除（:332）
        body = self._provider("claude-opus-5.5")._build_anthropic_body(
            _request("claude-opus-5.5"), [],
        )
        assert body["temperature"] == 0.1

    def test_request_model_carries_claude(self):
        """请求侧模型名带 claude 也不影响温度（openAiAdapter.js:107）。"""
        body = self._provider("metis-4")._build_openai_body(
            _request("kr/claude-opus-5.5"), [],
        )
        assert body["temperature"] == 0.1


class TestCommandCodeTemperature:
    def test_claude_drops_temperature(self):
        p = CommandCodeProvider(api_key="test-key", model="claude-haiku-5-5")
        payload = p._build_payload(_request("claude-haiku-5-5"))
        assert "temperature" not in payload["params"]

    def test_other_model_keeps_default(self):
        p = CommandCodeProvider(api_key="test-key", model="glm-5")
        payload = p._build_payload(_request("glm-5", None))
        assert payload["params"]["temperature"] == 0.3

    def test_default_model_carries_claude_even_if_request_alias(self):
        p = CommandCodeProvider(api_key="test-key", model="claude-haiku-5-5")
        payload = p._build_payload(_request("alias-model", 0.3))
        assert "temperature" not in payload["params"]


class TestUnifiedTemperatureRules:
    """统一规则：思考开启时不设置温度（所有模型）；否则遵循设置温度（含 ta3 / kimi）。"""

    def test_thinking_on_detection(self):
        from app.models.providers.temperature_policy import thinking_on

        def req(effort=None, thinking=None):
            return ChatRequest(messages=[ChatMessage(role="user", content="x")], model="m",
                               reasoning_effort=effort, thinking=thinking)

        assert thinking_on(req(effort="high")) is True
        assert thinking_on(req(effort="none")) is False
        assert thinking_on(req(effort="")) is False
        assert thinking_on(req(effort=None, thinking=True)) is True
        assert thinking_on(req(effort=None, thinking=False)) is False

    def test_request_temperature_none_when_thinking_on(self):
        from app.models.providers.temperature_policy import request_temperature

        r = ChatRequest(messages=[ChatMessage(role="user", content="x")], model="metis-4",
                        temperature=0.3, reasoning_effort="medium")
        assert request_temperature(r, model_name="metis-4") is None

    def test_request_temperature_follows_setting_when_thinking_off(self):
        from app.models.providers.temperature_policy import request_temperature

        r = ChatRequest(messages=[ChatMessage(role="user", content="x")], model="metis-4",
                        temperature=0.3, reasoning_effort="none")
        assert request_temperature(r, model_name="metis-4") == 0.3

    def test_ta3_kimi_body_uses_catalog_temperature(self):
        # plan-601-2495：ta3 温度只取目录值（anthropicAdapter.js:297），请求温度 0.5 不生效
        p = Ta3Provider(api_key="llm-test", base_url="https://lc.example.com/newcoder/ai/dispatch/v2",
                        model="kimi-k2", meta={"completionOptions": {"temperature": 0.2}})
        body = p._build_anthropic_body(_request("kimi-k2", 0.5), [])
        assert body.get("temperature") == 0.2

    def test_ta3_openai_body_keeps_temperature_when_thinking(self):
        # plan-601-2495：官方 OpenAI 面思考开启也恒发 temperature（openAiAdapter.js:107、114-131
        # 的思考分支只追加字段、从不删温度）
        p = Ta3Provider(api_key="llm-test", base_url="https://lc.example.com/newcoder/ai/dispatch/v2",
                        model="metis-4", meta={"completionOptions": {"thinkingEnabled": True}})
        req = _request("metis-4", 0.3)
        req.reasoning_effort = "high"
        body = p._build_openai_body(req, [])
        assert body["temperature"] == 0.1
        assert body["thinking"] == {"type": "enabled"}
