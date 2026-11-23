"""antigravity Provider 单测：消息转换 / 工具转换 / 请求体 / SSE 解析 / 签名回放。

方案 docs/plan-antigravity-provider-integration.md。重点覆盖 §3 坑点 1：
工具往返必须回放 thoughtSignature，否则第二轮 400。
"""
import json

import pytest

from app.models.providers.antigravity import SIGNATURE_KEY, AntigravityProvider
from app.models.schemas import ChatMessage, ChatRequest


def _provider(**kw) -> AntigravityProvider:
    return AntigravityProvider(
        api_key=kw.get("api_key", "tok-1"),
        base_url=kw.get("base_url", "https://daily-cloudcode-pa.googleapis.com"),
        model=kw.get("model", "gemini-3-flash"),
        meta=kw.get("meta", {"project": "aicode-consumers"}),
        refresh_token=kw.get("refresh_token"),
    )


class TestMessageConversion:
    def test_system_and_developer_go_to_system_instruction(self):
        provider = _provider()
        contents, system = provider._convert_messages([
            ChatMessage(role="system", content="行为准则"),
            ChatMessage(role="developer", content="上下文片段"),
            ChatMessage(role="user", content="你好"),
        ])
        assert system == {"parts": [{"text": "行为准则\n\n上下文片段"}]}
        assert contents == [{"role": "user", "parts": [{"text": "你好"}]}]

    def test_assistant_role_becomes_model(self):
        """协议只接受 user / model，绝不出现 assistant。"""
        provider = _provider()
        contents, _ = provider._convert_messages([
            ChatMessage(role="user", content="问"),
            ChatMessage(role="assistant", content="答"),
        ])
        roles = [c["role"] for c in contents]
        assert "assistant" not in roles
        assert contents[1] == {"role": "model", "parts": [{"text": "答"}]}

    def test_tool_result_becomes_function_response(self):
        provider = _provider()
        contents, _ = provider._convert_messages([
            ChatMessage(role="user", content="查天气"),
            ChatMessage(
                role="assistant", content=None,
                tool_calls=[{"id": "call_1", "name": "get_weather", "arguments": {"city": "北京"}}],
            ),
            ChatMessage(role="tool", tool_call_id="call_1", name="get_weather",
                        content='{"temp": "22C"}'),
        ])
        # 工具结果归到 user role 的 functionResponse
        last = contents[-1]
        assert last["role"] == "user"
        assert last["parts"][0]["functionResponse"]["name"] == "get_weather"
        assert last["parts"][0]["functionResponse"]["response"] == {"temp": "22C"}

    def test_tool_result_non_json_wrapped_as_output(self):
        provider = _provider()
        contents, _ = provider._convert_messages([
            ChatMessage(role="tool", tool_call_id="c1", name="fs_read", content="纯文本结果"),
        ])
        assert contents[0]["parts"][0]["functionResponse"]["response"] == {
            "output": "纯文本结果",
        }

    def test_consecutive_same_role_merged(self):
        """协议要求 role 交替，连续同 role 必须合并。"""
        provider = _provider()
        contents, _ = provider._convert_messages([
            ChatMessage(role="user", content="一"),
            ChatMessage(role="user", content="二"),
        ])
        assert len(contents) == 1
        assert len(contents[0]["parts"]) == 2

    def test_empty_thought_only_message_skipped(self):
        """只含思考的历史不进 contents，避免空 model 轮。"""
        provider = _provider()
        contents, _ = provider._convert_messages([
            ChatMessage(role="user", content="问"),
            ChatMessage(role="assistant", content=None, reasoning_content="只在思考"),
        ])
        assert len(contents) == 1
        assert contents[0]["role"] == "user"


class TestSignatureReplay:
    """坑点 1：签名必须与 functionCall 同处一个 part。"""

    def test_signature_attached_to_function_call_part(self):
        provider = _provider()
        contents, _ = provider._convert_messages([
            ChatMessage(role="user", content="查天气"),
            ChatMessage(
                role="assistant", content=None,
                tool_calls=[{
                    "id": "call_1", "name": "get_weather",
                    "arguments": {"city": "北京"},
                    SIGNATURE_KEY: "SIG-ABC",
                }],
            ),
        ])
        part = contents[-1]["parts"][0]
        assert part["functionCall"]["name"] == "get_weather"
        assert part["thoughtSignature"] == "SIG-ABC"
        # 签名不能落到 functionCall 内部（协议要求同层）
        assert "thoughtSignature" not in part["functionCall"]

    def test_missing_signature_omits_key(self):
        """无签名时不写空字段（保持请求干净，缺失由上游明确报 400）。"""
        provider = _provider()
        contents, _ = provider._convert_messages([
            ChatMessage(
                role="assistant", content=None,
                tool_calls=[{"id": "c1", "name": "fs_list", "arguments": {}}],
            ),
        ])
        assert "thoughtSignature" not in contents[0]["parts"][0]

    def test_arguments_json_string_normalized(self):
        """历史里的 arguments 可能是 JSON 字符串，回放前要转成对象。"""
        provider = _provider()
        contents, _ = provider._convert_messages([
            ChatMessage(
                role="assistant", content=None,
                tool_calls=[{"id": "c1", "name": "fs_read", "arguments": '{"path": "a.txt"}'}],
            ),
        ])
        assert contents[0]["parts"][0]["functionCall"]["args"] == {"path": "a.txt"}


