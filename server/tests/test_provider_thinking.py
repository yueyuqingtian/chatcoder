"""v21: 对齐 deepseek-harness/zcode 的 thinking 模式 wire 参数测试。"""

import json
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

    def test_explicit_off_sends_disabled_on_supporting_gateway(self):
        """v29: 用户选择档位 none（thinking=False）时显式下发关闭形态。

        此前只做“不传”，网关/模型默认仍开思考——选了 none 也关不掉。
        """
        p = _provider()
        kwargs = {"model": "m", "temperature": 0.3, "messages": []}
        p._apply_thinking(kwargs, ChatRequest(messages=[], model="m", thinking=False))
        assert kwargs["extra_body"] == {"thinking": {"type": "disabled"}}
        # 关闭不移除 temperature：只有开启时 thinking 模式才不支持采样参数
        assert kwargs["temperature"] == 0.3

    def test_effort_none_also_sends_disabled(self):
        """v29: 只带档位 none（thinking=None，如辅助请求）同样下发关闭形态。"""
        p = _provider()
        kwargs = {"temperature": 0.3}
        p._apply_thinking(kwargs, ChatRequest(messages=[], model="m", reasoning_effort="none"))
        assert kwargs["extra_body"] == {"thinking": {"type": "disabled"}}

    def test_unspecified_thinking_leaves_kwargs_untouched(self):
        """v29: 未指定档位（thinking=None 且无 effort）时不触碰任何字段。"""
        p = _provider()
        kwargs = {"temperature": 0.3}
        p._apply_thinking(kwargs, ChatRequest(messages=[], model="m"))
        assert kwargs == {"temperature": 0.3}

    def test_explicit_off_on_unsupported_gateway_untouched(self):
        """v29: 不声明支持 thinking 的网关（OpenAI 官方等）不发关闭字段，避免 400。"""
        p = _provider(base_url="https://api.openai.com/v1", model="gpt-5")
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


# ───────────── v29: 显式关闭思考（档位 none 不再只做“不传”） ─────────────

class TestAnthropicExplicitOff:
    """anthropic provider：用户选 none 时下发 thinking:{type:"disabled"}。"""

    def _p(self, base_url: str = "https://api.anthropic.com"):
        from app.models.providers.anthropic import AnthropicProvider

        return AnthropicProvider(api_key="k", base_url=base_url, model="claude-sonnet-4-5")

    def test_explicit_off_sends_disabled(self):
        payload = self._p()._build_payload(
            ChatRequest(messages=[], model="m", thinking=False), stream=True,
        )
        assert payload["thinking"] == {"type": "disabled"}

    def test_effort_none_sends_disabled(self):
        payload = self._p()._build_payload(
            ChatRequest(messages=[], model="m", reasoning_effort="none"), stream=True,
        )
        assert payload["thinking"] == {"type": "disabled"}

    def test_unspecified_leaves_no_thinking(self):
        payload = self._p()._build_payload(ChatRequest(messages=[], model="m"), stream=True)
        assert "thinking" not in payload


# ───────────── plan-20261017-claude-thinking: Claude 5.x adaptive 协议 ─────────────

class TestAdaptiveThinkingDetection:
    @pytest.mark.parametrize("model,expected", [
        ("claude-haiku-5-5", True),
        ("claude-opus-5-5", True),
        ("claude-sonnet-5", True),
        ("claude-opus-4-7", False),
        ("claude-sonnet-4-5", False),
        ("claude-3-7-sonnet-20250219", False),
        ("gpt-5", False),
        ("", False),
    ])
    def test_detection(self, model, expected):
        from app.models.providers.anthropic import _uses_adaptive_thinking

        assert _uses_adaptive_thinking(model) is expected


class TestAdaptivePayload:
    def _payload(self, model: str = "claude-haiku-5-5", effort: str = "high", **kw) -> dict:
        from app.models.providers.anthropic import AnthropicProvider

        p = AnthropicProvider(api_key="k", base_url="https://gw.example/v1", model=model)
        return p._build_payload(
            ChatRequest(messages=[], model=model, reasoning_effort=effort, **kw), stream=True,
        )

    def test_claude5_uses_adaptive(self):
        """Claude 5.x 只接受 adaptive；display 缺失时上游不返回思考明文。"""
        payload = self._payload()
        assert payload["thinking"] == {"type": "adaptive", "display": "summarized"}
        assert payload["output_config"] == {"effort": "high"}
        # 思考开启时不设置温度（统一规则，见 temperature_policy.thinking_on）
        assert "temperature" not in payload

    def test_effort_passthrough_and_fallback(self):
        assert self._payload(effort="max")["output_config"] == {"effort": "max"}
        assert self._payload(effort="xhigh")["output_config"] == {"effort": "xhigh"}
        # minimal 是 OpenAI 侧档位，Anthropic 无对应值 → low
        assert self._payload(effort="minimal")["output_config"] == {"effort": "low"}

    def test_claude4_keeps_enabled(self):
        payload = self._payload(model="claude-sonnet-4-5")
        assert payload["thinking"] == {"type": "enabled"}
        assert "output_config" not in payload

    def test_claude5_none_still_disabled(self):
        payload = self._payload(effort="none")
        assert payload["thinking"] == {"type": "disabled"}
        assert "output_config" not in payload


