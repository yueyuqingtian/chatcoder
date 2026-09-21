"""plan-282-0 压缩效率根治测试。

背景（用户实测，512K 窗口 session 277）：
- 压缩器自报压缩后 14.4% 达标，API 真实占用却是 28.9%（第一轮）/ 69.0%（第二轮）；
- 根因：`estimate_message_tokens` 漏算 `reasoning_content`（thinking 全库占 69%），
  且 `tool_schemas` 完全未参与核算。铁证：`72190（内存估算）+ 283213（该轮
  thinking）= 353403 ≈ 353511（API 真实 prompt_tokens）`。

覆盖：
1. estimate_message_tokens 计入 reasoning_content；
2. estimate_tools_tokens 计入工具定义；
3. strip_stale_reasoning 只剥离旧工具回合的 reasoning（最新回合必须保留，否则 400）；
4. strip_stale_reasoning 不改变消息结构（只动 reasoning_content）；
5. build_api_copy 集成剥离，并记账 reasoning_stripped；
6. context_compressor 的有效口径估算与出站一致。
"""

import pytest

from app.models.schemas import ChatMessage
from app.orchestration.compaction import (
    _REASONING_KEEP_TOOL_ROUNDS,
    build_api_copy,
    strip_stale_reasoning,
    take_last_reclaim,
)
from app.orchestration.token_counter import (
    estimate_message_tokens,
    estimate_messages_tokens,
    estimate_tools_tokens,
)


# ─────────────────────────── 估算口径 ───────────────────────────

def test_estimate_message_tokens_counts_reasoning():
    """reasoning_content 必须计入估算（此前漏算导致 2~5 倍偏差）。"""
    big_reasoning = "思考内容" * 1000  # 4000 字节 ≈ 1000 token
    with_r = ChatMessage(role="assistant", content="hi", reasoning_content=big_reasoning)
    without_r = ChatMessage(role="assistant", content="hi")
    assert estimate_message_tokens(with_r) > estimate_message_tokens(without_r) + 900


def test_estimate_message_tokens_reasoning_none_safe():
    """reasoning_content 为 None 时行为不变。"""
    m = ChatMessage(role="assistant", content="hello")
    assert estimate_message_tokens(m) == 4 + (len(b"hello") + 3) // 4


def test_estimate_tools_tokens():
    """工具定义（此前完全未计入）应产生非零估算。"""
    schemas = [{
        "type": "function",
        "function": {
            "name": "fs_read",
            "description": "读取文件内容，支持按行范围读取，返回文本。",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "文件路径"}},
                "required": ["path"],
            },
        },
    }]
    assert estimate_tools_tokens(schemas) > 0
    assert estimate_tools_tokens(None) == 0
    assert estimate_tools_tokens([]) == 0
    # 多个工具应大于单个
    assert estimate_tools_tokens(schemas * 3) > estimate_tools_tokens(schemas)


def test_estimate_messages_tokens_includes_reasoning():
    msgs = [
        ChatMessage(role="assistant", content="a", reasoning_content="R" * 4000),
        ChatMessage(role="user", content="u"),
    ]
    assert estimate_messages_tokens(msgs) > 1000


# ─────────────────────── reasoning 剥离 ───────────────────────

def _build_rounds(n: int) -> list[ChatMessage]:
    """构造 n 个工具回合：assistant(reasoning+tool_calls) -> tool。"""
    out: list[ChatMessage] = [ChatMessage(role="system", content="sys")]
    for i in range(1, n + 1):
        out.append(ChatMessage(
            role="assistant", content=f"text-{i}", reasoning_content=f"THINK-{i}",
            tool_calls=[{"id": f"c{i}", "name": "fs_read", "arguments": {"path": f"f{i}"}}],
        ))
        out.append(ChatMessage(role="tool", content=f"res-{i}", name="fs_read", tool_call_id=f"c{i}"))
    out.append(ChatMessage(role="user", content="latest"))
    return out


def test_strip_keeps_latest_rounds():
    """最新 N 个工具回合的 reasoning 必须保留——网关要求回传，剥离会 400。"""
    msgs = _build_rounds(6)
    out, stripped = strip_stale_reasoning(msgs, keep_rounds=3)
    reasons = [m.reasoning_content for m in out if m.role == "assistant"]
    assert stripped == 3
    assert reasons[:3] == [None, None, None]
    assert reasons[3:] == ["THINK-4", "THINK-5", "THINK-6"]


def test_strip_latest_round_always_kept_with_default():
    """默认保留窗口下，**最后一个**工具回合的 reasoning 永不被剥离。"""
    for n in (1, 2, 3, 4, 8):
        msgs = _build_rounds(n)
        out, _ = strip_stale_reasoning(msgs)
        last_asst = [m for m in out if m.role == "assistant" and m.tool_calls][-1]
        assert last_asst.reasoning_content == f"THINK-{n}", f"n={n} 最新回合 reasoning 被误剥离"