class TestToolConversion:
    def test_openai_schema_to_function_declarations(self):
        provider = _provider()
        tools = provider._convert_tools([
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "查询天气",
                    "parameters": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                        "required": ["city"],
                    },
                },
            }
        ])
        decls = tools[0]["functionDeclarations"]
        assert decls[0]["name"] == "get_weather"
        assert decls[0]["description"] == "查询天气"
        assert decls[0]["parameters"]["required"] == ["city"]

    def test_empty_tools_returns_none(self):
        assert _provider()._convert_tools([]) is None
        assert _provider()._convert_tools(None) is None

    def test_nameless_tool_dropped(self):
        assert _provider()._convert_tools([{"type": "function", "function": {}}]) is None


class TestBuildPayload:
    def test_payload_shape(self):
        provider = _provider()
        payload = provider._build_payload(ChatRequest(
            messages=[ChatMessage(role="user", content="hi")],
            model="gemini-3-flash",
            max_tokens=100,
        ))
        assert payload["project"] == "aicode-consumers"
        assert payload["model"] == "gemini-3-flash"
        assert payload["userAgent"] == "antigravity"
        assert payload["requestId"].startswith("req_")
        inner = payload["request"]
        assert inner["contents"] == [{"role": "user", "parts": [{"text": "hi"}]}]
        assert inner["generationConfig"]["maxOutputTokens"] == 100

    def test_system_instruction_is_object_not_string(self):
        """坑点 4：传纯字符串会 400。"""
        provider = _provider()
        payload = provider._build_payload(ChatRequest(
            messages=[
                ChatMessage(role="system", content="规则"),
                ChatMessage(role="user", content="hi"),
            ],
            model="gemini-3-flash",
        ))
        assert isinstance(payload["request"]["systemInstruction"], dict)
        assert payload["request"]["systemInstruction"]["parts"][0]["text"] == "规则"


class TestThinkingConfig:
    def test_thinking_true_includes_budget(self):
        provider = _provider()
        cfg = provider._thinking_config(
            ChatRequest(messages=[], model="m", thinking=True, reasoning_effort="high")
        )
        assert cfg["includeThoughts"] is True
        assert cfg["thinkingBudget"] == 16384

    def test_thinking_false_explicit_off(self):
        provider = _provider()
        cfg = provider._thinking_config(
            ChatRequest(messages=[], model="m", thinking=False, reasoning_effort="none")
        )
        assert cfg == {"thinkingBudget": 0, "includeThoughts": False}

    def test_thinking_none_leaves_field_absent(self):
        provider = _provider()
        assert provider._thinking_config(
            ChatRequest(messages=[], model="m", thinking=None)
        ) is None


class TestFinishReason:
    def test_tool_calls_wins(self):
        assert AntigravityProvider._map_finish("STOP", True) == "tool_calls"

    @pytest.mark.parametrize("raw,expected", [
        ("STOP", "stop"),
        ("MAX_TOKENS", "length"),
        ("SAFETY", "content_filter"),
        ("", "stop"),
    ])
    def test_mapping(self, raw, expected):
        assert AntigravityProvider._map_finish(raw, False) == expected


class _FakeResp:
    def __init__(self, lines):
        self._lines = lines

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aclose(self):
        return None


class _FakeClient:
    async def aclose(self):
        return None


def _sse(*objs) -> list[str]:
    return [f"data: {json.dumps(o, ensure_ascii=False)}" for o in objs]


def _frame(parts, finish=None, usage=None) -> dict:
    candidate = {"content": {"role": "model", "parts": parts}}
    if finish:
        candidate["finishReason"] = finish
    resp = {"candidates": [candidate]}
    if usage:
        resp["usageMetadata"] = usage
    return {"response": resp}


