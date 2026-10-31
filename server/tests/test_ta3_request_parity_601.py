"""ta3 供应商层请求构造与官方「Ta+3 牛码 0.7.5」对齐（plan-601-2495）。

官方依据统一指向 tmp_ta3_ref/dist/main/ 与 node_modules/undici/lib/web/fetch/：
1. 请求头：build*Headers（modelClient.js:186-232）+ undici 追加（fetch/index.js:498-503、
   1462-1529；util.js:230-253；client-h1.js:1250-1264）；
2. Anthropic 消息结构：convertMessages / pushAnthropicMessage / normalizeAnthropicThinkingBlocks
   （anthropicAdapter.js:159-238）；摘要形态 contextCompaction.js:129-136；
4. 温度/思考/键顺序：openAiAdapter.js:98-176、anthropicAdapter.js:283-341、deepSeekAdapter.js:32-52；
5. 协议判定与端点：modelClient.js:61-163、providers.js:122-131；
6. 辅助文本请求：textRequest.js:23-49；
7. 脱敏重发收窄：只在 403 + 拦截字样时重发。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.auth.ta3 import catalog
from app.core.config import settings
from app.models.providers import ta3 as ta3_mod
from app.models.providers.ta3 import Ta3Provider, _ta3_relocate_inline_system, _wire_headers
from app.models.schemas import ChatMessage, ChatRequest

BASE = "https://lc.example.com/newcoder/ai/dispatch/v2"


def _p(model: str = "glm-5", **meta) -> Ta3Provider:
    base = meta.pop("_base", BASE)
    return Ta3Provider(api_key="llm-k", base_url=base, model=model, meta=meta)


def _req(*msgs: ChatMessage, **kw) -> ChatRequest:
    return ChatRequest(messages=list(msgs), model=kw.pop("model", ""), **kw)


# ───────────────────────── 1. 请求头 ─────────────────────────


def test_wire_headers_follow_undici_order_and_defaults(monkeypatch):
    monkeypatch.setattr(settings, "ta3_send_user_agent", False)
    p = _p(orgId="org-1")
    biz = p._base_headers()
    # 官方 buildModelHeaders 键序（modelClient.js:192-200）
    assert list(biz) == ["Content-Type", "Accept", "X-Call-Source", "lcappid", "x-app-id",
                         "Authorization", "api-key"]
    assert biz["Accept"] == "text/event-stream, application/json"
    wire = _wire_headers(biz, f"{BASE}/chat/completions", 12)
    assert list(wire) == ["host", "connection", *biz.keys(), "accept-language",
                          "sec-fetch-mode", "user-agent", "accept-encoding", "content-length"]
    assert wire["user-agent"] == "node"
    assert wire["accept-language"] == "*"
    assert wire["sec-fetch-mode"] == "cors"
    assert wire["content-length"] == "12"
    # https 且 brotli 可用时宣告 br（fetch/index.js:1523-1528）
    assert wire["accept-encoding"] == ("br, gzip, deflate" if ta3_mod._BROTLI_OK else "gzip, deflate")
    assert _wire_headers(biz, "http://x/chat/completions", 1)["accept-encoding"] == "gzip, deflate"


def test_user_agent_switch_keeps_legacy_semantics(monkeypatch):
    p = _p()
    monkeypatch.setattr(settings, "ta3_send_user_agent", True)
    wire = _wire_headers(p._base_headers(), f"{BASE}/chat/completions", 1)
    assert wire["User-Agent"] == p._ua
    assert "user-agent" not in wire  # 不重复追加
    monkeypatch.setattr(settings, "ta3_send_user_agent", False)
    assert "User-Agent" not in p._base_headers()


def test_httpx_client_has_no_default_headers():
    p = _p()
    assert dict(p._client.headers) == {}  # 不再注入 python-httpx UA / Accept: */*


def test_anthropic_version_from_catalog_and_default():
    assert _p("kimi-k3", anthropic=True)._base_headers()["anthropic-version"] == "2023-06-01"
    h = _p("kimi-k3", anthropic=True, anthropicVersion="2024-10-22")._base_headers()
    assert h["anthropic-version"] == "2024-10-22"
    assert list(h)[-2:] == ["anthropic-version", "x-api-key"]


def test_text_request_uses_json_first_accept_and_compact_header_only_for_deepseek():
    h = _p("deepseek-v4", provider="deepseek")._base_headers(
        "application/json, text/event-stream", compact=True)
    assert h["Accept"] == "application/json, text/event-stream"
    assert h["x-ta3-compact"] == "1"
    assert "x-ta3-compact" not in _p()._base_headers(compact=True)


def test_sent_request_bytes_and_headers(monkeypatch):
    """端到端：实际发出的 content 为紧凑 JSON，头为 _wire_headers 结果。"""
    monkeypatch.setattr(settings, "ta3_send_user_agent", False)
    p = _p()
    seen = {}

    class _Resp:
        status_code = 200
        headers = {"content-type": "text/event-stream"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def aread(self):
            return b""

        def aiter_lines(self):
            async def g():
                yield 'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}'
                yield "data: [DONE]"
            return g()

    def _stream(method, url, **kw):
        seen.update(url=url, **kw)
        return _Resp()

    p._client.stream = _stream
    asyncio.run(p.chat(_req(ChatMessage(role="user", content="你好"))))
    assert seen["url"] == f"{BASE}/chat/completions"
    body = seen["content"].decode("utf-8")
    assert '"stream":false' in body and ", " not in body and "你好" in body
    assert seen["headers"]["content-length"] == str(len(seen["content"]))
    assert seen["headers"]["Accept"] == "application/json, text/event-stream"


# ───────────────────────── 2. 中途 system 重定位 + Anthropic 结构 ─────────────────────────


def test_relocate_inline_system_pure_function():
    from app.orchestration.prompts.summary import SUMMARY_PREFIX_ZH

    msgs = [
        ChatMessage(role="system", content="[compact-boundary id=x] 以上历史已压缩为摘要。"),
        ChatMessage(role="system", content="PERSONA"),
        ChatMessage(role="developer", content=f"{SUMMARY_PREFIX_ZH}\n\n摘要正文"),
        ChatMessage(role="user", content="u1"),
        ChatMessage(role="assistant", content="", tool_calls=[{"id": "c1", "name": "fs_read",
                                                               "arguments": {"path": "a"}}]),
        ChatMessage(role="tool", tool_call_id="c1", content="r1"),
        ChatMessage(role="system", content="提醒A"),
        ChatMessage(role="user", content="u2"),
        ChatMessage(role="system", content="提醒B"),
    ]
    before = [m.model_dump() for m in msgs]
    out = _ta3_relocate_inline_system(msgs)
    assert [m.model_dump() for m in msgs] == before  # 不改入参
    assert [m.role for m in out] == ["system", "assistant", "user", "assistant", "tool", "user"]
    assert out[0].content == "PERSONA"
    assert out[1].content == "<conversation_summary>\n摘要正文\n</conversation_summary>"
    # 提醒A 落到之后最近的 user（u2）；提醒B 之后无 user → 回落之前最近的 user（同为 u2）
    texts = [b["text"] for b in out[5].content_blocks]
    assert texts == ["<system-reminder>\n提醒A\n</system-reminder>",
                     "<system-reminder>\n提醒B\n</system-reminder>"]
    assert not any("compact-boundary" in (m.content or "") for m in out)


def test_relocate_after_tool_result_merges_into_tool_result_user():
    msgs = [
        ChatMessage(role="system", content="S"),
        ChatMessage(role="user", content="u"),
        ChatMessage(role="assistant", content="", tool_calls=[{"id": "c1", "name": "fs_read",
                                                               "arguments": {"path": "a"}}]),
        ChatMessage(role="tool", tool_call_id="c1", content="r1"),
        ChatMessage(role="system", content="提醒"),
    ]
    system, converted = _p("kimi-k3", anthropic=True)._convert_anthropic_messages(msgs)
    assert system == "S"
    last = converted[-1]
    assert last["role"] == "user"
    assert [b["type"] for b in last["content"]] == ["tool_result", "text"]
    assert last["content"][1]["text"] == "<system-reminder>\n提醒\n</system-reminder>"


def test_emergency_summary_becomes_assistant_summary():
    out = _ta3_relocate_inline_system([
        ChatMessage(role="system", content="S"),
        ChatMessage(role="system", content="以下是之前对话的摘要：\n已调用工具: x"),
        ChatMessage(role="user", content="u"),
    ])
    assert out[1].role == "assistant"
    assert out[1].content == "<conversation_summary>\n已调用工具: x\n</conversation_summary>"


def test_anthropic_merges_same_role_and_drops_empty_and_unsigned_thinking():
    msgs = [
        ChatMessage(role="system", content="S1"),
        ChatMessage(role="user", content="a"),
        ChatMessage(role="user", content="b"),
        ChatMessage(role="user", content=""),
        ChatMessage(role="assistant", content="x", reasoning_content="无签名思考"),
        ChatMessage(role="assistant", content=""),
    ]
    system, converted = _p("kimi-k3", anthropic=True, provider="kimi")._convert_anthropic_messages(msgs)
    assert system == "S1"
    assert converted == [
        {"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "x"}]},
    ]


# ───────────────────────── 3. OpenAI 单 system ─────────────────────────


def test_openai_body_single_leading_system():
    body = _p()._build_openai_body(_req(
        ChatMessage(role="system", content="S"),
        ChatMessage(role="user", content="u"),
        ChatMessage(role="system", content="提醒"),
    ), [])
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["system", "user"]
    assert body["messages"][1]["content"][-1]["text"] == "<system-reminder>\n提醒\n</system-reminder>"


# ───────────────────────── 4. 温度 / 思考 / 键顺序 ─────────────────────────


def test_openai_key_order_and_catalog_sampling():
    opts = {"temperature": 0, "topP": 0.9, "frequencyPenalty": 0.1, "presencePenalty": 0.2,
            "maxTokens": 8192, "stop": ["END"], "thinkingEnabled": True}
    p = _p(completionOptions=opts)
    body = p._build_openai_body(_req(ChatMessage(role="user", content="u"),
                                     temperature=0.7, reasoning_effort="high"),
                                [{"type": "function", "function": {"name": "Read"}}])
    assert list(body) == ["model", "messages", "stream", "temperature", "top_p",
                          "frequency_penalty", "presence_penalty", "max_tokens", "stop",
                          "thinking", "reasoning_effort", "tools", "tool_choice"]
    assert body["temperature"] == 0  # 0 不再被 `or` 吞掉
    assert body["max_tokens"] == 8192
    assert body["thinking"] == {"type": "enabled"}


def test_openai_thinking_control_modes():
    def body(control, effort="medium"):
        p = _p(completionOptions={"thinkingEnabled": True, "thinkingControl": control})
        return p._build_openai_body(_req(ChatMessage(role="user", content="u"),
                                         reasoning_effort=effort), [])

    assert body("enable-thinking")["enable_thinking"] is True
    assert body("enable-thinking", None)["enable_thinking"] is False
    assert "thinking" not in body("reasoning-effort") and body("reasoning-effort")["reasoning_effort"] == "medium"
    assert "reasoning_effort" not in body("reasoning-effort", "none")
    assert body("thinking-object", "none")["thinking"] == {"type": "disabled"}


def test_openai_deepseek_and_zai_tail_fields():
    ds = _p("deepseek-v4-flash", provider="deepseek")._build_openai_body(
        _req(ChatMessage(role="user", content="u")), [])
    assert list(ds)[-1] == "stream_options" and ds["stream_options"] == {"include_usage": True}
    zai = _p("glm-5", provider="zai")._build_openai_body(_req(ChatMessage(role="user", content="u")), [])
    assert list(zai)[-2:] == ["stream_options", "tool_stream"]
    # "zai" 子串不再误判（官方 provider 精确等于 'zai'）
    assert "tool_stream" not in _p("glm-zai-x", provider="glm")._build_openai_body(
        _req(ChatMessage(role="user", content="u")), [])


def test_anthropic_key_order_and_thinking_drops_sampling():
    opts = {"temperature": 0.3, "topP": 0.8, "stop": ["Z"], "thinkingEnabled": True}
    p = _p("claude-sonnet", anthropic=True, completionOptions=opts)
    req = _req(ChatMessage(role="system", content="S"), ChatMessage(role="user", content="u"),
               reasoning_effort="high")
    body = p._build_anthropic_body(req, [{"type": "function", "function": {"name": "Read"}}])
    assert list(body) == ["model", "messages", "system", "stream", "max_tokens",
                          "stop_sequences", "thinking", "output_config", "tools"]
    assert body["thinking"] == {"type": "adaptive"} and body["output_config"] == {"effort": "high"}
    off = p._build_anthropic_body(_req(ChatMessage(role="user", content="u")), [])
    assert list(off)[:7] == ["model", "messages", "stream", "temperature", "top_p",
                             "max_tokens", "stop_sequences"]
    assert off["temperature"] == 0.3 and off["thinking"] == {"type": "disabled"}


def test_anthropic_claude_name_keeps_temperature():
    body = _p("claude-opus", anthropic=True)._build_anthropic_body(
        _req(ChatMessage(role="user", content="u"), temperature=0.7), [])
    assert body["temperature"] == 0.1


# ───────────────────────── 5. 协议判定 / 端点 ─────────────────────────


def test_protocol_detection_matches_official():
    assert catalog.resolve_protocol({"model": "kimi-k3"}) == "anthropic"
    assert catalog.resolve_protocol({"model": "skimix-1"}) == "openai"  # 词边界
    assert catalog.resolve_protocol({"model": "claude-x", "protocol": "openai"}) == "openai"
    assert catalog.resolve_protocol({"model": "deepseek-claude"}) == "deepseek"  # DeepSeek 先匹配
    assert catalog.resolve_protocol({"model": "x", "apiProtocol": "deepseek"}) == "deepseek"
    assert catalog.resolve_protocol({"model": "x", "apiBase": "https://h/v2/anthropic"}) == "anthropic"


def test_catalog_entry_base_and_meta_fields():
    entries = catalog._parse_config_models({"configResult": {"config": {"models": [{
        "model": "glm-5", "apiBase": "https://a/v2", "apiBaseOpenAI": "https://o/v2/",
        "anthropicVersion": "2024-01-01", "chatCompletionsEndpoint": "https://e/chat/completions",
        "baseAgentSystemMessage": "TOP-AGENT",
        "chatOptions": {"baseAgentSystemMessage": "AG", "basePlanSystemMessage": "PL"},
    }]}}})
    e = entries[0]
    assert e["base_url"] == "https://o/v2"  # OpenAI 面优先 apiBaseOpenAI
    meta = catalog.entry_meta(e, "glm-5")
    assert meta["anthropicVersion"] == "2024-01-01"
    assert meta["endpoints"] == {"chatCompletionsEndpoint": "https://e/chat/completions"}
    assert meta["chatOptions"]["baseAgentSystemMessage"] == "AG"
    assert meta["chatOptions"]["basePlanSystemMessage"] == "PL"
    for k in ("baseSystemMessage", "baseChatSystemMessage", "systemMessage"):
        assert k in meta["chatOptions"]
    assert meta["baseAgentSystemMessage"] == "TOP-AGENT"
    assert meta["basePlanSystemMessage"] == "" and meta["baseChatSystemMessage"] == ""
    assert meta["systemMessage"] == "AG"  # 原字段保持不变


@pytest.mark.parametrize("base,anthropic,endpoints,expected", [
    (BASE, False, {}, f"{BASE}/chat/completions"),
    (f"{BASE}/chat/completions", False, {}, f"{BASE}/chat/completions"),
    (BASE, False, {"endpoint": "https://e/x/"}, "https://e/x"),
    (f"{BASE}/anthropic", True, {}, f"{BASE}/anthropic/v1/messages"),
    (f"{BASE}/anthropic/v1", True, {}, f"{BASE}/anthropic/v1/messages"),
    (f"{BASE}/anthropic/v1/messages", True, {}, f"{BASE}/anthropic/v1/messages"),
    (f"{BASE}/anthropic", True, {"endpoint": "https://e/v1/messages"}, "https://e/v1/messages"),
    (f"{BASE}/anthropic", True, {"endpoint": "https://e/other"}, f"{BASE}/anthropic/v1/messages"),
])
def test_endpoint_resolution(base, anthropic, endpoints, expected):
    p = _p("kimi-k3" if anthropic else "glm-5", _base=base, anthropic=anthropic, endpoints=endpoints)
    assert p._endpoint() == expected


# ───────────────────────── 6. 辅助文本请求 ─────────────────────────


def test_text_request_body_has_no_thinking_and_stream_false():
    p = _p(completionOptions={"thinkingEnabled": True})
    body = p._build_openai_body(_req(ChatMessage(role="user", content="u"), reasoning_effort="high"),
                                [], text=True)
    assert body["stream"] is False
    assert "thinking" not in body and "reasoning_effort" not in body
    zai = _p(provider="zai")._build_openai_body(_req(ChatMessage(role="user", content="u")), [], text=True)
    assert zai["thinking"] == {"type": "disabled"} and "stream_options" not in zai
    ds = _p("deepseek-v4", provider="deepseek")._build_openai_body(
        _req(ChatMessage(role="user", content="u")), [], text=True)
    assert "stream_options" not in ds


# ───────────────────────── 7. 脱敏重发收窄 ─────────────────────────


class _ErrResp:
    def __init__(self, status, text):
        self.status_code = status
        self._text = text
        self.headers = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def aread(self):
        return self._text.encode()


@pytest.mark.parametrize("status,text,expected_calls", [
    (403, '{"message":"API 异常，无法访问"}', 2),
    (403, '{"error":"routing_error"}', 2),
    (403, '{"message":"未授权"}', 1),
    (400, '{"message":"API 异常，无法访问"}', 1),
])
def test_block_retry_only_on_403_with_marker(status, text, expected_calls, monkeypatch):
    async def _noop(*a, **kw):
        return None

    import app.auth.ta3.auto_recover as ar
    monkeypatch.setattr(ar, "maybe_auto_recover", _noop)
    # 关闭常规脱敏，让请求体真正带着特征出站，才能观察兜底重发
    monkeypatch.setattr(settings, "ta3_sanitize_outbound", False)
    p = _p()
    calls = []

    def _stream(method, url, **kw):
        calls.append(json.loads(kw["content"]))
        return _ErrResp(status, text)

    p._client.stream = _stream
    with pytest.raises(RuntimeError):
        asyncio.run(p.chat(_req(ChatMessage(role="user", content="you are claude code"))))
    assert len(calls) == expected_calls
    if expected_calls == 2:
        assert "claude code" not in json.dumps(calls[1], ensure_ascii=False).lower()
