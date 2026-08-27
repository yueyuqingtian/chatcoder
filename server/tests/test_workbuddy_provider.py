"""workbuddy Provider 单测：请求头 / thinking 翻译 / SSE 解析 / 401 自动刷新重试。"""
import httpx
import pytest

from app.models.providers.workbuddy import WorkBuddyProvider
from app.models.schemas import ChatMessage, ChatRequest


def _provider(**kw) -> WorkBuddyProvider:
    return WorkBuddyProvider(
        api_key=kw.get("api_key", "token1"),
        base_url=kw.get("base_url", "https://copilot.tencent.com/v2"),
        model=kw.get("model", "deepseek-v4-pro"),
        meta=kw.get("meta", {"account_uid": "u-10086", "temperature": 1}),
        refresh_token=kw.get("refresh_token"),
    )


class TestBaseHeaders:
    def test_bearer_and_gateway_headers(self):
        p = _provider()
        h = p._base_headers()
        assert h["Authorization"] == "Bearer token1"
        assert h["X-Domain"] == "copilot.tencent.com"
        assert h["X-User-Id"] == "u-10086"
        assert h["X-Product"] == "SaaS"
        assert h["X-Agent-Intent"] == "craft"
        assert h["X-IDE-Name"] == "workbuddy-desktop"
        assert h["X-Private-Data"] == "false"
        assert "User-Agent" in h

    def test_user_id_urlencoded(self):
        p = _provider(meta={"account_uid": "user@qq.com"})
        assert p._base_headers()["X-User-Id"] == "user%40qq.com"

    def test_enterprise_headers_when_present(self):
        p = _provider(meta={"account_uid": "u", "enterprise_id": "ent-1"})
        h = p._base_headers()
        assert h["X-Enterprise-Id"] == "ent-1"
        assert h["X-Tenant-Id"] == "ent-1"


class TestThinking:
    def test_deepseek_uses_thinking_type_and_level_map(self):
        p = _provider(model="deepseek-v4-pro")
        body = p._build_body(ChatRequest(
            messages=[ChatMessage(role="user", content="hi")],
            model="deepseek-v4-pro",
            reasoning_effort="xhigh", thinking=True,
        ))
        assert body["thinking"] == {"type": "enabled"}
        # xhigh → thinkingLevelMap → max
        assert body["reasoning_effort"] == "max"
        assert body["reasoning_summary"] == "auto"

    def test_deepseek_unknown_effort_dropped(self):
        p = _provider(model="deepseek-v4-pro")
        body = p._build_body(ChatRequest(
            messages=[ChatMessage(role="user", content="hi")],
            model="deepseek-v4-pro",
            reasoning_effort="medium", thinking=True,
        ))
        # medium 在 deepseek level_map 中为 null → 删除字段
        assert "reasoning_effort" not in body
        assert body["thinking"] == {"type": "enabled"}

    def test_zai_uses_enable_thinking(self):
        p = _provider(model="glm-5.3")
        body = p._build_body(ChatRequest(
            messages=[ChatMessage(role="user", content="hi")],
            model="glm-5.3",
            reasoning_effort="high", thinking=True,
        ))
        assert body.get("enable_thinking") is True
        assert "reasoning_effort" not in body

    def test_unknown_model_passthrough_effort(self):
        p = _provider(model="hy3")
        body = p._build_body(ChatRequest(
            messages=[ChatMessage(role="user", content="hi")],
            model="hy3",
            reasoning_effort="high", thinking=True,
        ))
        assert body["reasoning_effort"] == "high"

    def test_no_thinking_leaves_standard_body(self):
        p = _provider(model="deepseek-v4-pro")
        body = p._build_body(ChatRequest(
            messages=[ChatMessage(role="user", content="hi")],
            model="deepseek-v4-pro",
        ))
        # thinking 关闭：deepseek 发 disabled，无 effort
        assert body["thinking"] == {"type": "disabled"}
        assert "reasoning_effort" not in body
        assert body["temperature"] == 1


class TestConvertMessages:
    def test_tool_call_turn_content_empty_string(self):
        p = _provider()
        msgs = [ChatMessage(
            role="assistant", content=None,
            tool_calls=[{"id": "call_1", "name": "fs_read", "arguments": {"path": "a.py"}}],
        )]
        out = [p._convert_message(m) for m in msgs]
        assert out[0]["content"] == ""
        assert out[0]["tool_calls"][0]["function"]["name"] == "fs_read"

    def test_reasoning_content_carried_on_tool_turn(self):
        p = _provider()
        msgs = [ChatMessage(
            role="assistant", content=None, reasoning_content="thinking…",
            tool_calls=[{"id": "c", "name": "todo_write", "arguments": {}}],
        )]
        out = p._convert_message(msgs[0])
        assert out["reasoning_content"] == "thinking…"

    def test_developer_role_maps_to_system(self):
        p = _provider()
        assert p._convert_message(ChatMessage(role="developer", content="ctx"))["role"] == "system"


class TestSseParse:
    def test_content_reasoning_tool_calls_and_usage(self):
        p = _provider()
        monitor = p._new_monitor()
        tool_delta = ('data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1",'
                      '"function":{"name":"fs_read","arguments":"{\\"path\\":"}}]}}]}')
        tool_arg_delta = ('data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
                          '"function":{"arguments":"\\"a.py\\"}"}}]}}]}')
        usage_frame = ('data: {"usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15,'
                       '"completion_tokens_details":{"reasoning_tokens":3}}}')
        frames = [
            'data: {"choices":[{"delta":{"reasoning_content":"先思考"}}]}',
            'data: {"choices":[{"delta":{"content":"你好"}}]}',
            tool_delta,
            tool_arg_delta,
            'data: {"choices":[{"finish_reason":"tool_calls"}]}',
            usage_frame,
            'data: [DONE]',
        ]
        for f in frames:
            p._parse_frame(f, monitor)
        assert "".join(monitor["thinking_parts"]) == "先思考"
        assert "".join(monitor["content_parts"]) == "你好"
        assert monitor["finish_reason"] == "tool_calls"
        assert monitor["usage"].reasoning_tokens == 3
        assert monitor["usage"].prompt_tokens == 10
        assert monitor["terminal"] is True


class TestStream401Refresh:
    @pytest.mark.asyncio
    async def test_401_refreshes_once_and_retries(self):
        calls: list[httpx.Request] = []
        refreshed = {"token2": False}

        async def refresh():
            refreshed["token2"] = True
            return "token2"

        async def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            if request.headers.get("Authorization") == "Bearer token1":
                return httpx.Response(401, json={"code": 401, "msg": "unauthorized"})
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                content=b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n',
            )

        p = _provider(refresh_token=refresh)
        p._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        events = [e async for e in p.stream_structured(ChatRequest(
            messages=[ChatMessage(role="user", content="hi")],
            model="deepseek-v4-pro",
        ))]
        assert refreshed["token2"] is True
        assert len(calls) == 2
        assert calls[0].headers["Authorization"] == "Bearer token1"
        assert calls[1].headers["Authorization"] == "Bearer token2"
        done = events[-1]
        assert done["type"] == "done"
        assert done["content"] == "ok"

    @pytest.mark.asyncio
    async def test_no_refresh_callback_401_raises(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"code": 401})

        p = _provider()  # 无 refresh_token
        p._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with pytest.raises(RuntimeError):
            async for _ in p.stream_structured(ChatRequest(
                messages=[ChatMessage(role="user", content="hi")],
                model="deepseek-v4-pro",
            )):
                pass
