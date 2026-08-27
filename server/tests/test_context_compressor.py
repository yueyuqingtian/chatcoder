"""context_compressor 落库式压缩单测。

覆盖：region 选择（token 预算 + tool 配对边界）、配对平衡判定、降级摘要。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.enums import MsgType, SenderType
from app.orchestration.context_compressor import (
    _build_transcript,
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
