"""v21: 对齐 deepseek-harness/zcode 的 thinking 模式 wire 参数测试。"""

from types import SimpleNamespace

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
        # v23.1: budget_tokens 仅发给官方 Anthropic 网关--轻量网关
        # （bigmodel 中转/LiteLLM 等）不认识该字段会 400 UNKNOWN_FIELD
        assert "budget_tokens" not in kwargs["extra_body"]["thinking"]
        assert "temperature" not in kwargs

    def test_anthropic_gateway_gets_budget_tokens(self):
        """v23.1: 官方 Anthropic 网关（extended thinking 协议）仍携带 budget_tokens。"""
        p = _provider(base_url="https://api.anthropic.com")
        kwargs = {"model": "m", "temperature": 0.3, "messages": []}
        p._apply_thinking(kwargs, ChatRequest(
            messages=[], model="m", temperature=0.3, reasoning_effort="high",
            thinking=True,
        ))
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


# ───────────── plan-53-265: 思考块字段名兼容（OpenRouter 风格 reasoning） ─────────────

def _delta(**kwargs):
    """最小 delta 替身：显式给出 SDK 直读字段，其余仅设传入项（未设=属性不存在）。"""
    d = SimpleNamespace(content=None, tool_calls=None)
    for k, v in kwargs.items():
        setattr(d, k, v)
    return d


def _chunk(delta, finish_reason=None, usage=None):
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(usage=usage, choices=[choice])


class _FakeStream:
    """模拟 openai SDK 的异步流（逐 chunk yield）。"""

    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        async def _gen():
            for c in self._chunks:
                yield c

        return _gen()


def _install_stream(provider, chunks):
    """替换 _create_compat，使解析路径吃到构造好的 chunk 序列。"""

    async def _fake_create(kwargs):
        return _FakeStream(chunks)

    provider._create_compat = _fake_create
    return provider


def _request():
    return ChatRequest(messages=[ChatMessage(role="user", content="hi")], model="m")


class TestReasoningFieldCompat:
    """plan-53-265: 部分中转网关把思考放在 delta.reasoning。

    实测（service.guyueyu.asia + deepseek/deepseek-v4.1-flash）：思考 chunk 只带
    reasoning/reasoning_details，旧实现只读 reasoning_content/thinking，思考块整段丢失。
    """

    async def test_stream_structured_reads_reasoning_field(self):
        """流式实时广播路径：只带 reasoning 也要产出 thinking 事件。"""
        p = _install_stream(_provider(), [
            _chunk(_delta(reasoning="先算")),
            _chunk(_delta(reasoning="再答")),
            _chunk(_delta(), finish_reason="stop"),
        ])
        events = []
        async for ev in p.stream_structured(_request()):
            events.append(ev)
        assert "".join(e["delta"] for e in events if e["type"] == "thinking") == "先算再答"
        assert events[-1]["type"] == "done"
        assert events[-1]["thinking"] == "先算再答"

    async def test_chat_reads_reasoning_field(self):
        """非流式聚合路径：只带 reasoning 也要回填 thinking。"""
        p = _install_stream(_provider(), [
            _chunk(_delta(reasoning="思考")),
            _chunk(_delta(content="答案")),
            _chunk(_delta(), finish_reason="stop"),
        ])
        resp = await p.chat(_request())
        assert resp.thinking == "思考"
        assert resp.content == "答案"

    async def test_reasoning_content_priority_kept(self):
        """三字段同现时保持既有优先级 reasoning_content > reasoning > thinking。"""
        p = _install_stream(_provider(), [
            _chunk(_delta(reasoning_content="A", reasoning="B", thinking="C")),
            _chunk(_delta(), finish_reason="stop"),
        ])
        events = []
        async for ev in p.stream_structured(_request()):
            events.append(ev)
        assert "".join(e["delta"] for e in events if e["type"] == "thinking") == "A"

    async def test_no_reasoning_field_no_thinking_event(self):
        """三字段均缺时不得产出思考事件（空值安全回归）。"""
        p = _install_stream(_provider(), [
            _chunk(_delta(content="ok")),
            _chunk(_delta(), finish_reason="stop"),
        ])
        events = []
        async for ev in p.stream_structured(_request()):
            events.append(ev)
        assert not [e for e in events if e["type"] == "thinking"]
