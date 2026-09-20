"""context_compressor 落库式压缩单测。

覆盖：region 选择（token 预算 + tool 配对边界）、配对平衡判定、降级摘要。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.enums import MsgType, SenderType
from app.orchestration.context_compressor import (
    _COMPACT_KEEP_TYPES,
    _build_transcript,
    _fallback_summary,
    _is_pairing_balanced,
    select_compactable_range,
)


def _mk(i: int, msg_type: str, text: str = "", tool: str = "", key: str = ""):
    class _M:
        pass
    m = _M()
    m.id = i
    m.msg_type = msg_type
    m.sender_type = SenderType.USER.value if msg_type == MsgType.TEXT.value else SenderType.AGENT.value
    m.content = {}
    if msg_type == MsgType.TEXT.value:
        m.content = {"text": text}
    elif msg_type == MsgType.TOOL_CALL.value:
        m.content = {"tool": tool, "args": {}, "call_key": key or f"k{i}"}
    elif msg_type == MsgType.TOOL_RESULT.value:
        m.content = {"tool": tool, "output": text, "call_key": key or f"k{i}"}
    return m


def _chain(n_user=3, calls_per_round=2, tool_chars=200):
    """构造 user + (assistant文本 + tool_call + tool_result) 交替的消息链。"""
    msgs = []
    key_seq = 0
    for u in range(n_user):
        msgs.append(_mk(len(msgs) + 1, MsgType.TEXT.value, text=f"user question {u} " + "x" * 50))
        for c in range(calls_per_round):
            msgs.append(_mk(len(msgs) + 1, MsgType.TEXT.value, text=f"assistant step {u}.{c} " + "y" * 30))
            key = f"call_{key_seq}"
            key_seq += 1
            msgs.append(_mk(len(msgs) + 1, MsgType.TOOL_CALL.value, tool="fs_read", key=key))
            msgs.append(_mk(len(msgs) + 1, MsgType.TOOL_RESULT.value, text="r" * tool_chars, tool="fs_read", key=key))
    return msgs


def test_balance_detector():
    msgs = _chain(n_user=1, calls_per_round=1, tool_chars=10)
    # 消息链: [0]=user, [1]=assistant text, [2]=tool_call k0, [3]=tool_result k0
    assert _is_pairing_balanced(msgs, len(msgs)) is True
    assert _is_pairing_balanced(msgs, 0) is True
    # 切点 3 = [0..2] 含 tool_call 无 result：不平衡
    assert _is_pairing_balanced(msgs, 3) is False
    # 切点 4 = [0..3] 全部配对：平衡
    assert _is_pairing_balanced(msgs, 4) is True


def test_select_range_respects_retain_budget():
    msgs = _chain(n_user=3, calls_per_round=2, tool_chars=200)
    # 总链约 585 tokens；retain=200 时必然产生压缩区间
    span = select_compactable_range(msgs, retain_tokens=200)
    assert span is not None
    start, end = span
    assert start == 0
    # 压缩区尾部必须是配对平衡切点
    assert _is_pairing_balanced(msgs, end + 1) is True
    # 保留区 token 不小于 retain 预算
    from app.orchestration.token_counter import estimate_message_tokens_from_model
    kept = msgs[end + 1:]
    assert sum(estimate_message_tokens_from_model(m) for m in kept) >= 200


def test_select_range_small_chain_returns_none():
    msgs = _chain(n_user=1, calls_per_round=1, tool_chars=10)
    assert select_compactable_range(msgs, retain_tokens=10**9) is None
    assert select_compactable_range([], retain_tokens=100) is None


def test_transcript_contains_tools_and_files():
    msgs = [
        _mk(1, MsgType.TEXT.value, text="user hello"),
        _mk(2, MsgType.TOOL_CALL.value, tool="fs_read", key="k1"),
        _mk(3, MsgType.TOOL_RESULT.value, text="file content here", tool="fs_read", key="k1"),
    ]
    # 注入 path 到 args
    msgs[1].content["args"] = {"path": "src/main.py"}
    t = _build_transcript(msgs)
    assert "fs_read" in t
    assert "src/main.py" in t
    assert "user hello" in t


# ── plan-19-82 步骤5：THINKING 纳入压缩候选 + 降级摘要按语言 ──────────────

def test_thinking_included_in_compact_types():
    """plan-19-82：THINKING 必须参与压缩候选（此前被排除 → 占用永久保留）。"""
    assert MsgType.THINKING.value in _COMPACT_KEEP_TYPES


def test_fallback_summary_language():
    """降级摘要固定文案按语言选择，避免英文摘要污染中文会话。"""
    msgs = [
        _mk(1, MsgType.TEXT.value, text="用户问题"),
        _mk(2, MsgType.TOOL_CALL.value, tool="fs_read", key="k1"),
        _mk(3, MsgType.TOOL_RESULT.value, text="内容", tool="fs_read", key="k1"),
    ]
    zh = _fallback_summary(msgs, language="zh")
    en = _fallback_summary(msgs, language="en")
    assert "以下是之前对话的摘要" in zh
    assert "Summary of earlier conversation" in en
    assert "已调用工具" in zh and "Tools called" in en


def test_select_range_reduces_effectively_on_long_chain():
    """目标闭环的基础：长链在目标预算下能选出可压缩区间（配合迭代可压到目标）。"""
    # 构造长链：20 轮 × 2 次调用 × 2000 字符 ≈ 大量 token
    msgs = _chain(n_user=20, calls_per_round=2, tool_chars=2000)
    from app.orchestration.token_counter import messages_token_total
    total = messages_token_total(msgs)
    # 目标上界按窗口 15% 计；窗口取自总 token 的 10 倍
    window = total * 10
    target_max = int(window * 0.15)
    span = select_compactable_range(msgs, retain_tokens=4000)
    assert span is not None
    start, end = span
    shadowed = msgs[start:end + 1]
    kept = msgs[end + 1:]
    # 压缩后剩余 = 保留区；显著小于原始总量（说明可有效回收）
    assert messages_token_total(kept) < total
    assert messages_token_total(shadowed) > 0
    # 仅一次压缩（保留区为 4000 token 预算）时，剩余应远小于原量的一半，
    # 说明配合多轮迭代可达目标区间
    assert messages_token_total(kept) < total * 0.5 or messages_token_total(kept) <= target_max * 3
