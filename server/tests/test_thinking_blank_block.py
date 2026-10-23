"""思考块「展开空白」根因回归（plan-20261023-thinking-blank）。

现象：执行任务时部分「思考过程」块展开后一片空白，另一些正常。
根因（实测数据库确认）：库里存在 text 仅为单个空格的 thinking 消息。
流式期间思考内容已广播给前端（ticker 有字），但网关在结束帧回一个纯空白
reasoning 占位；`_stream_chat_and_broadcast` 用 `event.get("thinking") or full_thinking`
取值时，空白字符串是 truthy，于是把已累积的完整思考整个覆盖掉，
最后 `thinking.done` 广播与落库都只剩空格。

本文件覆盖：
1. 判据函数 `_is_substantive_thinking`；
2. 结束帧的空白 thinking 不覆盖流式累积值；
3. 纯空白思考不广播 thinking.done、也不返回给上层（因而不落库）；
4. 正文侧同类问题（空白 content 不覆盖累积正文、不广播 token.done）；
5. 真正的思考内容照常保留（正文含内部换行 / 缩进不被 strip 掉）。
"""
from app.models.schemas import ChatRequest
from app.orchestration import agent_loop
from app.orchestration.agent_loop import (
    _is_substantive_thinking,
    _stream_chat_and_broadcast,
)


class _FakeProvider:
    """按脚本产事件的假 provider（模仿 stream_structured 的 yield 形态）。"""

    def __init__(self, events: list[dict]):
        self._events = events

    async def stream_structured(self, request: ChatRequest):
        for ev in self._events:
            yield ev


async def _stream(events: list[dict], collected: list[dict], *, reply_language: str = "auto"):
    """跑一次流式调用，收集广播事件并返回 ChatResponse。"""

    async def _collect(_session_id, event):
        collected.append(event)

    # agent_loop 用 `from agent_events import broadcast` 引用了函数本身，
    # 所以必须替换 agent_loop 模块上的名字，替换 agent_events 上的无效。
    original = agent_loop.broadcast
    agent_loop.broadcast = _collect
    try:
        return await _stream_chat_and_broadcast(
            _FakeProvider(events),
            ChatRequest(messages=[], model="m"),
            session_id=1, turn_id=1, agent_id=1, agent_name="a",
            reply_language=reply_language,
        )
    finally:
        agent_loop.broadcast = original


# ── 判据函数 ──

def test_substantive_thinking_rejects_whitespace():
    assert _is_substantive_thinking(" ") is False
    assert _is_substantive_thinking("\n\t  \n") is False
    assert _is_substantive_thinking("") is False
    assert _is_substantive_thinking(None) is False


def test_substantive_thinking_accepts_real_text():
    assert _is_substantive_thinking("继续排查根因") is True
    assert _is_substantive_thinking("  leading space then text  ") is True


# ── 思考侧 ──

async def test_blank_done_thinking_does_not_override_accumulated():
    """结束帧 thinking 只有一个空格时，必须保留流式累积的真实思考。"""
    collected: list[dict] = []
    resp = await _stream([
        {"type": "thinking", "delta": "让我先读取相关文件。\n再核对写入路径。"},
        {"type": "done", "content": "完成", "thinking": " ", "tool_calls": [],
         "finish_reason": "stop"},
    ], collected)

    assert "让我先读取相关文件" in resp.thinking
    done_events = [e for e in collected if e["event"] == "thinking.done"]
    assert len(done_events) == 1
    assert "让我先读取相关文件" in done_events[0]["payload"]["full_text"]


async def test_blank_thinking_never_broadcasts_done_or_persists():
    """全程只有空白思考：不广播 thinking.done，返回 None（上游据此不落库）。"""
    collected: list[dict] = []
    resp = await _stream([
        {"type": "thinking", "delta": " "},
        {"type": "done", "content": "完成", "thinking": " ", "tool_calls": [],
         "finish_reason": "stop"},
    ], collected)

    assert resp.thinking is None
    assert not [e for e in collected if e["event"] == "thinking.done"]


async def test_real_thinking_keeps_inner_newlines():
    """真实思考照常广播与返回，正文内部换行与缩进不被破坏。"""
    collected: list[dict] = []
    text = "第一行。\n\n  缩进的第二行。"
    resp = await _stream([
        {"type": "thinking", "delta": text},
        {"type": "done", "content": "完成", "thinking": text, "tool_calls": [],
         "finish_reason": "stop"},
    ], collected)

    assert resp.thinking == text
    assert [e for e in collected if e["event"] == "thinking.done"]


# ── 正文侧同类问题 ──

async def test_blank_done_content_does_not_override_accumulated():
    """结束帧 content 只有空白时，保留流式累积的正文。"""
    collected: list[dict] = []
    resp = await _stream([
        {"type": "content", "delta": "这是一段已经展示给用户的正文。"},
        {"type": "done", "content": " ", "thinking": "", "tool_calls": [],
         "finish_reason": "stop"},
    ], collected)

    assert resp.content == "这是一段已经展示给用户的正文。"
    assert [e for e in collected if e["event"] == "token.done"]


async def test_blank_content_never_broadcasts_token_done():
    """全程没有实质正文：不广播 token.done（避免前端被空白覆盖成空消息）。"""
    collected: list[dict] = []
    await _stream([
        {"type": "done", "content": " ", "thinking": "", "tool_calls": [],
         "finish_reason": "stop"},
    ], collected)

    assert not [e for e in collected if e["event"] == "token.done"]