class TestThinkingDegrade:
    def test_requires_adaptive_converts_payload(self):
        from app.models.providers.anthropic import _degrade_thinking_payload

        out = _degrade_thinking_payload(
            {"thinking": {"type": "enabled"}},
            "claude-haiku-5-5 requires adaptive thinking; omit thinking or use "
            "thinking.type=adaptive and output_config.effort",
        )
        assert out["thinking"] == {"type": "adaptive", "display": "summarized"}
        assert out["output_config"] == {"effort": "high"}

    def test_output_config_rejected_is_stripped(self):
        from app.models.providers.anthropic import _degrade_thinking_payload

        out = _degrade_thinking_payload(
            {"thinking": {"type": "adaptive", "display": "summarized"},
             "output_config": {"effort": "high"}},
            "400 UNKNOWN_FIELD: output_config",
        )
        assert "output_config" not in out
        assert out["thinking"]["type"] == "adaptive"

    def test_budget_tokens_rejected_is_stripped(self):
        from app.models.providers.anthropic import _degrade_thinking_payload

        out = _degrade_thinking_payload(
            {"thinking": {"type": "enabled", "budget_tokens": 16384}},
            "400 UNKNOWN_FIELD: thinking.budget_tokens",
        )
        assert out["thinking"] == {"type": "enabled"}

    def test_no_degrade_when_already_adaptive(self):
        from app.models.providers.anthropic import _degrade_thinking_payload

        assert _degrade_thinking_payload(
            {"thinking": {"type": "adaptive"}}, "requires adaptive thinking",
        ) is None

    def test_unrelated_400_not_degraded(self):
        from app.models.providers.anthropic import _degrade_thinking_payload

        assert _degrade_thinking_payload(
            {"thinking": {"type": "adaptive"}}, "max_tokens: field required",
        ) is None

    def test_output_cap_learned_and_applied_to_next_request(self, monkeypatch):
        """plan-549-2281 守卫承接：官方 128K 兜底遇到型号上限时自学，并应用到后续请求。

        原用例随 test_subsession_wait_and_guards.py 删除而丢失——本文件此前只覆盖降级的
        其它分支，「输出上限自学」这条防死循环守卫零覆盖，这里补回。"""
        from app.models.providers import anthropic as anthropic_mod
        from app.models.providers.anthropic import (
            AnthropicProvider,
            _degrade_thinking_payload,
        )

        monkeypatch.setattr(anthropic_mod, "_LEARNED_OUTPUT_CAPS", {})
        payload = {
            "model": "claude-sonnet-4-x", "max_tokens": 128_000,
            "thinking": {"type": "enabled", "budget_tokens": 70_000},
        }
        msg = ("max_tokens: 128000 > 64000, which is the maximum allowed "
               "number of output tokens for claude-sonnet-4-x")
        degraded = _degrade_thinking_payload(payload, msg)
        assert degraded is not None
        assert degraded["max_tokens"] == 64_000
        assert degraded["thinking"]["budget_tokens"] < 64_000  # 思考预算必须小于 max_tokens
        assert anthropic_mod._LEARNED_OUTPUT_CAPS["claude-sonnet-4-x"] == 64_000
        # 已无可收紧空间时不再重试，避免死循环
        assert _degrade_thinking_payload(degraded, msg) is None

        # 下一次请求直接按自学到的上限收紧；其它型号不受影响，仍是统一兜底
        provider = AnthropicProvider(api_key="k", base_url="https://api.anthropic.com/v1",
                                     model="claude-sonnet-4-x")
        req = ChatRequest(model="claude-sonnet-4-x", messages=[ChatMessage(role="user", content="hi")])
        assert provider._build_payload(req, stream=True)["max_tokens"] == 64_000
        other = ChatRequest(model="claude-opus-5-5", messages=[ChatMessage(role="user", content="hi")])
        assert provider._build_payload(other, stream=True)["max_tokens"] == 128_000


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200, text: str = "") -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise AssertionError(f"unexpected status {self.status_code}")


