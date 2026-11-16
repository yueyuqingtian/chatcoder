"""plan-20261112-retry-policy-1：从调用边界验证次数、等待、凭据切换与 SDK。"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.core.config import settings
from app.models.registry import _build_provider
from app.models.schemas import ChatMessage, ChatRequest, ChatResponse
from app.orchestration import agent_loop as loop
from tests import test_compact_target as fixtures


@pytest.fixture
async def db(tmp_path):
    async for session in fixtures.db.__wrapped__(tmp_path):
        yield session


@pytest.mark.parametrize("interval", [0, 0.1, 0.6, 10.25])
async def test_wait_uses_exact_duration_without_rounding(monkeypatch, interval):
    sleep = AsyncMock()
    monkeypatch.setattr(loop.asyncio, "sleep", sleep)
    assert await loop._wait_retry_interval(None, interval)
    sleep.assert_awaited_once_with(interval)

    async def timeout(awaitable, *, timeout):
        awaitable.close()
        assert timeout == interval
        raise asyncio.TimeoutError

    waiter = AsyncMock(side_effect=timeout)
    monkeypatch.setattr(loop.asyncio, "wait_for", waiter)
    assert await loop._wait_retry_interval(asyncio.Event(), interval)
    assert waiter.await_count == (1 if interval else 0)


async def test_cancel_interrupts_long_wait_immediately():
    event = asyncio.Event()
    asyncio.get_running_loop().call_later(0.01, event.set)
    assert not await asyncio.wait_for(loop._wait_retry_interval(event, 3600), timeout=1)
    assert not await loop._wait_retry_interval(event, 0)


@pytest.mark.parametrize("status", [429, 500, 503, "network"])
async def test_sdk_sends_only_one_request_for_one_provider_call(status):
    provider = _build_provider(api_key="test", base_url="https://local.invalid/v1", model="test")
    sent = []

    def handler(request):
        sent.append(request)
        if status == "network":
            raise httpx.ConnectError("connection reset", request=request)
        return httpx.Response(status, headers={"retry-after": "0"},
                              json={"error": {"message": "upstream failed"}})

    assert provider._client.max_retries == 0
    await provider._client._client.aclose()
    provider._client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(RuntimeError, match="model gateway error"):
            await provider.chat(ChatRequest(model="test", messages=[ChatMessage(role="user", content="测试")]))
        assert len(sent) == 1
    finally:
        await provider._client.close()


def test_registry_forwards_explicit_sdk_retry_override():
    provider = _build_provider(api_key="test", base_url="https://local.invalid/v1",
                               model="test", max_retries=2)
    assert provider._client.max_retries == 2
    asyncio.run(provider._client.close())


@pytest.mark.parametrize("failures, intervals, expected_calls, expected_waits", [
    ([RuntimeError("503 unavailable")] * 4, "10,20,30", 4, [10, 20, 30]),
    ([ChatResponse(), RuntimeError("503 unavailable"), ChatResponse(),
      RuntimeError("504 gateway timeout")], "10,20,30", 4, [10, 20, 30]),
    ([RuntimeError("503 unavailable")], "", 1, []),
    ([ChatResponse()], "", 1, []),
    ([RuntimeError("503 unavailable"), RuntimeError("context_length_exceeded"),
      RuntimeError("503 unavailable"), RuntimeError("503 unavailable")],
     "10,20,30", 4, [10, 20, 30]),
    ([ChatResponse(), RuntimeError("context_length_exceeded"),
      RuntimeError("503 unavailable"), RuntimeError("503 unavailable")],
     "10,20,30", 4, [10, 20, 30]),
    ([RuntimeError("503 unavailable"), RuntimeError("invalid_request_error: unknown field")],
     "10,20,30", 2, [10]),
    ([RuntimeError("503 unavailable"), ChatResponse(content="恢复完成")], "0,20,30", 2, [0]),
])
async def test_agent_paths_share_one_budget(db, tmp_path, monkeypatch, failures, intervals,
                                          expected_calls, expected_waits):
    session = await fixtures._mk_session(db)
    await db.commit()
    provider = SimpleNamespace(supports_thinking=lambda: False, credential_row_id=1)
    registry = SimpleNamespace(get_provider_for_agent=AsyncMock(return_value=(provider, None)))
    monkeypatch.setattr(loop, "get_model_registry", lambda: registry)
    monkeypatch.setattr(settings, "agent_retry_intervals", intervals)
    monkeypatch.setattr(loop, "broadcast", AsyncMock())
    monkeypatch.setattr(loop, "_emit_agent_msg", AsyncMock())
    monkeypatch.setattr(loop, "_report_credential", AsyncMock())
    buffer = SimpleNamespace(enqueue=lambda item: None, flush=AsyncMock())
    monkeypatch.setattr("app.persistence.write_behind.write_behind",
                        SimpleNamespace(get=lambda *args: buffer))
    monkeypatch.setattr(loop.hook_service, "run_hooks", AsyncMock(return_value=[]))
    monkeypatch.setattr(loop, "_compact_persistent_or_fallback",
                        AsyncMock(side_effect=lambda *a, **kw: (None, kw["messages"])))
    calls, waits = [], []

    async def stream(*args, **kwargs):
        calls.append(args[1])
        assert len(calls) <= len(failures), "出现未授权的额外模型调用"
        item = failures[len(calls) - 1]
        if isinstance(item, Exception):
            raise item
        return item.model_copy(deep=True)

    async def wait(event, seconds):
        waits.append(seconds)
        return True

    monkeypatch.setattr(loop, "_stream_chat_and_broadcast", stream)
    monkeypatch.setattr(loop, "_wait_retry_interval", wait)
    out = await loop.run_agent_loop(
        db, session_id=session.id, turn_id=1,
        agent=SimpleNamespace(id=1, kind="main", name="测试代理", model_id=None),
        context_messages=[ChatMessage(role="system", content="规则"),
                          ChatMessage(role="user", content="执行任务", context_kind="current_task")],
        tool_schemas=[], workspace=str(tmp_path), max_steps=1)
    assert len(calls) == expected_calls
    assert waits == expected_waits
    if failures[-1].__class__ is ChatResponse and failures[-1].content:
        assert out.kind == "message" and out.text == "恢复完成"
    else:
        assert out.kind == "error" and out.retry_handled
        if expected_calls > 1:
            assert f"已自动重试 {expected_calls - 1} 次" in out.error
        else:
            assert "已自动重试" not in out.error


async def test_failed_credentials_rotate_within_authorized_attempts(monkeypatch):
    providers = [SimpleNamespace(credential_row_id=i) for i in (1, 2, 3)]
    exclusions, called = [], []

    async def next_provider(db, model, exclude):
        exclusions.append(set(exclude))
        for provider in providers:
            if provider.credential_row_id not in exclude:
                return provider, "ok", provider.credential_row_id
        return None, "no_more_credentials", None

    async def stream(provider, request, **kwargs):
        called.append(provider.credential_row_id)
        return ChatResponse(content="ok")

    monkeypatch.setattr(loop, "get_model_registry", lambda: SimpleNamespace(
        next_provider_for_model=next_provider))
    monkeypatch.setattr(loop, "_broadcast_turn_status", AsyncMock())
    monkeypatch.setattr(loop, "_stream_chat_and_broadcast", stream)
    state, failed = {"provider": providers[0]}, {1}
    for _ in range(3):
        await loop._retry_stream_call(
            db=None, model=object(), state=state,
            api_messages=[ChatMessage(role="user", content="测试")], tool_schemas=[],
            session_id=1, turn_id=1, step=1, agent_id=1, agent_name="测试", thread_id=None,
            cancel_event=None, reasoning_effort=None, thinking=None, workspace="", effort=None,
            failed_creds=failed)
        failed.add(state["provider"].credential_row_id)
    assert called == [2, 3, 1]
    assert exclusions == [{1}, {1, 2}, {1, 2, 3}, {3}]


@pytest.mark.parametrize("status", ["paused", "failed"])
def test_workflow_adapter_preserves_retry_handled(status):
    from app.orchestration.workflow.executor_subagent import _to_result

    handle = SimpleNamespace(status=status, summary="", findings="", error="503 gateway timeout",
                             pause_reason="", retry_handled=True)
    result = _to_result(handle, 7)
    assert result.retry_handled and result.transient
    assert result.agent_id == 7


@pytest.mark.parametrize("error", ["503 service unavailable", "connection reset", "AI服务异常，请稍后重试"])
async def test_stream_failure_does_not_issue_hidden_nonstream_call(monkeypatch, error):
    async def stream(request):
        raise RuntimeError(error)
        yield  # pragma: no cover

    provider = SimpleNamespace(name="test", stream_structured=stream, chat=AsyncMock())
    monkeypatch.setattr(loop, "broadcast", AsyncMock())
    with pytest.raises(RuntimeError, match=error):
        await loop._stream_chat_and_broadcast(
            provider, ChatRequest(model="test", messages=[ChatMessage(role="user", content="测试")]),
            session_id=1, turn_id=1, agent_id=1, agent_name="测试")
    provider.chat.assert_not_awaited()
