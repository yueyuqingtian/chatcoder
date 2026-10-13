"""L3 合并步骤（设计 §5A.2）：同文件真冲突经重做仍失败时追加合并节点，失败则交给用户。"""

import pytest

from app.core.config import settings
from app.orchestration.workflow import guard, store
from app.orchestration.workflow.executor import NodeResult
from app.orchestration.workflow.runtime import MAX_MERGE_STEPS
from tests.test_workflow_runtime import (
    ScriptedExecutor,
    _backend_plan,
    _node_status,
    _program_status,
    _runtime,
    _session,
)

CONFLICT = {
    "kind": "conflict",
    "path": "server/app/points.py",
    "attempts": 3,
    "base": "def deduct(user, n):\n    return user.balance - n\n",
    "current": "def deduct(user, n):\n    if user.balance < n:\n        raise ValueError\n",
    "pending": "def deduct(user, n):\n    if user.balance < n:\n        raise InsufficientPoints\n",
    "reason": "多次被其它节点改动，无法自动合并",
}


class ConflictingExecutor(ScriptedExecutor):
    """实现节点首次执行时触发一次写入冲突闸门；合并步骤按参数给出结果或再次冲突。"""

    def __init__(
        self,
        *,
        merge_result: NodeResult | None = None,
        merge_conflicts: bool = False,
        always_conflict: bool = False,
    ):
        super().__init__()
        self.merge_result = merge_result
        self.merge_conflicts = merge_conflicts
        self.always_conflict = always_conflict
        self.gated = False

    async def run(self, req):
        if req.node_key == "impl" and (
            self.always_conflict or (req.mode == "fresh" and not self.gated)
        ):
            self.gated = True
            self.calls.append((req.node_key, req.mode, req.attempt_no, req.feedback))
            guard._GATES[req.node_id] = dict(CONFLICT)
            return NodeResult(status="done", output={"summary": "first try"})
        if req.node_key.startswith("merge-") and (self.merge_conflicts or self.merge_result):
            self.calls.append((req.node_key, req.mode, req.attempt_no, req.feedback))
            if self.merge_conflicts:
                guard._GATES[req.node_id] = dict(CONFLICT)
                return NodeResult(status="done", output={"summary": "merged?"})
            return self.merge_result
        return await super().run(req)


@pytest.fixture(autouse=True)
def _reset_guard_state():
    guard._GATES.clear()
    yield
    guard._GATES.clear()


async def _program(rt, tmp_path) -> int:
    return await rt.create_program(
        session_id=await _session(), plan=_backend_plan(), workspace=str(tmp_path)
    )


async def _impl_conflict(pid: int) -> dict:
    snap = await store.load_program(pid)
    flow = snap["flows"][0]
    return next(n for n in flow["nodes"] if n["key"] == "impl")


async def test_repeated_conflict_becomes_a_merge_step_and_the_node_continues(tmp_path):
    executor = ConflictingExecutor()
    rt = _runtime(executor)
    pid = await _program(rt, tmp_path)
    await rt.start(pid)
    await rt.run_until_idle(timeout=20)

    assert await _program_status(pid) == "succeeded"
    merge_calls = [c for c in executor.calls if c[0].startswith("merge-impl-")]
    assert len(merge_calls) == 1, "同一冲突只追加一个合并步骤"
    impl_calls = [c for c in executor.calls if c[0] == "impl"]
    assert [c[1] for c in impl_calls] == ["fresh", "rework"]
    assert "已由合并步骤合并" in impl_calls[1][3]
    assert "server/app/points.py" in impl_calls[1][3]


async def test_failed_merge_hands_the_original_conflict_to_the_user(tmp_path):
    executor = ConflictingExecutor(merge_result=NodeResult(status="failed", error="模型输出为空"))
    rt = _runtime(executor)
    pid = await _program(rt, tmp_path)
    await rt.start(pid)
    await rt.run_until_idle(timeout=20)

    assert await _node_status(pid, "backend", "impl") == "waiting_human"
    impl = await _impl_conflict(pid)
    assert impl["conflict"]["path"] == "server/app/points.py"
    assert impl["conflict"]["base"] and impl["conflict"]["pending"]

    await rt.decide_gate(pid, "backend", "impl", "approve")
    await rt.run_until_idle(timeout=20)
    assert await _program_status(pid) == "succeeded"


async def test_a_conflict_inside_the_merge_step_does_not_merge_again(tmp_path):
    executor = ConflictingExecutor(merge_conflicts=True)
    rt = _runtime(executor)
    pid = await _program(rt, tmp_path)
    await rt.start(pid)
    await rt.run_until_idle(timeout=20)

    assert [c[0] for c in executor.calls if c[0].startswith("merge-")] == ["merge-impl-1"]
    assert await _node_status(pid, "backend", "impl") == "waiting_human"


async def test_repeated_conflicts_stop_after_the_merge_budget_and_ask_the_user(tmp_path):
    executor = ConflictingExecutor(always_conflict=True)  # 每次重做都再次冲突
    rt = _runtime(executor)
    pid = await _program(rt, tmp_path)
    await rt.start(pid)
    await rt.run_until_idle(timeout=20)

    merges = [c[0] for c in executor.calls if c[0].startswith("merge-impl-")]
    assert len(merges) == MAX_MERGE_STEPS
    assert await _node_status(pid, "backend", "impl") == "waiting_human"


async def test_ask_policy_puts_the_conflict_straight_to_the_user(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workflow_conflict_policy", "ask")
    executor = ConflictingExecutor()
    rt = _runtime(executor)
    pid = await _program(rt, tmp_path)
    await rt.start(pid)
    await rt.run_until_idle(timeout=20)

    assert not [c for c in executor.calls if c[0].startswith("merge-")]
    assert await _node_status(pid, "backend", "impl") == "waiting_human"


async def test_disabled_auto_merge_skips_the_merge_step(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workflow_auto_merge", False)
    executor = ConflictingExecutor()
    rt = _runtime(executor)
    pid = await _program(rt, tmp_path)
    await rt.start(pid)
    await rt.run_until_idle(timeout=20)

    assert not [c for c in executor.calls if c[0].startswith("merge-")]
    assert await _node_status(pid, "backend", "impl") == "waiting_human"