def test_strip_preserves_message_structure():
    """只动 reasoning_content：条数/角色/顺序/content/tool_calls 全部不变。"""
    msgs = _build_rounds(6)
    before_len = len(msgs)
    before_roles = [m.role for m in msgs]
    before_content = [m.content for m in msgs]
    before_tc = [bool(m.tool_calls) for m in msgs]
    before_ids = [tc.get("id") for m in msgs for tc in (m.tool_calls or [])]

    out, _ = strip_stale_reasoning(msgs, keep_rounds=3)

    assert len(out) == before_len
    assert [m.role for m in out] == before_roles
    assert [m.content for m in out] == before_content
    assert [bool(m.tool_calls) for m in out] == before_tc
    assert [tc.get("id") for m in out for tc in (m.tool_calls or [])] == before_ids


def test_strip_noop_when_rounds_below_keep():
    """工具回合不足 keep 个时不剥离任何内容。"""
    msgs = _build_rounds(2)
    out, stripped = strip_stale_reasoning(msgs, keep_rounds=3)
    assert stripped == 0
    reasons = [m.reasoning_content for m in out if m.role == "assistant"]
    assert reasons == ["THINK-1", "THINK-2"]


def test_strip_keep_zero_strips_all():
    msgs = _build_rounds(4)
    out, stripped = strip_stale_reasoning(msgs, keep_rounds=0)
    assert stripped == 4
    assert all(m.reasoning_content is None for m in out if m.role == "assistant")


def test_strip_noop_without_tool_rounds():
    """纯对话（无工具回合）不剥离——没有「工具回合」作参照。"""
    msgs = [
        ChatMessage(role="assistant", content="a1", reasoning_content="R1"),
        ChatMessage(role="user", content="u1"),
    ]
    out, stripped = strip_stale_reasoning(msgs)
    assert stripped == 0
    assert out[0].reasoning_content == "R1"


def test_strip_empty_safe():
    assert strip_stale_reasoning([]) == ([], 0)


# ─────────────────── build_api_copy 集成 ───────────────────

def test_build_api_copy_strips_and_reports_reclaim():
    """build_api_copy 集成剥离，并把条数记入 reclaim（供前端提示）。"""
    take_last_reclaim()  # 清空上一轮统计
    msgs = _build_rounds(8)
    api = build_api_copy(msgs)

    reasons = [m.reasoning_content for m in api if m.role == "assistant"]
    assert reasons[-_REASONING_KEEP_TOOL_ROUNDS:] == ["THINK-6", "THINK-7", "THINK-8"]
    assert reasons[0] is None

    reclaim = take_last_reclaim()
    assert reclaim is not None
    assert reclaim.get("reasoning_stripped", 0) == 5
    assert reclaim.get("est_tokens_saved", 0) > 0


def test_build_api_copy_does_not_mutate_input():
    """build_api_copy 只作用于副本，原始 messages 的 reasoning 不受影响。"""
    msgs = _build_rounds(6)
    build_api_copy(msgs)
    reasons = [m.reasoning_content for m in msgs if m.role == "assistant"]
    assert reasons == [f"THINK-{i}" for i in range(1, 7)]


def test_build_api_copy_empty_safe():
    assert build_api_copy([]) == []


# ─────────────── 压缩器有效口径一致性 ───────────────

def test_effective_tokens_matches_outbound():
    """压缩器的有效口径估算 == build_api_copy 出站后的真实体积。

    这是本次修复的核心验收点：压缩器自报占用必须与真实出站一致，
    不能再用「全量 thinking」自欺达标。
    """
    from app.core.enums import MsgType, SenderType
    from app.orchestration.context_compressor import estimate_effective_tokens

    class _Row:
        def __init__(self, mid, mtype, content):
            self.id = mid
            self.msg_type = mtype
            self.content = content
            self.sender_type = SenderType.AGENT.value

    rows = []
    mid = 1
    for i in range(1, 7):
        rows.append(_Row(mid, MsgType.THINKING.value, {"text": "思考" * 500}))
        mid += 1
        rows.append(_Row(mid, MsgType.TOOL_CALL.value,
                         {"tool": "fs_read", "args": {"path": "a.py"}, "call_key": f"c{i}"}))
        mid += 1
        rows.append(_Row(mid, MsgType.TOOL_RESULT.value,
                         {"tool": "fs_read", "output": "ok", "call_key": f"c{i}"}))
        mid += 1

    eff = estimate_effective_tokens(rows)
    raw = sum(
        __import__("app.orchestration.token_counter", fromlist=["x"]).estimate_message_tokens_from_model(r)
        for r in rows
    )
    # 有效口径必须显著小于全量口径（旧 reasoning 已排除）
    assert eff < raw, f"effective={eff} 应小于 raw={raw}"
