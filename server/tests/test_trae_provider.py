"""TRAE Provider 单测：请求体构造、SSE 解析、401 刷新重试。"""
import re

import httpx

from app.auth.trae.business import build_business_headers
from app.models.providers.trae import TraeProvider
from app.models.schemas import ChatMessage, ChatRequest


def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)


def make_provider(**kw) -> TraeProvider:
    defaults = {
        "api_key": "jwt-token",
        "base_url": "https://trae-api-cn.mchost.guru",
        "model": "doubao-seed-evolving",
        "meta": {"config_name": "volcengine//doubao-seed-evolving",
                 "device_id": "d", "machine_id": "m",
                 "user_info": {"user_id": "u1", "token": "jwt-token", "region": "cn"},
                 "client_info": {"device_id": "d"}},
    }
    defaults.update(kw)
    return TraeProvider(**defaults)


def make_request() -> ChatRequest:
    return ChatRequest(
        messages=[
            ChatMessage(role="system", content="你是助手"),
            ChatMessage(role="user", content="你好"),
        ],
        model="doubao-seed-evolving",
    )


class TestHeaders:
    def test_business_headers(self):
        h = build_business_headers("jwt", {"device_id": "d", "machine_id": "m", "region": "cn"})
        assert h["Authorization"] == "Cloud-IDE-JWT jwt"
        assert h["X-User-Region"] == "cn"
        assert h["x-app-id"] == "6eefa01c-1036-4c7e-9ca5-d891f63bfcd8"
        assert h["x-device-id"] == "d"
        assert h["x-machine-id"] == "m"
        assert h["x-ide-version"] == "0.1.51"


class TestBody:
    def test_build_body_contains_messages_and_query(self):
        p = make_provider()
        body = p._build_body(make_request())
        # model_name 用纯配置名（manual 解析路径），不拼 provider 前缀、不用底层名
        assert body["model_name"] == "doubao-seed-evolving"
        assert body["message_content"][0]["role"] == "system"
        assert body["function"] == "chat"  # llm_utils_chat 必须指定 function 才能对话
        assert "你好" in body["query"]  # user 消息进 query 块
        assert body["user_info"]["user_id"] == "u1"
        # 模型解析必须带 model_auto_selection：manual_config_name 取纯名供
        # 纯名解析路径（与 create_agent_task 一致，实测 DeepSeek-V4-Pro 可过）。
        assert body["model_auto_selection"] == {
            "strategy": "manual", "manual_config_name": "doubao-seed-evolving"}
        # llm_utils_chat 是无状态 utility 端点：session_id/message_id 必须为全新
        # UUID hex，绝不能复用本地数据库会话 ID（数字 ID 会触发云端历史校验
        # “missing history count exceeded” / “model config is empty”）。
        assert re.fullmatch(r"[0-9a-f]{32}", body["session_id"])
        assert re.fullmatch(r"[0-9a-f]{32}", body["message_id"])

    def test_build_body_uses_provider_model_name(self):
        # 实测（2026-08-26，服务端 0.1.56）：create_agent_task 对全新会话按
        # model_name 精确匹配配置——纯配置名报 "model config is empty"；带
        # 档位后缀的底层名（...__dev）可被识别。model_name 优先用目录下发的
        # provider_model_name（__dev），config_name / manual_config_name 仍用纯名。
        p = make_provider(meta={"config_name": "DeepSeek-V4-Flash-Official",
                                "model_name": "DeepSeek-V4-Flash-Official",
                                "provider_model_name": "DeepSeek-V4-Flash-Official__dev",
                                "device_id": "d", "machine_id": "m",
                                "user_info": {"user_id": "u1"},
                                "client_info": {"device_id": "d"}})
        body = p._build_body(make_request())
        assert body["model_name"] == "DeepSeek-V4-Flash-Official__dev"
        assert body["config_name"] == "DeepSeek-V4-Flash-Official"
        assert body["model_auto_selection"]["manual_config_name"] == "DeepSeek-V4-Flash-Official"
        # work 通道同样用档位名
        work = p._build_work_body(make_request())
        assert work["model_name"] == "DeepSeek-V4-Flash-Official__dev"
        assert work["config_name"] == "DeepSeek-V4-Flash-Official"
        # 老数据无 provider_model_name 时回退纯配置名
        p2 = make_provider(meta={"config_name": "DeepSeek-V4-Flash-Official",
                                 "device_id": "d", "machine_id": "m",
                                 "user_info": {"user_id": "u1"},
                                 "client_info": {"device_id": "d"}})
        body2 = p2._build_work_body(make_request())
        assert body2["model_name"] == "DeepSeek-V4-Flash-Official"

    def test_build_work_body_uses_fresh_uuids(self):
        # 修复回归：create_agent_task 的 session_id/message_id 必须全新 UUID，
        # 不复用 request.session_id（本地数字 ID 如 '67' 会触发 4000105 missing history）。
        p = make_provider()
        request = make_request().model_copy(update={"session_id": "67", "message_id": "68"})
        body = p._build_work_body(request)
        assert body["conversation_id"] == body["session_id"]
        assert body["session_id"] != "67"
        assert body["message_id"] != "68"
        assert re.fullmatch(r"[0-9a-f]{32}", body["session_id"])
        assert re.fullmatch(r"[0-9a-f]{32}", body["message_id"])
        assert body["user_input"]["data"]["content"] == "你好"
        assert body["config_name"] == "doubao-seed-evolving"
        assert body["model_name"] == "doubao-seed-evolving"
        assert body["model_auto_selection"]["manual_config_name"] == "doubao-seed-evolving"

    def test_message_convert_assistant_tool_calls(self):
        p = make_provider()
        m = ChatMessage(role="assistant", content="",
                        tool_calls=[{"id": "c1", "name": "fs_read",
                                     "arguments": {"path": "a.py"}}])
        out = p._convert_message(m)
        assert out["tool_calls"][0]["function"]["name"] == "fs_read"


