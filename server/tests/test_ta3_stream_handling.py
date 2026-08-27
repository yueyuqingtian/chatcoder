"""ta3/长思考模型流式修复单测（v28：运行中突然停止且无报错）。

覆盖：
- Anthropic 帧解析补全：message_start/message_delta 的 usage、ping 心跳、message_stop 终止
- Anthropic max_tokens 以目录 completionOptions.maxTokens 封顶（超上限会导致网关截断/空响应）
- agent_loop 超时识别 `_is_stream_timeout` 与响应健康检查 `_response_failure_reason`
- v29 (plan-78)：kimi effort 归一化、supports_thinking、thinking 帧变体、思考看门狗
"""
import asyncio
import time

from app.models.providers.ta3 import Ta3Provider
from app.models.schemas import ChatRequest, ChatResponse
from app.orchestration.agent_loop import _is_stream_timeout, _response_failure_reason


def _provider(**meta) -> Ta3Provider:
    model = meta.pop("_model", "kimi-k3")
    return Ta3Provider(
        api_key="k", base_url="https://lc.example.com/newcoder",
        model=model, meta=meta,
    )


# ── Anthropic 帧解析补全 ──

def test_anthropic_message_start_sets_input_tokens():
    p = _provider(anthropic=True, provider="kimi")
    monitor = p._new_monitor()
    done = p._parse_anthropic_frame(
        'data: {"type":"message_start","message":{"usage":{"input_tokens":1234}}}', monitor,
    )
    assert done is False
    assert monitor["usage"].prompt_tokens == 1234


def test_anthropic_message_delta_sets_output_tokens_and_stop_reason():
    p = _provider(anthropic=True, provider="kimi")
    monitor = p._new_monitor()
    monitor["usage"].prompt_tokens = 100
    done = p._parse_anthropic_frame(
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
        '"usage":{"output_tokens":50}}',
        monitor,
    )
    assert done is False
    assert monitor["finish_reason"] == "end_turn"
    assert monitor["usage"].completion_tokens == 50
    assert monitor["usage"].total_tokens == 150


def test_anthropic_ping_records_heartbeat():
    p = _provider(anthropic=True, provider="kimi")
    monitor = p._new_monitor()
    assert monitor["last_heartbeat_at"] is None
    done = p._parse_anthropic_frame('data: {"type":"ping"}', monitor)
    assert done is False
    assert monitor["last_heartbeat_at"] is not None
    assert time.time() - monitor["last_heartbeat_at"] < 5


def test_anthropic_message_stop_is_terminal():
    p = _provider(anthropic=True, provider="kimi")
    monitor = p._new_monitor()
    done = p._parse_anthropic_frame('data: {"type":"message_stop"}', monitor)
    assert done is True
    assert monitor["terminal"] is True


# ── Anthropic max_tokens 对齐 ──

def test_anthropic_max_tokens_capped_by_catalog():
    p = _provider(anthropic=True, provider="kimi", completionOptions={"maxTokens": 32768})
    body = p._build_anthropic_body(ChatRequest(messages=[], model="kimi-k3", max_tokens=131072), [])
    assert body["max_tokens"] == 32768


def test_anthropic_max_tokens_uses_request_when_below_catalog():
    p = _provider(anthropic=True, provider="kimi", completionOptions={"maxTokens": 32768})
    body = p._build_anthropic_body(ChatRequest(messages=[], model="kimi-k3", max_tokens=5000), [])
    assert body["max_tokens"] == 5000


def test_anthropic_max_tokens_default_2048_without_catalog():
    p = _provider(anthropic=True, provider="kimi")
    body = p._build_anthropic_body(ChatRequest(messages=[], model="kimi-k3"), [])
    assert body["max_tokens"] == 2048


# ── agent_loop 超时识别 ──

