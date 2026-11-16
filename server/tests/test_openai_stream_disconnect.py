"""plan-20261114-gemini-stream：通过真实 SDK 验证流尾断连与响应释放。"""

import asyncio
import json

import httpx
import pytest
from openai import AsyncOpenAI

from app.models.providers.openai_compatible import OpenAICompatibleProvider
from app.models.schemas import ChatMessage, ChatRequest


def _frame(delta=None, finish_reason=None, usage=None):
    return {
        "id": "chat-test", "object": "chat.completion.chunk", "created": 0,
        "model": "gemini-test",
        "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish_reason}],
        **({"usage": usage} if usage else {}),
    }


class _Body(httpx.AsyncByteStream):
    def __init__(self, frames, error=None, hang=False):
        self.frames = frames
        self.error = error
        self.hang = hang
        self.closed = False
        self.waiting = asyncio.Event()

    async def __aiter__(self):
        for frame in self.frames:
            yield ("data: " + json.dumps(frame) + "\n\n").encode()
        if self.hang:
            self.waiting.set()
            await asyncio.Event().wait()
        if self.error:
            raise self.error

    async def aclose(self):
        self.closed = True


async def _run(method, body):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=body)

    provider = OpenAICompatibleProvider(
        api_key="test", base_url="https://gateway.test/v1", model="gemini-test",
    )
    await provider._client.close()
    async with AsyncOpenAI(
        api_key="test", base_url="https://gateway.test/v1", max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    ) as client:
        provider._client = client
        provider._chunk_timeout = 0.1 if body.hang else 5
        request = ChatRequest(messages=[ChatMessage(role="user", content="测试")], model="gemini-test")
        if method == "chat":
            result = await provider.chat(request)
        else:
            result = [event async for event in getattr(provider, method)(request)]
    assert len(calls) == 1
    assert calls[0]["model"] == "gemini-test"
    return result


def _disconnect():
    return httpx.RemoteProtocolError(
        "peer closed connection without sending complete message body (incomplete chunked read)"
    )


@pytest.mark.parametrize("method", ["chat", "stream", "stream_structured"])
async def test_completed_response_survives_truncated_transport(method):
    body = _Body([_frame({"content": "完整回答"}), _frame(finish_reason="stop")], _disconnect())
    result = await _run(method, body)
    assert body.closed
    if method == "chat":
        assert result.content == "完整回答"
    elif method == "stream":
        assert result == ["完整回答"]
    else:
        assert result[-1]["type"] == "done"
        assert result[-1]["content"] == "完整回答"


@pytest.mark.parametrize("method", ["chat", "stream", "stream_structured"])
@pytest.mark.parametrize("delta", [
    {"content": "未完成"},
    {"tool_calls": [{"index": 0, "id": "call_test", "type": "function",
                     "function": {"name": "fs_read", "arguments": '{"path":'}}]},
    {},
])
async def test_disconnect_before_completion_remains_failure(method, delta):
    body = _Body([_frame(delta)], _disconnect())
    with pytest.raises((RuntimeError, httpx.RemoteProtocolError), match="incomplete chunked read"):
        await _run(method, body)
    assert body.closed


@pytest.mark.parametrize("method", ["chat", "stream_structured"])
async def test_completed_tool_call_keeps_arguments_and_usage(method):
    body = _Body([
        _frame({"tool_calls": [{"index": 0, "id": "call_test", "type": "function",
                               "function": {"name": "fs_read", "arguments": '{"path":'}}]}),
        _frame({"tool_calls": [{"index": 0, "function": {"arguments": '"a.txt"}'}}]}, "tool_calls"),
        {**_frame(usage={"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10}),
         "choices": []},
    ], _disconnect())
    result = await _run(method, body)
    done = result if method == "chat" else result[-1]
    calls = done.tool_calls if method == "chat" else done["tool_calls"]
    usage = done.usage if method == "chat" else done["usage"]
    assert calls == [{"id": "call_test", "name": "fs_read", "arguments": {"path": "a.txt"}}]
    assert usage.total_tokens == 10
    assert body.closed


@pytest.mark.parametrize("method", ["chat", "stream", "stream_structured"])
@pytest.mark.parametrize("delta", [{}, {"reasoning_content": "只想了一下"}])
async def test_completed_without_payload_is_not_treated_as_success(method, delta):
    """plan-20261115-stream-tail：只下发 finish_reason（无正文、无工具调用）就断流，
    正文其实已全部丢失——不能当成功返回空回答，必须按未完成上抛让恢复引擎重试。

    仅思考无正文同样不算可交付载荷：下游本就判致命，这里直接报"断流"比报"空响应"
    更接近真实原因。
    """
    body = _Body([_frame(delta), _frame(finish_reason="stop")], _disconnect())
    with pytest.raises((RuntimeError, httpx.RemoteProtocolError), match="incomplete chunked read"):
        await _run(method, body)
    assert body.closed


@pytest.mark.parametrize("method", ["chat", "stream", "stream_structured"])
async def test_failure_before_response_is_not_swallowed(method):
    """plan-20261115-stream-tail：建连阶段就失败（SDK 会包成 APIConnectionError）
    必须照旧上抛，证明"吞尾部断连"只针对已完成且已拿到正文的场景，不会掩盖建连失败。
    """
    attempts = []

    def handler(request):
        attempts.append(request)
        raise _disconnect()

    provider = OpenAICompatibleProvider(
        api_key="test", base_url="https://gateway.test/v1", model="gemini-test",
    )
    await provider._client.close()
    async with AsyncOpenAI(
        api_key="test", base_url="https://gateway.test/v1", max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    ) as client:
        provider._client = client
        request = ChatRequest(
            messages=[ChatMessage(role="user", content="测试")], model="gemini-test",
        )
        with pytest.raises(RuntimeError, match="model gateway error"):
            if method == "chat":
                await provider.chat(request)
            else:
                [event async for event in getattr(provider, method)(request)]
    # SDK max_retries=0：建连失败不重试，只发一次请求
    assert len(attempts) == 1


@pytest.mark.parametrize("method", ["chat", "stream", "stream_structured"])
async def test_normal_stream_closes_response(method):
    body = _Body([_frame({"content": "正常"}), _frame(finish_reason="stop")])
    await _run(method, body)
    assert body.closed


@pytest.mark.parametrize("method", ["chat", "stream", "stream_structured"])
async def test_other_errors_after_finish_are_not_hidden(method):
    body = _Body([_frame(finish_reason="stop")], httpx.ReadError("读取失败"))
    with pytest.raises((RuntimeError, httpx.ReadError), match="读取失败"):
        await _run(method, body)
    assert body.closed


@pytest.mark.parametrize("method", ["chat", "stream", "stream_structured"])
async def test_timeout_closes_response(method):
    body = _Body([], hang=True)
    with pytest.raises(RuntimeError, match="stream chunk timeout"):
        await _run(method, body)
    assert body.closed


@pytest.mark.parametrize("method", ["chat", "stream", "stream_structured"])
async def test_cancellation_closes_response(method):
    body = _Body([], hang=True)
    task = asyncio.create_task(_run(method, body))
    await asyncio.wait_for(body.waiting.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert body.closed
