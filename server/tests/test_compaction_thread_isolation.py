"""上下文压缩的线程隔离与候选过滤回归用例（plan-560-2311 A1/A2/A3）。

plan-560-2311 A1：`_summarize_with_llm` 声明返回 `(摘要, 记忆)` 二元组，但空 replay 分支
曾只返回空字符串，调用方 `summary, mems = await ...` 直接抛 ValueError。回归点因此是
「能不能安全解包」，不是「摘要写了什么」——用只含 THINKING 的最小输入把 replay 构造成空。

plan-560-2311 A2：子代理触发压缩时会读写主线程（`thread_id IS NULL`）的消息——把主会话
历史写进 `compacted_ids`、把 SUMMARY 卡写回主线程、改写主会话 `shared_context`，而子代理
自己的线程完全没被压缩。回归点因此是「子代理身份下根本不去查会话行」：用带计数的假 db
断言 `db.get` 没被调用。比断言压缩结果更可靠——结果为 None 可能只是「压不动」，
无法区分「没压」与「压了但无收益」。

plan-560-2311 A3：`emergency_compact_session` 的候选筛选曾漏掉图片过滤，与
`compact_session` 口径不一致，紧急压缩会把含图消息遮蔽且不可恢复。回归点因此是两条
路径的一致性：图片消息都不得进入 shadowed 集合。
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.enums import MsgType  # noqa: E402
from app.orchestration import context_compressor as cc  # noqa: E402


class _Msg:
    """满足 `_summarize_with_llm` / `_is_image_message` 读取口径的最小消息对象。"""

    def __init__(self, mid: int, msg_type: str, content: dict):
        self.id = mid
        self.msg_type = msg_type
        self.content = content
        self.sender_type = "agent"


# ── A1：空 replay 必须返回二元组 ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_summarize_with_llm_empty_replay_returns_pair():
    """只含 THINKING 的消息构造不出 replay，必须返回 ("", [])。

    plan-560-2311 A1：THINKING 在 `_SUMMARY_SKIP_TYPES` 里，既不进 replay 也不累积
    pending_text，正好把 replay 走成空——这是能触发该分支的最小输入，不需要 provider。
    """
    messages = [_Msg(i, MsgType.THINKING.value, {"text": f"内部思考 {i}"}) for i in range(1, 4)]

    result = await cc._summarize_with_llm(None, None, messages)

    # A1 的直接回归点：调用方的二元解包不得抛 ValueError
    summary, memories = result
    assert summary == ""
    assert memories == []
    assert isinstance(result, tuple) and len(result) == 2


@pytest.mark.asyncio
async def test_summarize_with_llm_empty_message_list_returns_pair():
    """空消息列表走的是另一个提前返回分支，返回契约必须同样成立。"""
    summary, memories = await cc._summarize_with_llm(None, None, [])
    assert (summary, memories) == ("", [])


# ── A2：子代理不进入落库式压缩 ─────────────────────────────────────────────────

class _CountingDB:
    """只统计 `get` 调用的假 db：A2 的回归点是「有没有去查会话行」。"""

    def __init__(self) -> None:
        self.get_calls: list[object] = []

    async def get(self, model, key):
        self.get_calls.append(key)
        return None  # 即使被调用也拿不到会话行，不会真的压


async def _compact(db, **overrides):
    """按 `_compact_persistent_or_fallback` 的真实签名调用，只覆盖与线程身份相关的入参。"""
    from app.orchestration.agent_loop import _compact_persistent_or_fallback

    kwargs: dict = {
        "session_id": 101,
        "provider": None,
        "agent_window": 200_000,
        "used_tokens": 5_000,
        "agent_id": 2,
        "agent_name": "sub-a",
        "turn_id": 7,
        "messages": [],
    }
    kwargs.update(overrides)
    return await _compact_persistent_or_fallback(db, **kwargs)


@pytest.mark.asyncio
async def test_subagent_thread_skips_persistent_compaction():
    """thread_id 非空 = 子代理线程，不得查会话行（否则会压到主线程消息上）。"""
    db = _CountingDB()

    result, messages = await _compact(db, thread_id=55, agent_kind="sub")

    assert db.get_calls == [], "子代理压缩不能查主会话行，否则会把主线程历史写进 compacted_ids"
    assert result is None
    assert messages == []


@pytest.mark.asyncio
async def test_subagent_kind_skips_persistent_compaction_even_without_thread():
    """agent_kind != "main" 单独一条也要拦住：两个判据是「或」的关系，缺一即可绕开。"""
    db = _CountingDB()

    result, _ = await _compact(db, thread_id=None, agent_kind="sub")

    assert db.get_calls == []
    assert result is None


@pytest.mark.asyncio
async def test_subagent_overflow_trigger_also_skips_persistent_compaction():
    """溢出恢复（context-overflow）路径同样不能落库压缩，两条触发路径都要守。"""
    db = _CountingDB()

    result, _ = await _compact(db, thread_id=9, agent_kind="sub", trigger="context-overflow")

    assert db.get_calls == []
    assert result is None


@pytest.mark.asyncio
async def test_main_thread_enters_persistent_path():
    """主代理身份（thread_id=None 且 agent_kind="main"）会进入落库路径——对照组。

    没有这条对照，「子代理不查库」的断言可能因为别的原因恒真（例如参数名写错）。
    """
    db = _CountingDB()

    result, _ = await _compact(db, thread_id=None, agent_kind="main")

    assert len(db.get_calls) == 1, "主代理必须尝试取会话行，否则落库式压缩永远不会生效"
    assert result is None  # 假 db 取不到行，安全回退内存式


# ── A3：图片消息不参与紧急压缩 ─────────────────────────────────────────────────

class _FakeResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def scalars(self):
        return self

    def all(self) -> list:
        return list(self._rows)


class _RowsDB:
    """按 `_fetch_main_messages` 的调用形态返回固定消息集（不需要真实库）。"""

    def __init__(self, rows: list) -> None:
        self._rows = rows

    async def execute(self, stmt):
        # `_fetch_main_messages` 会 reversed 一次，这里先反一次，最终得到正序
        return _FakeResult(list(reversed(self._rows)))


def _tool_rounds(pairs: int = 8) -> list[_Msg]:
    """构造 pairs 个 (tool_call, tool_result) 回合——call 位置数 > 6 才会触发溢出压缩。"""
    rows: list[_Msg] = []
    for i in range(pairs):
        rows.append(_Msg(len(rows) + 1, MsgType.TOOL_CALL.value,
                         {"tool": "fs_read", "args": {}, "call_key": f"k{i}"}))
        rows.append(_Msg(len(rows) + 1, MsgType.TOOL_RESULT.value,
                         {"tool": "fs_read", "output": "r" * 200, "call_key": f"k{i}"}))
    return rows


@pytest.mark.asyncio
async def test_emergency_compact_excludes_image_messages(monkeypatch):
    """带图片附件的消息不得进入 shadowed 集合（图片被遮蔽后无法恢复）。

    plan-560-2311 A3：把图片消息放在消息链最前面。若候选没过滤它，它会占据
    shadowed 的第一个位置（切点由 tool_call 位置决定，与图片消息无关），
    因此「不在 shadowed 里」就是筛选生效的直接证据。
    """
    image_msg = _Msg(1, MsgType.TEXT.value, {
        "text": "看这张图",
        "attachments": [{"type": "image", "filename": "shot.png", "path": "u/shot.png"}],
    })
    rows = [image_msg, *_tool_rounds()]
    # 重新编号，保证 id 唯一且图片消息排在最前
    for idx, m in enumerate(rows, start=1):
        m.id = idx

    captured: dict = {}

    async def _fake_commit(db, *, session, provider, shadowed, **kwargs):
        captured["shadowed"] = list(shadowed)
        captured["retained"] = list(kwargs.get("retained_messages") or [])
        return {}

    monkeypatch.setattr(cc, "_commit_compaction", _fake_commit)

    session = SimpleNamespace(id=1, shared_context=None)
    await cc.emergency_compact_session(
        _RowsDB(rows), session=session, provider=None,
        context_window=200_000, used_tokens=1_000,
    )

    shadowed_ids = [m.id for m in captured["shadowed"]]
    assert shadowed_ids, "本用例的前提是本次确实发生了溢出压缩"
    assert image_msg.id not in shadowed_ids, "含图片附件的消息不得被紧急压缩遮蔽"


def test_both_compaction_paths_share_image_filter():
    """两条落库式压缩路径的候选过滤都必须引用 `_is_image_message`。

    plan-560-2311 A3 的成因就是两份实现漂移：`compact_session` 有过滤、
    `emergency_compact_session` 漏了。这里直接钉住「两处都在用同一个判据」，
    避免以后又被改回两套口径。
    """
    for func in (cc.compact_session, cc.emergency_compact_session):
        source = inspect.getsource(func)
        assert "_is_image_message" in source, f"{func.__name__} 的候选过滤缺少图片判据"


def test_is_image_message_recognizes_attachment_types():
    """`_is_image_message` 的两个判据（type=image / 图片扩展名）都要认。"""
    from app.orchestration.context_memory import _is_image_message

    assert _is_image_message(_Msg(1, MsgType.TEXT.value, {
        "text": "x", "attachments": [{"type": "image"}],
    })) is True
    assert _is_image_message(_Msg(2, MsgType.TEXT.value, {
        "text": "x", "attachments": [{"filename": "a.PNG"}],
    })) is True
    assert _is_image_message(_Msg(3, MsgType.TEXT.value, {"text": "纯文本"})) is False
    assert _is_image_message(_Msg(4, MsgType.TEXT.value, {
        "text": "x", "attachments": [{"filename": "a.pdf"}],
    })) is False