def test_is_stream_timeout_matches_provider_errors():
    # ta3：httpx.ReadTimeout → RuntimeError("模型请求超时：ReadTimeout")
    assert _is_stream_timeout(RuntimeError("模型请求超时：ReadTimeout")) is True
    # openai_compatible：asyncio.TimeoutError → RuntimeError("stream chunk timeout")
    _chunk_err = RuntimeError("model gateway error: stream chunk timeout (180s)")
    assert _is_stream_timeout(_chunk_err) is True
    # asyncio.TimeoutError 直接命中
    assert _is_stream_timeout(asyncio.TimeoutError()) is True


def test_is_stream_timeout_rejects_other_errors():
    assert _is_stream_timeout(RuntimeError("模型请求失败 500：oops")) is False
    assert _is_stream_timeout(ValueError("bad")) is False
    assert _is_stream_timeout(RuntimeError("模型请求失败：ConnectError")) is False


# ── 响应健康检查（空响应/超时/截断不再静默） ──

def test_empty_response_is_fatal():
    reason, fatal = _response_failure_reason(ChatResponse(content=None, finish_reason="stop"))
    assert fatal is True
    assert "空响应" in reason


def test_timeout_empty_response_is_fatal():
    reason, fatal = _response_failure_reason(ChatResponse(content=None, finish_reason="timeout"))
    assert fatal is True
    assert "超时" in reason


def test_timeout_with_partial_content_is_not_fatal():
    resp = ChatResponse(content="部分内容", finish_reason="timeout")
    reason, fatal = _response_failure_reason(resp)
    assert fatal is False
    assert "部分内容" in reason


def test_max_tokens_truncation_is_not_fatal():
    resp = ChatResponse(content="正文", finish_reason="max_tokens")
    reason, fatal = _response_failure_reason(resp)
    assert fatal is False
    assert "token 上限" in reason


def test_tool_calls_response_is_healthy():
    resp = ChatResponse(content="", tool_calls=[{"name": "fs_read"}], finish_reason="tool_calls")
    assert _response_failure_reason(resp) is None


def test_normal_stop_response_is_healthy():
    assert _response_failure_reason(ChatResponse(content="完成", finish_reason="stop")) is None


# ── v29 (plan-78): thinking_timeout 空响应识别 ──

def test_thinking_timeout_empty_response_is_fatal():
    reason, fatal = _response_failure_reason(ChatResponse(content=None, finish_reason="thinking_timeout"))
    assert fatal is True
    assert "思考超时" in reason


# ── v29 (plan-78): kimi effort 归一化与 supports_thinking ──

def test_supports_thinking_true_for_kimi():
    p = _provider(anthropic=True, provider="kimi")
    assert p.supports_thinking() is True


def test_supports_thinking_false_for_others():
    p = _provider(anthropic=True, provider="qwen", _model="qwen3.8-max")
    assert p.supports_thinking() is False


def test_supports_thinking_true_when_catalog_enabled():
    p = _provider(anthropic=True, provider="some", _model="custom-x",
                  completionOptions={"thinkingEnabled": True})
    assert p.supports_thinking() is True


def test_kimi_effort_normalization():
    from app.core.config import settings
    p = _provider(anthropic=True, provider="kimi")
    _orig = settings.ta3_kimi_thinking_effort
    settings.ta3_kimi_thinking_effort = "low"
    try:
        # 通用档位 → kimi 官方档位（low/high/max）
        assert p._kimi_effort(ChatRequest(messages=[], model="kimi-k3", reasoning_effort="low")) == "low"
        assert p._kimi_effort(ChatRequest(messages=[], model="kimi-k3", reasoning_effort="medium")) == "low"
        assert p._kimi_effort(ChatRequest(messages=[], model="kimi-k3", reasoning_effort="high")) == "high"
        assert p._kimi_effort(ChatRequest(messages=[], model="kimi-k3", reasoning_effort="xhigh")) == "max"
        assert p._kimi_effort(ChatRequest(messages=[], model="kimi-k3", reasoning_effort="max")) == "max"
        # 未知档位 → 保守默认 low
        assert p._kimi_effort(ChatRequest(messages=[], model="kimi-k3", reasoning_effort="ultra")) == "low"
    finally:
        settings.ta3_kimi_thinking_effort = _orig


