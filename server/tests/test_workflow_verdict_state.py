"""评估结论解析、返工决策与状态推进（§4.2 / §5.3 / §5.6）：纯逻辑测试。"""

from app.orchestration.workflow.state import (
    NodeView,
    derive_flow_status,
    derive_program_status,
    ready_node_keys,
)
from app.orchestration.workflow.verdict import (
    decide_review,
    issues_as_feedback,
    parse_verdict,
)

_GOOD = (
    '结论如下：\n```json\n{"decision": "pass", "summary": "通过", '
    '"criteria": [{"id": "C1", "result": "met", "evidence": "pytest 通过"}], "issues": []}\n```'
)


def test_parse_verdict_takes_last_valid_object():
    text = 'noise {"a": 1} ' + _GOOD
    verdict = parse_verdict(text)
    assert verdict is not None
    assert verdict["decision"] == "pass"
    assert verdict["criteria"][0]["result"] == "met"


def test_parse_verdict_rejects_bad_contract():
    assert parse_verdict('{"decision": "maybe"}') is None
    assert parse_verdict('{"decision": "pass", "criteria": [{"id": "C1", "result": "??"}]}') is None
    assert parse_verdict('{"decision": "rework", "issues": [{"severity": "fatal"}]}') is None
    assert parse_verdict("没有任何 JSON") is None


def test_pass_is_overridden_when_checks_fail():
    verdict = parse_verdict(_GOOD)
    decision = decide_review(verdict, checks_passed=False, rework_round=0, max_rework=2)
    assert decision.action == "rework"
    assert "确定性检查" in decision.reason


def test_pass_is_overridden_by_blocker_or_unmet_criterion():
    blocker = parse_verdict(
        '{"decision": "pass", "criteria": [], "issues": [{"severity": "blocker", "problem": "x"}]}'
    )
    assert (
        decide_review(blocker, checks_passed=True, rework_round=0, max_rework=2).action == "rework"
    )
    unmet = parse_verdict(
        '{"decision": "pass", "criteria": [{"id": "C1", "result": "unmet"}], "issues": []}'
    )
    assert decide_review(unmet, checks_passed=True, rework_round=0, max_rework=2).action == "rework"


def test_exhausted_rework_goes_to_gate():
    verdict = parse_verdict('{"decision": "rework", "criteria": [], "issues": []}')
    decision = decide_review(verdict, checks_passed=True, rework_round=2, max_rework=2)
    assert decision.action == "gate"
    assert "已用尽" in decision.reason


def test_missing_verdict_is_rework_and_escalate_is_gate():
    assert decide_review(None, checks_passed=True, rework_round=0, max_rework=2).action == "rework"
    esc = parse_verdict('{"decision": "escalate", "criteria": [], "issues": []}')
    assert decide_review(esc, checks_passed=True, rework_round=0, max_rework=2).action == "gate"


def test_feedback_lists_issues_and_unmet_criteria():
    verdict = parse_verdict(
        '{"decision": "rework", "criteria": '
        '[{"id": "C2", "result": "unmet", "evidence": "缺少 409"}],'
        ' "issues": [{"severity": "major", "file": "a.py", "line": 3, "problem": "缺校验",'
        ' "fix_hint": "加判断"}]}'
    )
    text = issues_as_feedback(verdict)
    assert "a.py:3" in text and "缺校验" in text and "[验收未满足] C2" in text


def test_ready_nodes_require_upstream_done_and_flow_ready():
    nodes = [
        NodeView("explore", "done", ()),
        NodeView("impl", "pending", ("explore",)),
        NodeView("review", "pending", ("impl",)),
    ]
    assert ready_node_keys(nodes, flow_ready=True) == ["impl"]
    assert ready_node_keys(nodes, flow_ready=False) == []
    blocked = [NodeView("explore", "running", ()), NodeView("impl", "pending", ("explore",))]
    assert ready_node_keys(blocked, flow_ready=True) == []


def test_flow_and_program_status_derivation():
    assert derive_flow_status(["done", "skipped"]) == "succeeded"
    assert derive_flow_status(["done", "running"]) == "running"
    assert derive_flow_status(["done", "interrupted"]) == "paused"
    assert derive_flow_status(["done", "waiting_human"]) == "waiting_human"
    assert derive_flow_status(["done", "failed"]) == "failed"
    assert derive_flow_status(["pending"], cancelled=True) == "cancelled"
    assert derive_program_status(["succeeded", "running"]) == "running"
    assert derive_program_status(["succeeded", "paused"]) == "paused"
    assert derive_program_status(["succeeded", "cancelled"]) == "succeeded"
    assert derive_program_status(["cancelled", "cancelled"]) == "cancelled"
    assert derive_program_status(["succeeded", "failed"]) == "failed"
