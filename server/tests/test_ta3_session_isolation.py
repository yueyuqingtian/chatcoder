"""ta3 会话隔离单测（2026-09-15 现场：一个会话跑长命令时，另一会话的流空转 2 分钟）。

现场证据（server.log）：
- turn=1312（另一会话）19:25:53 发出流式请求，19:26:03 收到 HTTP 200 后**长时间无任何
  chunk**；19:28:06 报 `httpcore.RemoteProtocolError: peer closed connection without
  sending complete message body (incomplete chunked read)`，随后降级非流式才恢复；
- 同一时段服务端并未停摆（事件循环正常、无全局锁），说明卡的不是本地调度，
  而是**到网关的那条长连接**。

据此修的本地缺陷：provider 每次请求都会新建一个 `httpx.AsyncClient`，但此前只关响应
不关 client——keep-alive 连接会一直挂着。远端按账号维度限制并发流时，这些残留连接
会占住槽位，表现为「新流拿到 200 却收不到数据」。本文件锁住修复后的契约：

1. 流结束（正常/取消）后立即关闭本次请求的客户端；
2. 关闭后再次取用会懒重建（provider 实例被复用时不会踩"client has been closed"）；
3. 两个并发流各自持有独立客户端、内容互不串味（会话隔离）。
"""
import asyncio

import pytest

from app.models.providers.ta3 import Ta3Provider
from app.models.schemas import ChatRequest


def _provider(**meta) -> Ta3Provider:
    model = meta.pop("_model", "kimi-k3")
    return Ta3Provider(
        api_key="k", base_url="https://lc.example.com/newcoder",
        model=model, meta=meta,
    )


class _FakeStreamingResponse:
    """模拟 SSE 响应：按行吐出后结束（或挂起，供取消用例使用）。"""

    status_code = 200

    def __init__(self, lines: list[str], hang: bool = False):
        self._lines = lines
        self._hang = hang

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
            if self._hang:
                await asyncio.sleep(30)

        return _gen()


def _openai_line(text: str) -> str:
    return 'data: {"choices":[{"delta":{"content":"%s"}}]}' % text


async def _consume(p: Ta3Provider, request: ChatRequest) -> list[dict]:
    return [ev async for ev in p._stream_llm(request, p._parse_openai_frame)]


def test_stream_closes_client_after_completion():
    """流正常结束后，本次请求的 httpx 客户端被关闭（连接池随之释放）。"""
    p = _provider()
    client = p._client
    p._client.stream = lambda *a, **kw: _FakeStreamingResponse([_openai_line("hi")])
    try:
        events = asyncio.run(_consume(p, ChatRequest(messages=[], model="kimi-k3")))
    finally:
        p._client.stream = client.stream  # 兼容既有测试写法（属性回写）
    assert client.is_closed is True
    done = [e for e in events if e["type"] == "done"][0]
    assert done["content"] == "hi"


def test_client_rebuilt_lazily_after_close():
    """关闭后再次取用会懒重建——provider 实例被复用时不会踩"client has been closed"。"""
    p = _provider()
    first = p._client
    p._client.stream = lambda *a, **kw: _FakeStreamingResponse([_openai_line("x")])
    asyncio.run(_consume(p, ChatRequest(messages=[], model="kimi-k3")))
    assert first.is_closed is True

    second = p._ensure_client()
    assert second is not first
    assert second.is_closed is False
    asyncio.run(p._close_client())  # 收尾，避免遗留未关闭连接


def test_stream_cancel_closes_client():
    """消费者取消（用户中断）时，客户端同样被关闭，不留悬挂连接。"""
    p = _provider()
    client = p._client
    p._client.stream = lambda *a, **kw: _FakeStreamingResponse([_openai_line("a")], hang=True)

    async def _run_then_cancel():
        task = asyncio.create_task(_consume(p, ChatRequest(messages=[], model="kimi-k3")))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_run_then_cancel())
    assert client.is_closed is True


def test_concurrent_streams_are_isolated():
    """两个并发流（等价两个会话）各自独立客户端，内容互不串味。"""
    p1, p2 = _provider(), _provider()
    c1, c2 = p1._client, p2._client
    assert c1 is not c2  # 会话各持一条连接，不共享

    p1._client.stream = lambda *a, **kw: _FakeStreamingResponse([_openai_line("A")])
    p2._client.stream = lambda *a, **kw: _FakeStreamingResponse([_openai_line("B")])

    async def _both():
        return await asyncio.gather(
            _consume(p1, ChatRequest(messages=[], model="kimi-k3")),
            _consume(p2, ChatRequest(messages=[], model="kimi-k3")),
        )

    ev1, ev2 = asyncio.run(_both())
    content1 = [e for e in ev1 if e["type"] == "done"][0]["content"]
    content2 = [e for e in ev2 if e["type"] == "done"][0]["content"]
    assert content1 == "A" and content2 == "B"
    # 两条流结束后各自关闭，互不影响
    assert c1.is_closed is True and c2.is_closed is True
