"""评估结论需要用户决定（设计 §7.7）：没有返工目标的评估，闸门只提供“采纳”与“中止”；
决定返工会被拒绝，且不改动节点状态与返工计数。"""

import pytest

from app.orchestration.workflow import store
from app.orchestration.workflow.executor import NodeResult
from tests.test_workflow_runtime import (
    REWORK_TEXT,
    ScriptedExecutor,
    _backend_plan,
    _node_status,
    _program_status,
    _runtime,
    _session,
)


def _plan_without_rework_target() -> dict:
    plan = _backend_plan(max_rework=1)
    review = next(n for n in plan["flows"][0]["nodes"] if n["key"] == "review")
    review.pop("rework_to", None)
    return plan


async def _review_node(program_id: int) -> dict:
    snap = await store.load_program(program_id)
    return next(n for f in snap["flows"] for n in f["nodes"] if n["key"] == "review")


async def _last_gate_options(program_id: int) -> list[str]:
    events = await store.list_events(program_id)
    gates = [e for e in events if e["type"] == "workflow.gate.opened"]
    return gates[-1]["payload"]["options"]


async def _start_until_gate(tmp_path):
    executor = ScriptedExecutor({"review": [NodeResult(status="done", text=REWORK_TEXT)]})
    rt = _runtime(executor)
    pid = await rt.create_program(
        session_id=await _session(), plan=_plan_without_rework_target(), workspace=str(tmp_path)
    )
    await rt.start(pid)
    await rt.run_until_idle(timeout=20)
    return rt, pid


async def test_review_without_rework_target_gates_with_approve_and_abort_only(tmp_path):
    rt, pid = await _start_until_gate(tmp_path)

    assert await _program_status(pid) == "waiting_human"
    assert await _node_status(pid, "backend", "review") == "waiting_human"
    assert await _last_gate_options(pid) == ["approve", "abort"]

    # 结束程序，避免后台调度在用例之后继续读写数据库
    await rt.decide_gate(pid, "backend", "review", "abort")
    await rt.run_until_idle(timeout=20)
    assert await _program_status(pid) in {"cancelled", "failed", "succeeded"}


async def test_rework_decision_without_target_is_refused_and_keeps_the_gate(tmp_path):
    rt, pid = await _start_until_gate(tmp_path)
    before = await _review_node(pid)

    with pytest.raises(ValueError, match="没有可返工的实现步骤"):
        await rt.decide_gate(pid, "backend", "review", "rework", "再来一次")

    after = await _review_node(pid)
    assert after["status"] == "waiting_human"
    assert after["rework_round"] == before["rework_round"]
    assert after["max_rework"] == before["max_rework"]
    assert await _program_status(pid) == "waiting_human"

    await rt.decide_gate(pid, "backend", "review", "approve")
    await rt.run_until_idle(timeout=20)
    assert await _program_status(pid) == "succeeded"
