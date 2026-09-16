"""trae / workbuddy provider 的连接生命周期单测（会话隔离配套）。

背景与 ta3 同源（现场 2026-09-15，见 test_ta3_session_isolation.py）：provider 每次
请求新建 `httpx.AsyncClient`，此前只关响应、不关 client——keep-alive 长连接一直挂着，
远端按账号维度限制并发流时会占住槽位，表现为「另一会话的新流拿到 200 却长时间收不到
任何 chunk」。本文件锁住两家修复后的契约：

1. 请求结束（含取消）后关闭本次请求的客户端；
2. 关闭后再次取用懒重建（provider 实例被复用时不会踩 "client has been closed"）。
"""
import asyncio

import httpx
import pytest

from app.models.providers.trae import TraeProvider
from app.models.providers.workbuddy import WorkBuddyProvider
from app.models.schemas import ChatRequest


def _trae() -> TraeProvider:
    return TraeProvider(api_key="k", base_url="https://x.example.com", model="m", meta={})


def _wb() -> WorkBuddyProvider:
    return WorkBuddyProvider(api_key="k", base_url="https://x.example.com", model="m", meta={})


def _empty_ok_client() -> httpx.AsyncClient:
    """200 + 空 SSE 体：解析器不会报错，生成器正常收尾并产出 done。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"")

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _collect(p, request: ChatRequest) -> list[dict]:
    return [ev async for ev in p.stream_structured(request)]


def test_trae_stream_closes_client_after_completion():
    """trae：流正常结束后客户端被关闭（连接池随之释放）。"""
    p = _trae()
    client = _empty_ok_client()
    p._client = client
    events = asyncio.run(_collect(p, ChatRequest(messages=[], model="m")))
    assert client.is_closed is True
    assert [e for e in events if e["type"] == "done"]


def test_workbuddy_stream_closes_client_after_completion():
    """workbuddy：流正常结束后客户端被关闭。"""
    p = _wb()
    client = _empty_ok_client()
    p._client = client
    events = asyncio.run(_collect(p, ChatRequest(messages=[], model="m")))
    assert client.is_closed is True
    assert [e for e in events if e["type"] == "done"]


def test_trae_client_rebuilt_lazily_after_close():
    """关闭后再次取用会懒重建——复用 provider 时不会踩 "client has been closed"。"""
    p = _trae()
    first = p._client
    asyncio.run(p._close_client())
    assert first.is_closed is True

    second = p._ensure_client()
    assert second is not first and second.is_closed is False
    asyncio.run(p._close_client())  # 收尾


def test_workbuddy_cancel_closes_client():
    """用户中断（取消消费）时同样关闭客户端，不留悬挂连接。"""
    p = _wb()
    client = _empty_ok_client()
    p._client = client

    async def _hang(self, request):
        await asyncio.sleep(30)
        yield {"type": "done"}  # pragma: no cover

    p._stream_llm_inner = _hang.__get__(p, type(p))

    async def _run_then_cancel():
        task = asyncio.create_task(_collect(p, ChatRequest(messages=[], model="m")))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_run_then_cancel())
    assert client.is_closed is True
