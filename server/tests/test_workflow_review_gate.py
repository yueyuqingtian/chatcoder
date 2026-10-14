"""没有返工目标的评估（集成验收、汇总等）：不通过时不开闸门，结论记入汇报后完成（plan-workflow-collab）。"""

import pytest

from app.orchestration.workflow import store
from app.orchestration.workflow.executor import NodeResult
from tests.test_workflow_runtime import (
    REWORK_TEXT,
    ScriptedExecutor,
    _backend_plan,
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


async def test_review_without_rework_target_records_issues_and_completes(tmp_path):
    notes: list[str] = []

    async def record(session_id: int, text: str) -> None:
        notes.append(text)

    executor = ScriptedExecutor({"review": [NodeResult(status="done", text=REWORK_TEXT)]})
    rt = _runtime(executor, notify=record)
    pid = await rt.create_program(
        session_id=await _session(), plan=_plan_without_rework_target(), workspace=str(tmp_path)
    )
    await rt.start(pid)
    await rt.run_until_idle(timeout=20)

    # plan-workflow-collab：没有返工目标的评估不卡在闸门上，结论记入汇报后完成
    assert await _program_status(pid) == "succeeded"
    review = await _review_node(pid)
    assert review["status"] == "done"
    assert "已记入汇报" in (review["reason"] or "")
    assert review["output"]["rounds"][0]["outcome"] == "recorded"
    assert len(notes) == 1
    assert "遗留问题" in notes[0] and "缺余额校验" in notes[0]


async def test_decision_on_a_step_that_is_not_waiting_is_refused(tmp_path):
    executor = ScriptedExecutor({"review": [NodeResult(status="done", text=REWORK_TEXT)]})
    rt = _runtime(executor)
    pid = await rt.create_program(
        session_id=await _session(), plan=_plan_without_rework_target(), workspace=str(tmp_path)
    )
    await rt.start(pid)
    await rt.run_until_idle(timeout=20)

    with pytest.raises(ValueError, match="不在等待决定状态"):
        await rt.decide_gate(pid, "backend", "review", "rework", "再来一次")
