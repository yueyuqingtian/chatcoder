"""v21: 对齐 deepseek-harness/zcode 的 thinking 模式 wire 参数测试。"""

import pytest

from app.models.providers.openai_compatible import OpenAICompatibleProvider
from app.models.schemas import ChatMessage, ChatRequest


def _provider(base_url: str = "https://api.deepseek.com", model: str = "deepseek-v4-flash") -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(api_key="test-key", base_url=base_url, model=model)


class TestSupportsThinking:
    @pytest.mark.parametrize("base_url,model,expected", [
        ("https://api.deepseek.com", "deepseek-chat", True),
        ("https://open.bigmodel.cn/api/paas/v4", "glm-4.6", True),
        ("http://localhost:8080", "deepseek-v4-flash", True),   # 模型名前缀兜底
        ("http://localhost:8080", "DeepSeek-V4-Pro", True),     # 大小写不敏感
        ("http://localhost:8080", "my-custom-model", False),
        ("https://api.openai.com/v1", "gpt-5", False),
        ("https://api.anthropic.com", "claude-sonnet-4-5", False),
    ])
    def test_detection(self, base_url, model, expected):
        assert _provider(base_url, model).supports_thinking() is expected


class TestApplyThinking:
    def test_enabled_adds_thinking_and_drops_temperature(self):
        p = _provider()
        kwargs = {"model": "m", "temperature": 0.3, "messages": []}
        p._apply_thinking(kwargs, ChatRequest(
            messages=[], model="m", temperature=0.3, reasoning_effort="high",
            thinking=True,
        ))
        assert kwargs["extra_body"]["thinking"]["type"] == "enabled"
        assert kwargs["extra_body"]["thinking"]["budget_tokens"] > 0
        assert "temperature" not in kwargs

    def test_budget_maps_by_effort(self):
        p = _provider()
        # high → 16384（对齐 anthropic provider 映射）
        assert p._thinking_budget("high") == 16384
        assert p._thinking_budget("medium") == 8192
        assert p._thinking_budget("low") == 2048
        # 未指定 effort → 默认 1024（对齐 zcode）
        assert p._thinking_budget(None) == 1024

    def test_disabled_leaves_kwargs_untouched(self):
        p = _provider()
        kwargs = {"temperature": 0.3}
        p._apply_thinking(kwargs, ChatRequest(messages=[], model="m", thinking=False))
        assert kwargs == {"temperature": 0.3}


class TestConvertMessages:
    def test_tool_call_turn_content_is_empty_string_not_null(self):
        """对齐 deepseek-harness serializeAssistant：纯 tool_call 回合 content 发空串而非 null。"""
        p = _provider()
        msgs = [ChatMessage(
            role="assistant", content=None,
            tool_calls=[{"id": "call_1", "name": "fs_read", "arguments": {"path": "a.py"}}],
        )]
        out = p._convert_messages(msgs)
        assert out[0]["content"] == ""
        assert out[0]["tool_calls"][0]["function"]["name"] == "fs_read"

    def test_developer_role_maps_to_system_by_default(self):
        p = _provider()
        out = p._convert_messages([ChatMessage(role="developer", content="ctx")])
        assert out[0]["role"] == "system"