def test_kimi_effort_in_anthropic_body():
    p = _provider(anthropic=True, provider="kimi", completionOptions={"thinkingEnabled": True})
    body = p._build_anthropic_body(
        ChatRequest(messages=[], model="kimi-k3", reasoning_effort="medium", thinking=True),
        [],
    )
    # medium → low（kimi 无 medium 档，保守降级）
    assert body.get("output_config") == {"effort": "low"}


def test_kimi_effort_unknown_falls_back_to_conservative():
    from app.core.config import settings
    p = _provider(anthropic=True, provider="kimi", completionOptions={"thinkingEnabled": True})
    _orig = settings.ta3_kimi_thinking_effort
    settings.ta3_kimi_thinking_effort = "low"
    try:
        body = p._build_anthropic_body(
            ChatRequest(messages=[], model="kimi-k3", reasoning_effort="ultra", thinking=True),
            [],
        )
        assert body.get("output_config") == {"effort": "low"}
    finally:
        settings.ta3_kimi_thinking_effort = _orig


# ── v29 (plan-78): Anthropic thinking 帧变体解析 ──

def test_anthropic_thinking_delta_variant_fields():
    p = _provider(anthropic=True, provider="kimi")
    monitor = p._new_monitor()
    # thinking_delta 规范字段
    p._parse_anthropic_frame(
        'data: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"thinking_delta","thinking":"step1"}}', monitor,
    )
    # 变体：reasoning_delta / reasoning / thinking 顶层字段
    p._parse_anthropic_frame(
        'data: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"reasoning_delta","reasoning":"step2"}}', monitor,
    )
    p._parse_anthropic_frame(
        'data: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"thinking","thinking":"step3"}}', monitor,
    )
    assert monitor["thinking_parts"] == ["step1", "step2", "step3"]


def test_anthropic_message_delta_thinking_variant():
    p = _provider(anthropic=True, provider="kimi")
    monitor = p._new_monitor()
    p._parse_anthropic_frame(
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn","thinking":"tail"}}',
        monitor,
    )
    assert monitor["finish_reason"] == "end_turn"
    assert monitor["thinking_parts"] == ["tail"]


# ── v29 (plan-78): 思考看门狗 ──

class _FakeStreamingResponse:
    """模拟网关 SSE 流：首行思考帧后挂起超过看门狗阈值。"""

    status_code = 200

    def __init__(self, lines: list[str]):
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aread(self):
        return b""

    def aiter_lines(self):
        async def _gen():
            for line in self._lines:
                yield line
            await asyncio.sleep(5)  # 超出看门狗 0.1s，且小于 httpx read 超时（不触发后者）

        return _gen()


async def _collect_stream(p: Ta3Provider, request: ChatRequest):
    events = []
    async for ev in p._stream_llm(request, p._parse_anthropic_frame):
        events.append(ev)
    return events


def test_thinking_watchdog_triggers(monkeypatch):
    from app.core.config import settings
    p = _provider(anthropic=True, provider="kimi")
    p._stream_idle_timeout = 180  # 保证触发的是思考看门狗而非空闲超时
    monkeypatch.setattr(settings, "ta3_thinking_watchdog", 0.1)
    _orig_stream = p._client.stream
    _fake = _FakeStreamingResponse([
        'data: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"thinking_delta","thinking":"ponder..."}}',
    ])
    p._client.stream = lambda *a, **kw: _fake  # httpx .stream("POST", url, ...) → 返回 fake
    try:
        events = asyncio.run(_collect_stream(p, ChatRequest(messages=[], model="kimi-k3")))
    finally:
        p._client.stream = _orig_stream
    done = [e for e in events if e["type"] == "done"][0]
    assert done["finish_reason"] == "thinking_timeout"
    assert done["thinking"] == "ponder..."
    assert done["content"] is None