class _FakeAsyncClient:
    """替代 httpx.AsyncClient，返回预置响应。"""

    def __init__(self, responses: list[_FakeResponse]) -> None:
        self._responses = responses
        self.posted: list[dict] = []

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None):
        self.posted.append(json or {})
        return self._responses.pop(0)


class TestChatParsesThinking:
    async def test_thinking_block_collected(self, monkeypatch):
        """非流式回退路径此前只解析 text/tool_use，thinking block 被整段丢弃。"""
        from app.models.providers import anthropic as mod

        resp = _FakeResponse({
            "content": [
                {"type": "thinking", "thinking": "先设鸡为 x", "signature": "sig"},
                {"type": "text", "text": "鸡 23 只，兔 12 只。"},
            ],
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 30,
                "output_tokens_details": {"thinking_tokens": 12},
            },
        })
        fake = _FakeAsyncClient([resp])
        monkeypatch.setattr(mod.httpx, "AsyncClient", fake)

        p = mod.AnthropicProvider(api_key="k", base_url="https://gw.example/v1",
                                  model="claude-haiku-5-5")
        result = await p.chat(ChatRequest(
            messages=[ChatMessage(role="user", content="鸡兔同笼")],
            model="claude-haiku-5-5",
            reasoning_effort="high",
        ))

        assert result.thinking == "先设鸡为 x"
        assert result.content == "鸡 23 只，兔 12 只。"
        assert result.usage.reasoning_tokens == 12

    async def test_400_adaptive_retry(self, monkeypatch):
        """网关要求 adaptive 时自动降级重试，不让整轮对话失败。"""
        from app.models.providers import anthropic as mod

        first = _FakeResponse(
            {}, status_code=400,
            text="claude-haiku-5-5 requires adaptive thinking; omit thinking",
        )
        second = _FakeResponse({
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        })
        fake = _FakeAsyncClient([first, second])
        monkeypatch.setattr(mod.httpx, "AsyncClient", fake)

        # 用 4.x 模型名构造，确保首轮走 enabled，从而触发降级
        p = mod.AnthropicProvider(api_key="k", base_url="https://gw.example/v1",
                                  model="claude-sonnet-4-5")
        result = await p.chat(ChatRequest(
            messages=[], model="claude-sonnet-4-5", reasoning_effort="high",
        ))

        assert result.content == "ok"
        assert fake.posted[0]["thinking"] == {"type": "enabled"}
        assert fake.posted[1]["thinking"] == {"type": "adaptive", "display": "summarized"}


# ───────────── plan-20261017-claude-tools: 流式工具调用解析 ─────────────

class _FakeStreamResponse:
    """替代 httpx 的流式响应：按行回放 SSE。"""

    def __init__(self, lines: list[str], status_code: int = 200) -> None:
        self._lines = lines
        self.status_code = status_code

    async def aread(self) -> bytes:
        return b""

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise AssertionError(f"unexpected status {self.status_code}")

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeStreamCtx:
    def __init__(self, resp: _FakeStreamResponse) -> None:
        self._resp = resp

    async def __aenter__(self) -> _FakeStreamResponse:
        return self._resp

    async def __aexit__(self, *exc) -> bool:
        return False


class _FakeStreamClient:
    """替代 httpx.AsyncClient：client.stream(...) 返回预置 SSE 响应。"""

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines
        self.posted: list[dict] = []

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, method, url, json=None, headers=None):
        self.posted.append(json or {})
        return _FakeStreamCtx(_FakeStreamResponse(self._lines))


def _sse(event: str, payload: dict) -> list[str]:
    return [f"event: {event}", f"data: {json.dumps(payload, ensure_ascii=False)}", ""]


def _reply_with_tool_use(*, partials: list[str], stop_reason: str = "tool_use",
                         tool_input: dict | None = None) -> list[str]:
    """拼一段 Anthropic 流式响应：一个 tool_use 块 + 若干参数增量。"""
    lines: list[str] = []
    lines += _sse("message_start", {
        "type": "message_start",
        "message": {"usage": {"input_tokens": 12}},
    })
    lines += _sse("content_block_start", {
        "type": "content_block_start",
        "index": 0,
        "content_block": {
            "type": "tool_use", "id": "toolu_1", "name": "fs_read",
            "input": tool_input if tool_input is not None else {},
        },
    })
    for piece in partials:
        lines += _sse("content_block_delta", {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": piece},
        })
    lines += _sse("content_block_stop", {"type": "content_block_stop", "index": 0})
    lines += _sse("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason},
        "usage": {"output_tokens": 20},
    })
    lines += _sse("message_stop", {"type": "message_stop"})
    return lines