class TestStreamParsing:
    async def test_text_and_usage(self, monkeypatch):
        provider = _provider()
        lines = _sse(
            _frame([{"text": "你"}]),
            _frame([{"text": "好"}], finish="STOP",
                   usage={"promptTokenCount": 5, "candidatesTokenCount": 2,
                          "totalTokenCount": 7}),
        )

        async def fake_open(payload):
            return _FakeClient(), _FakeResp(lines)

        monkeypatch.setattr(provider, "_open_stream", fake_open)
        events = [e async for e in provider.stream_structured(
            ChatRequest(messages=[ChatMessage(role="user", content="hi")], model="gemini-3-flash")
        )]
        contents = [e["delta"] for e in events if e["type"] == "content"]
        assert contents == ["你", "好"]
        done = events[-1]
        assert done["type"] == "done"
        assert done["content"] == "你好"
        assert done["finish_reason"] == "stop"
        assert done["usage"].prompt_tokens == 5
        assert done["usage"].completion_tokens == 2

    async def test_empty_thought_not_emitted(self, monkeypatch):
        """坑点 10：空串 thought 直接丢弃，否则前端出现空白思考块。"""
        provider = _provider()
        lines = _sse(
            _frame([{"thought": True, "text": ""}]),
            _frame([{"thought": True, "text": "思考中"}]),
            _frame([{"text": "答"}], finish="STOP"),
        )

        async def fake_open(payload):
            return _FakeClient(), _FakeResp(lines)

        monkeypatch.setattr(provider, "_open_stream", fake_open)
        events = [e async for e in provider.stream_structured(
            ChatRequest(messages=[], model="m")
        )]
        thinking = [e["delta"] for e in events if e["type"] == "thinking"]
        assert thinking == ["思考中"]
        assert events[-1]["thinking"] == "思考中"

    async def test_function_call_captures_signature(self, monkeypatch):
        """签名与 functionCall 同帧时直接挂到同一 part。"""
        provider = _provider()
        lines = _sse(
            _frame([{
                "functionCall": {"name": "get_weather", "args": {"city": "北京"}, "id": "fc_1"},
                "thoughtSignature": "SIG-XYZ",
            }], finish="STOP"),
        )

        async def fake_open(payload):
            return _FakeClient(), _FakeResp(lines)

        monkeypatch.setattr(provider, "_open_stream", fake_open)
        events = [e async for e in provider.stream_structured(
            ChatRequest(messages=[], model="m")
        )]
        done = events[-1]
        assert done["finish_reason"] == "tool_calls"
        tc = done["tool_calls"][0]
        assert tc["name"] == "get_weather"
        assert tc["arguments"] == {"city": "北京"}
        assert tc[SIGNATURE_KEY] == "SIG-XYZ"

    async def test_lone_signature_attached_to_last_call(self, monkeypatch):
        """签名单独成帧时挂到最近的工具调用上（方案 §5.4 第 4 点）。"""
        provider = _provider()
        lines = _sse(
            _frame([{"functionCall": {"name": "fs_list", "args": {}, "id": "fc_9"}}]),
            _frame([{"thoughtSignature": "SIG-LATE"}], finish="STOP"),
        )

        async def fake_open(payload):
            return _FakeClient(), _FakeResp(lines)

        monkeypatch.setattr(provider, "_open_stream", fake_open)
        events = [e async for e in provider.stream_structured(
            ChatRequest(messages=[], model="m")
        )]
        assert events[-1]["tool_calls"][0][SIGNATURE_KEY] == "SIG-LATE"

    async def test_malformed_heartbeat_lines_ignored(self, monkeypatch):
        provider = _provider()
        lines = ["", ": ping", "data:", "data: [DONE]", "event: x"]

        async def fake_open(payload):
            return _FakeClient(), _FakeResp(lines)

        monkeypatch.setattr(provider, "_open_stream", fake_open)
        events = [e async for e in provider.stream_structured(
            ChatRequest(messages=[], model="m")
        )]
        # 只有 done 帧，不抛异常
        assert events[-1]["type"] == "done"
        assert events[-1]["content"] is None


# ─────────────────────────────────────────────
# API Key 通道（方案 §5.3 第二条认证方式）
# ─────────────────────────────────────────────
def _api_key_provider(**kw) -> AntigravityProvider:
    return AntigravityProvider(
        api_key=kw.get("api_key", "AIza-test-key"),
        base_url=kw.get("base_url", "https://generativelanguage.googleapis.com"),
        model=kw.get("model", "gemini-3-flash"),
        meta=kw.get("meta", {}),
        client_kwargs=kw.get("client_kwargs"),
        auth_kind="api_key",
    )