class TestParseFrame:
    def test_openai_style_delta(self):
        p = make_provider()
        mon = p._new_monitor()
        terminal = p._parse_frame("data: {\"choices\": [{\"delta\": {\"content\": \"hi\"}}]}", mon)
        assert mon["content_parts"] == ["hi"]
        assert terminal is False

    def test_done_frame(self):
        p = make_provider()
        mon = p._new_monitor()
        assert p._parse_frame("data: [DONE]", mon) is True

    def test_usage_frame(self):
        p = make_provider()
        mon = p._new_monitor()
        frame = ("data: {\"usage\": {\"prompt_tokens\": 10, "
                 "\"completion_tokens\": 5, \"total_tokens\": 15}}")
        p._parse_frame(frame, mon)
        assert mon["usage"].prompt_tokens == 10
        assert mon["usage"].completion_tokens == 5

    def test_trae_style_event(self):
        p = make_provider()
        mon = p._new_monitor()
        p._parse_frame("data: {\"type\": \"assistant\", \"text\": \"hello\"}", mon)
        assert mon["content_parts"] == ["hello"]

    def test_thinking(self):
        p = make_provider()
        mon = p._new_monitor()
        p._parse_frame("data: {\"reasoning_content\": \"想…\"}", mon)
        assert mon["thinking_parts"] == ["想…"]

    def test_error_event_marks_terminal_with_message(self):
        p = make_provider()
        mon = p._new_monitor()
        terminal = p._parse_frame(
            'data: {"code": 2001, "message": "[LLMUtilsChat.resolveByUsage] function is empty"}',
            mon)
        assert terminal is True
        assert mon["error"] == "[LLMUtilsChat.resolveByUsage] function is empty"

    def test_error_event_then_done(self):
        p = make_provider()
        mon = p._new_monitor()
        p._parse_frame('data: {"code": 4008, "message": "Your requests have exceeded the quota."}',
                       mon)
        p._parse_frame('data: {"finish_reason": "stop"}', mon)
        assert mon["error"] == "Your requests have exceeded the quota."
        assert mon["terminal"] is True

    def test_nested_error_and_credit_event(self):
        p = make_provider()
        mon = p._new_monitor()
        terminal = p._parse_frame(
            'data: {"type":"error","data":{"code":4008,"message":"quota",'
            '"cn_credits_remain_info":{"ide_credits":0,"work_credits":12.5}}}', mon)
        assert terminal is True
        assert mon["error"] == "quota"
        assert mon["credits"] == {"ide_credits": 0, "work_credits": 12.5}