class TestStreamToolUse:
    async def _collect(self, monkeypatch, lines: list[str]) -> list[dict]:
        from app.models.providers import anthropic as mod

        fake = _FakeStreamClient(lines)
        monkeypatch.setattr(mod.httpx, "AsyncClient", fake)
        p = mod.AnthropicProvider(api_key="k", base_url="https://gw.example/v1",
                                  model="claude-haiku-5-5")
        out = []
        async for ev in p.stream_structured(ChatRequest(
            messages=[], model="claude-haiku-5-5", reasoning_effort="high",
        )):
            out.append(ev)
        return out

    async def test_tool_use_collected(self, monkeypatch):
        """此前完全不解析 tool_use 块，tool_calls 恒为空 —— 模型"说一句话就停"。"""
        events = await self._collect(monkeypatch, _reply_with_tool_use(
            partials=['{"path": ', '"README.md"}'],
        ))
        done = [e for e in events if e["type"] == "done"][0]
        assert done["finish_reason"] == "tool_calls"
        assert done["tool_calls"] == [{
            "id": "toolu_1", "name": "fs_read", "arguments": {"path": "README.md"},
        }]

    async def test_cumulative_frames_not_duplicated(self, monkeypatch):
        """网关重复投递累计式完整内容时不能拼出重复 JSON，否则参数解析失败。"""
        events = await self._collect(monkeypatch, _reply_with_tool_use(
            partials=['{"path": ', '{"path": "README.md"}'],
        ))
        done = [e for e in events if e["type"] == "done"][0]
        assert done["tool_calls"][0]["arguments"] == {"path": "README.md"}

    async def test_start_frame_full_input(self, monkeypatch):
        """部分网关在 start 帧直接给完整 input，没有增量帧。"""
        events = await self._collect(monkeypatch, _reply_with_tool_use(
            partials=[], tool_input={"path": "CLAUDE.md"},
        ))
        done = [e for e in events if e["type"] == "done"][0]
        assert done["tool_calls"][0]["arguments"] == {"path": "CLAUDE.md"}

    async def test_text_and_tool_use_coexist(self, monkeypatch):
        """正文与工具调用同时存在时都要保留（前者说明意图，后者驱动执行）。"""
        lines: list[str] = []
        lines += _sse("message_start", {
            "type": "message_start", "message": {"usage": {"input_tokens": 5}},
        })
        lines += _sse("content_block_start", {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "text", "text": ""},
        })
        lines += _sse("content_block_delta", {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": "我先看一下文件。"},
        })
        lines += _reply_with_tool_use(partials=['{"path": "a.py"}'])
        events = await self._collect(monkeypatch, lines)
        content = "".join(e["delta"] for e in events if e["type"] == "content")
        done = [e for e in events if e["type"] == "done"][0]
        assert content == "我先看一下文件。"
        assert done["tool_calls"][0]["name"] == "fs_read"

    async def test_no_tool_use_keeps_stop(self, monkeypatch):
        lines: list[str] = []
        lines += _sse("message_start", {
            "type": "message_start", "message": {"usage": {"input_tokens": 3}},
        })
        lines += _sse("content_block_start", {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "text", "text": ""},
        })
        lines += _sse("content_block_delta", {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": "好的。"},
        })
        lines += _sse("message_delta", {
            "type": "message_delta", "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 4},
        })
        lines += _sse("message_stop", {"type": "message_stop"})
        events = await self._collect(monkeypatch, lines)
        done = [e for e in events if e["type"] == "done"][0]
        assert done["finish_reason"] == "end_turn"
        assert done["tool_calls"] == []


class TestChatToolUseFinishReason:
    async def test_stop_reason_tool_use_mapped(self, monkeypatch):
        """非流式同样要映射成 tool_calls，否则畸形流检测会误判。"""
        from app.models.providers import anthropic as mod

        resp = _FakeResponse({
            "content": [
                {"type": "text", "text": "我看一下。"},
                {"type": "tool_use", "id": "toolu_9", "name": "fs_read",
                 "input": {"path": "README.md"}},
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 8, "output_tokens": 12},
        })
        fake = _FakeAsyncClient([resp])
        monkeypatch.setattr(mod.httpx, "AsyncClient", fake)

        p = mod.AnthropicProvider(api_key="k", base_url="https://gw.example/v1",
                                  model="claude-haiku-5-5")
        result = await p.chat(ChatRequest(messages=[], model="claude-haiku-5-5"))

        assert result.finish_reason == "tool_calls"
        assert result.tool_calls[0]["arguments"] == {"path": "README.md"}