class TestApiKeyMode:
    def test_is_api_key_mode_flag(self):
        assert _api_key_provider().is_api_key_mode is True
        assert _provider().is_api_key_mode is False

    def test_defaults_to_oauth_when_unset(self):
        """未显式给 auth_kind 且 meta 里也没有时，默认走 CloudCode 网关。"""
        provider = AntigravityProvider(api_key="t", base_url="", model="m")
        assert provider.is_api_key_mode is False

    def test_auth_kind_from_meta(self):
        provider = AntigravityProvider(
            api_key="t", base_url="", model="m", meta={"auth_kind": "api_key"},
        )
        assert provider.is_api_key_mode is True

    def test_headers_use_x_goog_api_key(self):
        """API Key 模式用 x-goog-api-key，不能带 Authorization（会与官方鉴权冲突）。"""
        headers = _api_key_provider()._headers()
        assert headers["x-goog-api-key"] == "AIza-test-key"
        assert "Authorization" not in headers
        # 也不该带 Antigravity 的伪装头
        assert "Client-Metadata" not in headers
        assert headers["Accept"] == "text/event-stream"

    def test_oauth_headers_unchanged(self):
        headers = _provider(api_key="tok")._headers()
        assert headers["Authorization"] == "Bearer tok"
        assert "x-goog-api-key" not in headers

    def test_endpoint_is_gemini_native(self):
        endpoint = _api_key_provider()._endpoint()
        assert "/v1beta/models/gemini-3-flash:streamGenerateContent" in endpoint
        assert "alt=sse" in endpoint
        assert "v1internal" not in endpoint

    def test_endpoint_uses_config_default_when_base_empty(self):
        """Base URL 留空时回落配置里的 Gemini 兼容地址。"""
        from app.core.config import settings

        endpoint = _api_key_provider(base_url="")._endpoint()
        assert endpoint.startswith(settings.antigravity_api_key_base_url.rstrip("/"))
        assert "v1beta/models/" in endpoint

    def test_oauth_endpoint_is_cloudcode(self):
        endpoint = _provider()._endpoint()
        assert "v1internal:streamGenerateContent" in endpoint
        assert "v1beta" not in endpoint

    def test_payload_has_no_antigravity_wrapper(self):
        """原生接口只收 contents/systemInstruction/tools/generationConfig。"""
        payload = _api_key_provider()._build_payload(ChatRequest(
            messages=[ChatMessage(role="user", content="hi")],
            model="gemini-3-flash", max_tokens=64,
        ))
        assert "project" not in payload
        assert "requestId" not in payload
        assert "userAgent" not in payload
        assert "request" not in payload
        assert payload["contents"] == [{"role": "user", "parts": [{"text": "hi"}]}]
        assert payload["generationConfig"]["maxOutputTokens"] == 64

    def test_oauth_payload_keeps_wrapper(self):
        payload = _provider()._build_payload(ChatRequest(
            messages=[ChatMessage(role="user", content="hi")], model="gemini-3-flash",
        ))
        assert payload["project"] == "aicode-consumers"
        assert payload["request"]["contents"][0]["role"] == "user"

    async def test_streams_native_frames_without_response_wrapper(self, monkeypatch):
        """原生帧没有 {"response": ...} 包装，解析必须兼容。"""
        provider = _api_key_provider()
        lines = _sse(
            {"candidates": [{"content": {"role": "model", "parts": [{"text": "你"}]}}]},
            {"candidates": [{"content": {"role": "model", "parts": [{"text": "好"}]},
                             "finishReason": "STOP"}],
             "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 2,
                               "totalTokenCount": 5}},
        )

        async def fake_open(payload):
            return _FakeClient(), _FakeResp(lines)

        monkeypatch.setattr(provider, "_open_stream", fake_open)
        events = [e async for e in provider.stream_structured(
            ChatRequest(messages=[ChatMessage(role="user", content="hi")], model="gemini-3-flash")
        )]
        assert [e["delta"] for e in events if e["type"] == "content"] == ["你", "好"]
        done = events[-1]
        assert done["content"] == "你好"
        assert done["finish_reason"] == "stop"
        assert done["usage"].prompt_tokens == 3


class TestFrameBody:
    """帧兼容：CloudCode 包装帧与 Gemini 原生帧都要能取到有效负载。"""

    def test_wrapped_frame(self):
        provider = _provider()
        body = provider._frame_body({"response": {"candidates": []}, "traceId": "t"})
        assert body == {"candidates": []}

    def test_native_frame(self):
        provider = _provider()
        native = {"candidates": [{"content": {"parts": [{"text": "x"}]}}]}
        assert provider._frame_body(native) is native

    def test_native_frame_with_only_usage(self):
        provider = _provider()
        assert provider._frame_body({"usageMetadata": {"totalTokenCount": 1}}) is not None

    @pytest.mark.parametrize("frame", [None, {}, {"traceId": "t"}, "str", [], 0])
    def test_useless_frames_return_none(self, frame):
        assert _provider()._frame_body(frame) is None

    def test_iter_sse_parts_handles_both_shapes(self):
        provider = _provider()
        text = (
            'data: {"response": {"candidates": [{"n": 1}]}}\n'
            'data: {"candidates": [{"n": 2}]}\n'
            "\n"
            "data: [DONE]\n"
            ": ping\n"
        )
        parts = provider._iter_sse_parts(text)
        assert parts == [{"candidates": [{"n": 1}]}, {"candidates": [{"n": 2}]}]
