"""工作流协同整改（plan-workflow-collab）：返工前确认、次要问题不返工、复审沿用原评估子代理、
节点即时绑定子代理、过程报告、decide_workflow 工具、待确认上下文与工作流模式。"""

from typing import Any

import pytest

from app.core.config import settings
from app.orchestration.workflow import report, store
from app.orchestration.workflow.executor import NodeRequest, NodeResult
from app.orchestration.workflow.executor_subagent import _description
from app.orchestration.workflow.tools import run_workflow_tool
from tests.test_workflow_runtime import (
    REWORK_TEXT,
    ScriptedExecutor,
    _backend_plan,
    _node_status,
    _program_status,
    _runtime,
    _session,
)

MINOR_TEXT = (
    '{"decision": "rework", "summary": "基本可用", '
    '"criteria": [{"id": "C1", "result": "met", "evidence": "pytest 通过"}], '
    '"issues": [{"severity": "minor", "file": "server/app/points.py", "line": 8, '
    '"problem": "变量名可以更清楚", "fix_hint": "改名"}]}'
)


async def _review(program_id: int) -> dict[str, Any]:
    snap = await store.load_program(program_id)
    return next(n for f in snap["flows"] for n in f["nodes"] if n["key"] == "review")


async def test_rework_waits_for_confirmation_and_carries_user_feedback(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workflow_rework_confirm", True)
    notes: list[str] = []

    async def record(session_id: int, text: str) -> None:
        notes.append(text)

    executor = ScriptedExecutor({"review": [NodeResult(status="done", text=REWORK_TEXT)]})
    rt = _runtime(executor, notify=record)
    pid = await rt.create_program(
        session_id=await _session(), plan=_backend_plan(max_rework=2), workspace=str(tmp_path)
    )
    await rt.start(pid)
    await rt.run_until_idle(timeout=20)

    # 评估要求返工：先停下等确认，结论连同问题交给主代理
    assert await _program_status(pid) == "waiting_human"
    review = await _review(pid)
    assert review["conflict"]["kind"] == "review" and review["conflict"]["stage"] == "confirm"
    assert [c[0] for c in executor.calls] == ["explore", "impl", "review"]
    assert len(notes) == 1 and "缺余额校验" in notes[0] and "decide_workflow" in notes[0]

    await rt.decide_gate(pid, "backend", "review", "rework", "余额不足时返回 409，并补一个测试")
    await rt.run_until_idle(timeout=20)

    modes = [(c[0], c[1]) for c in executor.calls]
    assert modes[3:] == [("impl", "rework"), ("review", "rework")]
    impl_feedback = executor.calls[3][3]
    assert "缺余额校验" in impl_feedback and "补一个测试" in impl_feedback
    rereview = executor.calls[4][3]
    assert "复审" in rereview and "缺余额校验" in rereview
    assert await _program_status(pid) == "succeeded"

    snap = await store.load_program(pid)
    text = report.program_report(snap)
    assert "第 1 轮评估" in text and "已返工" in text
    assert "第 1 轮返工" in text and "补一个测试" in text
    assert "第 2 轮评估" in text
    assert "过程报告" in notes[-1] and "第 1 轮返工" in notes[-1]


async def test_minor_issues_do_not_trigger_rework_and_are_reported(tmp_path):
    notes: list[str] = []

    async def record(session_id: int, text: str) -> None:
        notes.append(text)

    executor = ScriptedExecutor({"review": [NodeResult(status="done", text=MINOR_TEXT)]})
    rt = _runtime(executor, notify=record)
    pid = await rt.create_program(
        session_id=await _session(), plan=_backend_plan(max_rework=2), workspace=str(tmp_path)
    )
    await rt.start(pid)
    await rt.run_until_idle(timeout=20)

    assert [c[0] for c in executor.calls] == ["explore", "impl", "review"]
    assert await _program_status(pid) == "succeeded"
    assert "遗留问题" in notes[-1] and "变量名可以更清楚" in notes[-1]


async def _agent_row(session_id: int) -> int:
    from app.persistence.database import run_write_locked
    from app.persistence.models.agent import Agent as AgentRow

    def _op(s):
        row = AgentRow(kind="sub", name="impl", session_id=session_id, status="running")
        s.add(row)
        s.flush()
        agent_id = row.id
        s.commit()
        return agent_id

    return await run_write_locked(_op, label="test.agent")


async def test_running_node_points_at_its_current_subagent(tmp_path):
    seen: dict[str, Any] = {}
    sid = await _session()
    agent_id = await _agent_row(sid)

    class BindingExecutor(ScriptedExecutor):
        async def run(self, req: NodeRequest) -> NodeResult:
            if req.node_key == "impl" and req.on_agent is not None:
                await req.on_agent(agent_id)
                snap = await store.load_program(req.run_id)
                node = next(n for f in snap["flows"] for n in f["nodes"] if n["key"] == "impl")
                seen["status"], seen["agent_id"] = node["status"], node["agent_id"]
            return await super().run(req)

    rt = _runtime(BindingExecutor())
    pid = await rt.create_program(session_id=sid, plan=_backend_plan(), workspace=str(tmp_path))
    await rt.start(pid)
    await rt.run_until_idle(timeout=20)

    assert seen == {"status": "running", "agent_id": agent_id}


def test_fallback_spawn_keeps_rework_feedback():
    req = NodeRequest(
        run_id=1,
        session_id=1,
        flow_key="backend",
        node_key="impl",
        node_id=1,
        attempt_id=1,
        attempt_no=2,
        kind="execute",
        role=None,
        task="实现扣减",
        acceptance=(),
        write_scope=("server/app/points.py",),
        mode="rework",
        feedback="- [重要] 缺余额校验\n用户补充说明（优先遵循）：返回 409",
    )
    text = _description(req)
    assert "这是返工" in text and "返回 409" in text


async def test_decide_workflow_tool_is_refused_in_readonly_and_applies_decision(
    fresh_runtime, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "workflow_rework_confirm", True)
    executor = ScriptedExecutor({"review": [NodeResult(status="done", text=REWORK_TEXT)]})
    fresh_runtime._executor = executor
    sid = await _session()
    pid = await fresh_runtime.create_program(
        session_id=sid, plan=_backend_plan(max_rework=2), workspace=str(tmp_path)
    )
    await fresh_runtime.start(pid)
    await fresh_runtime.run_until_idle(timeout=20)
    assert await _node_status(pid, "backend", "review") == "waiting_human"

    gates = await store.list_pending_gates(sid)
    assert gates and gates[0]["node_key"] == "review" and "rework" in gates[0]["options"]

    args = {"program_id": pid, "flow_key": "backend", "node_key": "review", "decision": "approve"}
    refused = await run_workflow_tool(
        "decide_workflow",
        args,
        session_id=sid,
        turn_id=None,
        workspace=str(tmp_path),
        readonly_only=True,
    )
    assert "只读/计划模式" in refused
    done = await run_workflow_tool(
        "decide_workflow",
        args,
        session_id=sid,
        turn_id=None,
        workspace=str(tmp_path),
        readonly_only=False,
    )
    assert "已采纳" in done
    await fresh_runtime.run_until_idle(timeout=20)
    assert await _program_status(pid) == "succeeded"
    review = await _review(pid)
    assert review["output"]["rounds"][-1]["outcome"] == "accepted"


def test_decisions_block_guides_the_main_agent():
    from app.orchestration.context_manager import _build_workflow_decisions_block

    block = _build_workflow_decisions_block(
        [
            {
                "program_id": 3,
                "program_title": "积分兑换",
                "flow_key": "backend",
                "flow_title": "后端",
                "node_key": "review",
                "node_title": "评审",
                "kind": "review",
                "stage": "confirm",
                "reason": "评估发现需要修改的问题",
                "options": ["approve", "rework", "abort"],
                "issues": [{"severity": "major", "problem": "缺余额校验"}],
            }
        ]
    )
    assert "decide_workflow" in block and "缺余额校验" in block and "program_id=3" in block


def test_workflow_mode_is_a_builtin_full_mode():
    from app.orchestration.approval_policy import normalize_execution_mode
    from app.orchestration.engine import _MODE_HINTS
    from app.services import permission_profile_service as pps

    assert normalize_execution_mode("workflow") == "workflow"
    profile = pps.get_profile("workflow")
    assert profile is not None and profile["kind"] == "full" and profile["builtin"]
    assert pps.resolve_tools("workflow", {"fs_read", "fs_write"}) is None
    assert not pps.is_readonly_like("workflow")
    assert "plan_workflow" in _MODE_HINTS["workflow"]


@pytest.fixture
def fresh_runtime(monkeypatch):
    from app.orchestration.workflow import service

    monkeypatch.setattr(service, "_runtime", None)
    return service.get_runtime()


@pytest.fixture(autouse=True)
def _restore_confirm():
    before = settings.workflow_rework_confirm
    yield
    settings.workflow_rework_confirm = before
