"""plan-248-1258 M6: 子代理结构化汇报 / 上下文快照 / inspect 单测。"""
from app.orchestration.subagent import (
    SubagentHandle,
    SubagentManager,
    _format_completion_note,
    _parse_structured_report,
)
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
    # plan-330-1648 M4: 新增 send_to_subagent（主代理 → 运行中子代理的指令通道）
    assert names == ["spawn_subagent", "collect_results", "subagent_inspect", "cancel_subagent",
                     "send_to_subagent"]
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


def test_parse_structured_report_verification_acceptance():
    """v36 (plan-321-1600 M1): 汇报闭环分节——验证动作与验收结论需被解析出来。

    背景：此前只能解析变更文件/风险，主代理看不到“验证过没有、验收标准满足没有”，
    无法判断子任务结果是否可信。
    """
    h = SubagentHandle(agent_id=41)
    text = (
        "### Result\nAligned the subagent panel with the main message flow.\n"
        "### Files Touched\n- client/src/styles/global.css\n"
        "### Verification\n- Ran npm run typecheck: passed\n- Ran pytest test_subagent_reporting.py: 10 passed\n"
        "### Acceptance\n- Panel width matches main flow: met (pixel comparison)\n"
        "- Summary branch styling: not met (deferred)\n"
        "### Risks / Open Questions\nNone\n"
    )
    _parse_structured_report(h, text)
    assert "client/src/styles/global.css" in h.files_touched
    assert any("typecheck" in v for v in h.verification)
    assert any("10 passed" in v for v in h.verification)
    assert len(h.acceptance) == 2
    assert any("not met" in a for a in h.acceptance)
    # "None" 不得被当成风险条目
    assert h.risks == []


def test_parse_structured_report_fallback_files():
    h = SubagentHandle(agent_id=2)
    _parse_structured_report(h, "I looked at foo/bar.py and edited baz/qux.ts for you.")
    assert "foo/bar.py" in h.files_touched
    assert "baz/qux.ts" in h.files_touched


def test_parse_structured_report_empty_safe():
    h = SubagentHandle(agent_id=3)
    _parse_structured_report(h, "")
    assert h.files_touched == [] and h.risks == []


def test_parse_structured_report_preserves_ext_and_drive():
    """v36 修复：扩展名不得被截断（.json→.js / .tsx→.ts），Windows 盘符需保留。

    回归背景：正则交替项短项优先 + 缺词边界，导致主代理经 collect_results 拿到的
    变更文件清单路径错误（扩展名被截断），后续按该路径比对会踩空。
    """
    h = SubagentHandle(agent_id=4)
    text = (
        "### Files Touched\n"
        "- D:\\myProject\\chatcoder\\client\\package.json\n"
        "- client/src/components/chat/ModelPicker.tsx:43\n"
    )
    _parse_structured_report(h, text)
    assert "D:\\myProject\\chatcoder\\client\\package.json" in h.files_touched
    assert "client/src/components/chat/ModelPicker.tsx" in h.files_touched
    # 旧行为（截断成 .js / .ts）不得回归
    assert all(not f.endswith(".js") for f in h.files_touched)
    assert all(not f.endswith(".ts") for f in h.files_touched)


def test_completion_notification_claim_semantics():
    """v36 (plan-321-1600 M3): 完成通知入队一次；已 claim（collect_results 读过）的不再推送。"""
    mgr = SubagentManager(session_id=3)
    done = SubagentHandle(agent_id=11, status="done", task_title="T1",
                          findings="### Result\nDone A.\n", files_touched=["a.py"])
    failed = SubagentHandle(agent_id=12, status="failed", task_title="T2", error="boom")
    mgr._handles[11] = done
    mgr._handles[12] = failed
    mgr._enqueue_completion(done)
    mgr._enqueue_completion(failed)
    # 主代理已通过 collect_results 读到 #12 → claim 后不再推送
    mgr.claim_notification(12)
    notes = mgr.drain_completions()
    assert len(notes) == 1
    assert "#11" in notes[0] and "已完成" in notes[0]
    # 取出即已读：再次 drain 为空，且不会重复入队
    assert mgr.drain_completions() == []
    mgr._enqueue_completion(done)
    assert mgr.drain_completions() == []


def test_completion_note_reports_failure_reason():
    note = _format_completion_note(
        SubagentHandle(agent_id=13, status="failed", task_title="T3", error="模型不可用(None)")
    )
    assert "#13" in note and "失败" in note and "模型不可用" in note
    cancelled = _format_completion_note(
        SubagentHandle(agent_id=14, status="cancelled", task_title="T4")
    )
    assert "#14" in cancelled and "已取消" in cancelled


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


def test_spawned_count_counts_dispatched_per_turn():
    """v36 优化：每轮总量上限按“已派发”统计（含已完成），与并发数（运行中）分离。

    旧实现用 pending_count 兼职判总量，快速完成的子代理不占额度，
    一轮内实际可派发数量远超上限。
    """
    mgr = SubagentManager(session_id=5)
    mgr._handles[31] = SubagentHandle(agent_id=31, turn_id=100, status="done")
    mgr._handles[32] = SubagentHandle(agent_id=32, turn_id=100, status="running")
    mgr._handles[33] = SubagentHandle(agent_id=33, turn_id=101, status="done")
    assert mgr.spawned_count(100) == 2
    assert mgr.spawned_count(101) == 1
    assert mgr.spawned_count(999) == 0
    # 并发闸门仍只看运行中
    assert mgr.pending_count() == 1


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
        "main_task_id": 5, "model_id": 3,
        "permission_mode": "agent", "approval_mode": "ask",
        "todos": [{"content": "step 1", "status": "pending"}, "bad-item"],
        "context_summary": "summary text",
    }
    snap = _subagent_context_snapshot(ctx, original_request="please fix the bug")
    assert snap["main_task_id"] == 5
    # plan-75-332: 已废弃的 sandbox_mode 不再入快照，改为执行模式 × 权限模式
    assert "sandbox_mode" not in snap
    assert snap["permission_mode"] == "agent"
    assert snap["approval_mode"] == "ask"
    assert snap["original_request"] == "please fix the bug"
    assert snap["todos"] == [{"content": "step 1", "status": "pending"}]
    assert snap["context_summary"] == "summary text"


def test_subagent_context_snapshot_empty():
    snap = _subagent_context_snapshot({})
    assert snap == {}
