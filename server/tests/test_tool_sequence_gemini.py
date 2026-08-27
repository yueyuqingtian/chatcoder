from app.models.schemas import ChatMessage
from app.orchestration.compaction import (
    ensure_tool_pairing,
    normalize_tool_sequence,
    repair_tool_call_ids,
)


def test_normalize_tool_sequence_gemini_adjacent_assistant():
    """测试 Gemini 协议场景：连续 assistant 消息智能合并，杜绝 400"""
    msgs = [
        ChatMessage(role="system", content="sys"),
        ChatMessage(role="user", content="hello"),
        ChatMessage(role="assistant", content="好的，我来帮你。"),
        ChatMessage(role="assistant", content=None, tool_calls=[{"id": "c1", "name": "fs_list", "arguments": {}}]),
        ChatMessage(role="tool", content="[file1]", tool_call_id="c1"),
    ]

    fixed = repair_tool_call_ids(msgs)
    fixed = ensure_tool_pairing(fixed)
    fixed = normalize_tool_sequence(fixed)

    assert len(fixed) == 4
    assert fixed[0].role == "system"
    assert fixed[1].role == "user"
    assert fixed[2].role == "assistant"
    assert fixed[2].content == "好的，我来帮你。"
    assert fixed[2].tool_calls == [{"id": "c1", "name": "fs_list", "arguments": {}}]
    assert fixed[3].role == "tool"
    assert fixed[3].tool_call_id == "c1"


def test_normalize_tool_sequence_consecutive_users():
    """测试连续 user 消息合并"""
    msgs = [
        ChatMessage(role="user", content="msg 1"),
        ChatMessage(role="user", content="msg 2"),
    ]
    fixed = normalize_tool_sequence(msgs)
    assert len(fixed) == 1
    assert fixed[0].role == "user"
    assert fixed[0].content == "msg 1\n\nmsg 2"


def test_normalize_tool_sequence_assistant_after_system():
    """测试 assistant(tool_calls) 紧随 system 时自动插入 user 消息满足 Gemini 要求"""
    msgs = [
        ChatMessage(role="system", content="sys prompt"),
        ChatMessage(role="assistant", content=None, tool_calls=[{"id": "c2", "name": "fs_read", "arguments": {}}]),
        ChatMessage(role="tool", content="data", tool_call_id="c2"),
    ]
    fixed = normalize_tool_sequence(msgs)
    assert len(fixed) == 4
    assert fixed[0].role == "system"
    assert fixed[1].role == "user"  # 自动插入的 user
    assert fixed[2].role == "assistant"
    assert fixed[2].tool_calls is not None
    assert fixed[3].role == "tool"


if __name__ == "__main__":
    test_normalize_tool_sequence_gemini_adjacent_assistant()
    test_normalize_tool_sequence_consecutive_users()
    test_normalize_tool_sequence_assistant_after_system()
    print("All sequence tests passed successfully!")