class TestStream:
    def test_work_fallback_uses_work_endpoint_after_quota(self):
        """IDE 额度耗尽时必须自动回退 Work create_agent_task（真实对话通道）。"""
        calls: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            if request.url.path.endswith("llm_utils_chat"):
                body = b'data: {"code":4008,"message":"quota",' \
                    b'"cn_credits_remain_info":{"ide_credits":0,"work_credits":12}}\n\n'
            else:
                body = b'data: {"content":"ok"}\n\ndata: [DONE]\n\n'
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=body)

        p = make_provider()
        p._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        events = asyncio_run(_collect(p.stream_structured(make_request())))
        assert calls == ["/api/agent/v3/llm_utils_chat", "/api/agent/v3/create_agent_task"]
        assert any(e.get("type") == "content" and e.get("delta") == "ok" for e in events)

    def test_stream_emits_content_and_done(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["Authorization"].startswith("Cloud-IDE-JWT")
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                content=("data: {\"choices\": [{\"delta\": {\"content\": \"你\"}}]}\n\n"
                         "data: {\"choices\": [{\"delta\": {\"content\": \"好\"}}]}\n\n"
                         "data: [DONE]\n\n").encode(),
            )

        p = make_provider()
        p._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        events = asyncio_run(_collect(p.stream_structured(make_request())))
        contents = [e["delta"] for e in events if e["type"] == "content"]
        assert "".join(contents) == "你好"
        done = [e for e in events if e["type"] == "done"]
        assert len(done) == 1

    def test_quota_error_is_eligible_for_work_fallback(self):
        from app.models.providers.trae import _is_quota_error

        assert _is_quota_error("TRAE 模型调用失败：Your requests have exceeded the quota")
        assert not _is_quota_error("invalid model config")

    def test_error_event_raises_instead_of_silent_empty(self):
        """服务端业务错误必须显式抛错（修复前会静默产出空回复）。"""
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                content=(b'event:error\ndata: {"code": 4008, "message": '
                         b'"Your requests have exceeded the quota."}\n\n'
                         b'event:done\ndata: {"finish_reason": "stop"}\n\n'),
            )

        p = make_provider()
        p._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            asyncio_run(_collect(p.stream_structured(make_request())))
            raise AssertionError("应抛出 RuntimeError")
        except RuntimeError as e:
            assert "exceeded the quota" in str(e)

    def test_401_refreshes_once_and_retries(self):
        calls: list[httpx.Request] = []
        refreshed = {"n": 0}

        async def fake_refresh():
            refreshed["n"] += 1
            return "new-jwt"

        async def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            if request.headers["Authorization"] == "Cloud-IDE-JWT jwt-token":
                return httpx.Response(401, json={"ResponseMetadata": {"Error": {}}})
            assert request.headers["Authorization"] == "Cloud-IDE-JWT new-jwt"
            body = b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n'
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                content=body,
            )

        p = make_provider(refresh_token=fake_refresh)
        p._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        events = asyncio_run(_collect(p.stream_structured(make_request())))
        assert refreshed["n"] == 1
        assert len(calls) == 2
        assert calls[0].headers["Authorization"] == "Cloud-IDE-JWT jwt-token"
        assert calls[1].headers["Authorization"] == "Cloud-IDE-JWT new-jwt"
        assert any(e["type"] == "done" for e in events)


async def _collect(agen):
    out = []
    async for e in agen:
        out.append(e)
    return out
