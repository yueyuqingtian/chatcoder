"""plan-20261103-fork：工具序列的少量边界回归，不访问真实供应商。"""
import pytest
from app.models.schemas import ChatMessage
from app.orchestration.history_sequence import prepare_history, validate_tool_sequence, validate_anthropic_wire, HistorySequenceError


def test_parallel_results_can_finish_in_reverse_order():
    rows = [ChatMessage(role="user", content="读文件"),
            ChatMessage(role="assistant", reasoning_content="先检查", tool_calls=[{"id":"a","name":"fs_read","arguments":{}}, {"id":"b","name":"fs_read","arguments":{}}]),
            ChatMessage(role="tool", tool_call_id="b", content="二"),
            ChatMessage(role="tool", tool_call_id="a", content="一")]
    assert prepare_history(rows) is rows
    validate_tool_sequence(rows)


def test_repairs_copy_without_changing_original():
    rows = [ChatMessage(role="user", content="读文件"),
            ChatMessage(role="assistant", reasoning_content="保留思考", tool_calls=[{"id":"a","name":"fs_read","arguments":{}}])]
    before = [m.model_dump() for m in rows]
    result = prepare_history(rows)
    validate_tool_sequence(result)
    assert [m.model_dump() for m in rows] == before
    assert result[1].reasoning_content == "保留思考"


def test_wire_rejects_orphan_and_checks_anthropic_blocks():
    with pytest.raises(HistorySequenceError):
        validate_tool_sequence([{"role":"tool", "tool_call_id":"orphan"}])
    validate_anthropic_wire([
        {"role":"assistant", "content":[{"type":"tool_use", "id":"a"}, {"type":"tool_use", "id":"b"}]},
        {"role":"user", "content":[{"type":"tool_result", "tool_use_id":"b"}, {"type":"tool_result", "tool_use_id":"a"}]},
    ])
