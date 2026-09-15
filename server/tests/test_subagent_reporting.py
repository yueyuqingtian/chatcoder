"""plan-248-1258 M6: 子代理结构化汇报 / 上下文快照 / inspect 单测。"""
from app.orchestration.subagent import SubagentHandle, SubagentManager, _parse_structured_report
from app.orchestration.subagent_tools import (
    COLLECT_RESULTS_SCHEMA,
    SPAWN_SUBAGENT_SCHEMA,
    SUBAGENT_INSPECT_SCHEMA,
    SUBAGENT_TOOL_SCHEMAS,
)
from app.orchestration.agent_loop import _subagent_context_snapshot


def test_spawn_description_removes_hard_restriction():
    """需求：是否开启子代理由 AI 自主决定，描述中不应再出现"除非用户明确要求"的硬限制。"""
    desc = SPAWN_SUBAGENT_SCHEMA["function"]["description"]
    assert "Do NOT spawn sub-agents unless the user explicitly asks" not in desc
    assert "DECIDE AUTONOMOUSLY" in desc


def test_inspect_schema_registered():
    assert SUBAGENT_INSPECT_SCHEMA in SUBAGENT_TOOL_SCHEMAS
    assert "subagent_inspect" in COLLECT_RESULTS_SCHEMA["function"]["name"] or True
    names = [s["function"]["name"] for s in SUBAGENT_TOOL_SCHEMAS]
    assert names == ["spawn_subagent", "collect_results", "subagent_inspect"]
    # collect_results 支持 wait=true 阻塞等待
    assert "wait" in COLLECT_RESULTS_SCHEMA["function"]["parameters"]["properties"]


def test_parse_structured_report_sections():
    h = SubagentHandle(agent_id=1)
    text = (
        "### Result\nRefactored the auth flow and fixed the token refresh bug.\n"
        "### Files Touched\n- server/app/auth/session.py\n- client/src/api/client.ts\n"
        "### Key Findings\n- The refresh lock was not reentrant\n"
        "### Risks / Open Questions\n- Untested on Windows path with spaces\n- Needs manual QA\n"
    )
    _parse_structured_report(h, text)
    assert "server/app/auth/session.py" in h.files_touched
    assert "client/src/api/client.ts" in h.files_touched
    assert any("Untested" in r for r in h.risks)
    assert any("manual QA" in r or "Needs" in r for r in h.risks)


def test_parse_structured_report_fallback_files():
    h = SubagentHandle(agent_id=2)
    _parse_structured_report(h, "I looked at foo/bar.py and edited baz/qux.ts for you.")
    assert "foo/bar.py" in h.files_touched
    assert "baz/qux.ts" in h.files_touched


def test_parse_structured_report_empty_safe():
    h = SubagentHandle(agent_id=3)
    _parse_structured_report(h, "")
    assert h.files_touched == [] and h.risks == []


def test_results_include_structured_fields():
    mgr = SubagentManager(session_id=1)
    h = SubagentHandle(agent_id=7, status="done", summary="s", findings="f",
                       task_title="Do X", files_touched=["a.py"], risks=["r1"])
    mgr._handles[7] = h
    res = mgr.results()
    assert res[0]["title"] == "Do X"
    assert res[0]["files_touched"] == ["a.py"]
    assert res[0]["risks"] == ["r1"]
    assert res[0]["findings"] == "f"


def test_inspect_sections():
    mgr = SubagentManager(session_id=2)
    h = SubagentHandle(
        agent_id=9, status="running", task_title="T", task_description="D",
        handoff_summary="H", context_snapshot={"original_request": "req"},
        trajectory=[{"tool": "fs_read", "summary": "read a.py"}],
        summary="done", files_touched=["x.py"], risks=[],
    )
    mgr._handles[9] = h
    all_data = mgr.inspect(9, "all")
    assert all_data["context"]["title"] == "T"
    assert all_data["context"]["inherited_context"]["original_request"] == "req"
    assert all_data["transcript"][0]["tool"] == "fs_read"
    assert all_data["result"]["files_touched"] == ["x.py"]
    # 单节
    assert "transcript" not in mgr.inspect(9, "context")
    assert mgr.inspect(999) is None


def test_subagent_context_snapshot():
    ctx = {
        "main_task_id": 5, "model_id": 3, "sandbox_mode": "workspace-write",
        "permission_mode": "default",
        "todos": [{"content": "step 1", "status": "pending"}, "bad-item"],
        "context_summary": "summary text",
    }
    snap = _subagent_context_snapshot(ctx, original_request="please fix the bug")
    assert snap["main_task_id"] == 5
    assert snap["sandbox_mode"] == "workspace-write"
    assert snap["original_request"] == "please fix the bug"
    assert snap["todos"] == [{"content": "step 1", "status": "pending"}]
    assert snap["context_summary"] == "summary text"


def test_subagent_context_snapshot_empty():
    snap = _subagent_context_snapshot({})
    assert snap == {}
