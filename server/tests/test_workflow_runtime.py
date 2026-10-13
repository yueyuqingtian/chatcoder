"""工作流运行时（§5.3–§5.7）：用脚本化执行器驱动真实数据库，覆盖调度、返工、闸门、重试、续跑与准入。"""

import asyncio
from typing import Any

import pytest
from sqlalchemy import select

from app.orchestration.workflow import store
from app.orchestration.workflow.conflicts import LeaseOwner, WriteLeaseTable
from app.orchestration.workflow.executor import NodeRequest, NodeResult
from app.orchestration.workflow.runtime import WorkflowRuntime
from app.orchestration.workflow.scheduler import PoolLimits
from app.persistence.database import async_session_factory, run_write_locked
from app.persistence.models.message import Session as SessionRow
from app.persistence.models.workflow import WorkflowEvent

PASS_TEXT = (
    '{"decision": "pass", "summary": "通过", '
    '"criteria": [{"id": "C1", "result": "met", "evidence": "pytest 通过"}], "issues": []}'
)
REWORK_TEXT = (
    '{"decision": "rework", "summary": "缺校验", '
    '"criteria": [{"id": "C1", "result": "unmet", "evidence": "缺 409"}], '
    '"issues": [{"severity": "major", "file": "server/app/points.py", "line": 3, '
    '"problem": "缺余额校验", "fix_hint": "加判断"}]}'
)


class ScriptedExecutor:
    """按节点键返回脚本化结果；未脚本化时：评估节点给 pass，其它节点给 done。"""

    def __init__(self, scripts: dict[str, list[Any]] | None = None) -> None:
        self.scripts = {k: list(v) for k, v in (scripts or {}).items()}
        self.calls: list[tuple[str, str, int, str]] = []
        self.requests: list[NodeRequest] = []
        self.in_flight = 0
        self.max_in_flight = 0

    async def run(self, req: NodeRequest) -> NodeResult:
        self.calls.append((req.node_key, req.mode, req.attempt_no, req.feedback))
        self.requests.append(req)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0)
            queue = self.scripts.get(req.node_key)
            if queue:
                item = queue.pop(0)
                if isinstance(item, BaseException):
                    raise item
                return item
            if req.kind == "review":
                return NodeResult(status="done", text=PASS_TEXT)
            return NodeResult(
                status="done", output={"summary": f"{req.node_key} 完成"}, text="done"
            )
        finally:
            self.in_flight -= 1


async def _noop_broadcast(session_id: int, event: dict[str, Any]) -> None:
    return None


async def _no_commands(command: str, cwd: str) -> dict[str, Any]:
    return {"cmd": command, "ok": True, "exit_code": 0, "output": ""}


async def _session() -> int:
    def _op(s):
        row = SessionRow(title="工作流测试")
        s.add(row)
        s.flush()
        sid = row.id
        s.commit()
        return sid

    return await run_write_locked(_op, label="test.session")


async def _model_reachable(session_id):
    return True, ""


def _model_down(reason: str = "模拟：连接被拒绝"):
    """返回一个始终不可达的预检（续跑 / 重试前的模型可达性检查）。"""

    async def probe(session_id):
        return False, reason

    return probe


def _runtime(executor, **kwargs) -> WorkflowRuntime:
    kwargs.setdefault("probe", _model_reachable)
    kwargs.setdefault("leases", WriteLeaseTable())
    kwargs.setdefault("command_runner", _no_commands)
    kwargs.setdefault("retry_plan", lambda: [0.0])
    kwargs.setdefault("broadcast", _noop_broadcast)
    return WorkflowRuntime(executor, **kwargs)


def _backend_plan(max_rework: int = 1, scope: str = "server/app/points.py") -> dict[str, Any]:
    return {
        "title": "积分兑换",
        "goal": "实现兑换流程",
        "flows": [
            {
                "key": "backend",
                "title": "后端",
                "nodes": [
                    {"key": "explore", "kind": "explore", "task": "探索服务与模型"},
                    {
                        "key": "impl",
                        "kind": "execute",
                        "inputs": ["explore"],
                        "write_scope": [scope],
                        "acceptance": ["余额不足返回 409"],
                        "task": "实现扣减",
                    },
                    {
                        "key": "review",
                        "kind": "review",
                        "inputs": ["impl"],
                        "rework_to": "impl",
                        "max_rework": max_rework,
                        "acceptance": ["测试通过"],
                        "commands": ["pytest -q tests/test_points.py"],
                        "task": "评审",
                    },
                ],
            }
        ],
    }


async def _program_status(program_id: int) -> str:
    snap = await store.load_program(program_id)
    return snap["run"]["status"]


async def _node_status(program_id: int, flow_key: str, node_key: str) -> str:
    snap = await store.load_program(program_id)
    for flow in snap["flows"]:
        if flow["run"]["package_key"] == flow_key:
            for node in flow["nodes"]:
                if node["key"] == node_key:
                    return node["status"]
    raise AssertionError(f"节点 {flow_key}/{node_key} 不存在")


async def _event_types(program_id: int) -> set[str]:
    async with async_session_factory() as s:
        rows = (
            (await s.execute(select(WorkflowEvent.type).where(WorkflowEvent.run_id == program_id)))
            .scalars()
            .all()
        )
        return set(rows)


async def test_happy_path_runs_in_order_and_succeeds(tmp_path):
    executor = ScriptedExecutor()
    rt = _runtime(executor)
    pid = await rt.create_program(
        session_id=await _session(), plan=_backend_plan(), workspace=str(tmp_path)
    )
    await rt.start(pid)
    await rt.run_until_idle(timeout=20)

    assert [c[0] for c in executor.calls] == ["explore", "impl", "review"]
    assert await _program_status(pid) == "succeeded"
    assert await _node_status(pid, "backend", "review") == "done"
    snap = await store.load_program(pid)
    review = snap["flows"][0]["nodes"][2]
    assert review["verdict"]["decision"] == "pass"
    assert review["checks"][0]["ok"] is True
    types = await _event_types(pid)
    assert {"workflow.node.updated", "workflow.run.updated", "workflow.log.appended"} <= types


async def test_rework_sends_issues_back_to_implementer_then_passes(tmp_path):
    executor = ScriptedExecutor({"review": [NodeResult(status="done", text=REWORK_TEXT)]})
    rt = _runtime(executor)
    pid = await rt.create_program(
        session_id=await _session(), plan=_backend_plan(max_rework=2), workspace=str(tmp_path)
    )
    await rt.start(pid)
    await rt.run_until_idle(timeout=20)

    modes = [(c[0], c[1]) for c in executor.calls]
    assert modes == [
        ("explore", "fresh"),
        ("impl", "fresh"),
        ("review", "fresh"),
        ("impl", "rework"),
        ("review", "fresh"),
    ]
    rework_feedback = executor.calls[3][3]
    assert "缺余额校验" in rework_feedback and "server/app/points.py:3" in rework_feedback
    assert await _program_status(pid) == "succeeded"
    snap = await store.load_program(pid)
    assert snap["run"]["rework_total"] == 1


async def test_exhausted_rework_opens_gate_and_approve_completes(tmp_path):
    executor = ScriptedExecutor(
        {
            "review": [
                NodeResult(status="done", text=REWORK_TEXT),
                NodeResult(status="done", text=REWORK_TEXT),
            ]
        }
    )
    rt = _runtime(executor)
    pid = await rt.create_program(
        session_id=await _session(), plan=_backend_plan(max_rework=1), workspace=str(tmp_path)
    )
    await rt.start(pid)
    await rt.run_until_idle(timeout=20)

    assert await _program_status(pid) == "waiting_human"
    assert await _node_status(pid, "backend", "review") == "waiting_human"
    assert "workflow.gate.opened" in await _event_types(pid)

    await rt.decide_gate(pid, "backend", "review", "approve")
    await rt.run_until_idle(timeout=20)
    assert await _program_status(pid) == "succeeded"


async def test_failing_check_overrides_pass_and_gates_after_rounds(tmp_path):
    async def failing(command: str, cwd: str) -> dict[str, Any]:
        return {
            "cmd": command,
            "ok": False,
            "exit_code": 1,
            "output": "FAILED tests/test_points.py",
        }

    executor = ScriptedExecutor()  # 评估者每次都给 pass，但检查失败必须改判
    rt = _runtime(executor, command_runner=failing)
    pid = await rt.create_program(
        session_id=await _session(), plan=_backend_plan(max_rework=1), workspace=str(tmp_path)
    )
    await rt.start(pid)
    await rt.run_until_idle(timeout=20)

    assert await _node_status(pid, "backend", "review") == "waiting_human"
    snap = await store.load_program(pid)
    assert snap["flows"][0]["nodes"][2]["checks"][0]["ok"] is False


async def test_transient_failure_retries_then_pauses_and_resume_keeps_progress(tmp_path):
    transient = NodeResult(status="failed", transient=True, error="网关超时")
    executor = ScriptedExecutor(
        {"impl": [transient, transient, NodeResult(status="done", output={"summary": "ok"})]}
    )
    rt = _runtime(executor, retry_plan=lambda: [0.0])
    pid = await rt.create_program(
        session_id=await _session(), plan=_backend_plan(), workspace=str(tmp_path)
    )
    await rt.start(pid)
    await rt.run_until_idle(timeout=20)

    snap = await store.load_program(pid)
    assert snap["run"]["status"] == "paused"
    assert snap["run"]["pause_kind"] == "network"
    assert await _node_status(pid, "backend", "impl") == "interrupted"

    await rt.resume(pid, note="")
    await rt.run_until_idle(timeout=20)

    assert await _program_status(pid) == "succeeded"
    explore_runs = [c for c in executor.calls if c[0] == "explore"]
    assert len(explore_runs) == 1, "已完成的节点不得重跑"
    impl_modes = [c[1] for c in executor.calls if c[0] == "impl"]
    assert impl_modes == ["fresh", "checkpoint", "checkpoint"]


async def test_deterministic_failure_marks_program_failed(tmp_path):
    executor = ScriptedExecutor({"impl": [NodeResult(status="failed", error="鉴权失败")]})
    rt = _runtime(executor)
    pid = await rt.create_program(
        session_id=await _session(), plan=_backend_plan(), workspace=str(tmp_path)
    )
    await rt.start(pid)
    await rt.run_until_idle(timeout=20)

    assert await _program_status(pid) == "failed"
    assert await _node_status(pid, "backend", "impl") == "failed"


async def test_invalid_review_output_gets_one_repair_attempt(tmp_path):
    executor = ScriptedExecutor({"review": [NodeResult(status="done", text="没有结构化结论")]})
    rt = _runtime(executor)
    pid = await rt.create_program(
        session_id=await _session(), plan=_backend_plan(), workspace=str(tmp_path)
    )
    await rt.start(pid)
    await rt.run_until_idle(timeout=20)

    review_modes = [c[1] for c in executor.calls if c[0] == "review"]
    assert review_modes == ["fresh", "repair"]
    assert await _program_status(pid) == "succeeded"


async def test_write_scope_lease_held_elsewhere_keeps_node_queued(tmp_path):
    leases = WriteLeaseTable()
    workspace = str(tmp_path)
    holder = LeaseOwner(run_id=999, node_key="other-session-node")
    leases.acquire(workspace, holder, ["server/app/points.py"])

    executor = ScriptedExecutor()
    rt = _runtime(executor, leases=leases)
    pid = await rt.create_program(
        session_id=await _session(), plan=_backend_plan(), workspace=workspace
    )
    await rt.start(pid)
    await rt.run_until_idle(timeout=20)

    assert await _node_status(pid, "backend", "impl") == "queued"
    snap = await store.load_program(pid)
    impl = snap["flows"][0]["nodes"][1]
    assert "other-session-node" in (impl["reason"] or "")
    assert [c[0] for c in executor.calls] == ["explore"]

    leases.release(workspace, holder)
    await rt.run_until_idle(timeout=20)
    assert await _program_status(pid) == "succeeded"


async def test_pool_global_limit_bounds_parallel_nodes(tmp_path):
    def _flow(key):
        return {
            "key": key,
            "title": key,
            "nodes": [{"key": "e", "kind": "explore", "task": "探索"}],
        }

    plan = {"title": "并行调研", "goal": "调研", "flows": [_flow("a"), _flow("b"), _flow("c")]}
    executor = ScriptedExecutor()
    rt = _runtime(
        executor, limits=PoolLimits(global_limit=1, per_session_limit=0, per_flow_limit=2)
    )
    pid = await rt.create_program(session_id=await _session(), plan=plan, workspace=str(tmp_path))
    await rt.start(pid)
    await rt.run_until_idle(timeout=20)

    assert executor.max_in_flight == 1
    assert len(executor.calls) == 3
    assert await _program_status(pid) == "succeeded"


async def test_cancel_stops_pending_nodes(tmp_path):
    executor = ScriptedExecutor()
    rt = _runtime(executor)
    pid = await rt.create_program(
        session_id=await _session(), plan=_backend_plan(), workspace=str(tmp_path)
    )
    await rt.start(pid)
    await rt.cancel(pid)
    await rt.run_until_idle(timeout=5)

    assert await _program_status(pid) == "cancelled"
    assert executor.calls == []
    assert await _node_status(pid, "backend", "explore") == "cancelled"


async def test_create_program_rejects_invalid_plan(tmp_path):
    plan = _backend_plan()
    plan["flows"][0]["nodes"][1].pop("write_scope")
    rt = _runtime(ScriptedExecutor())
    with pytest.raises(ValueError, match="写入范围"):
        await rt.create_program(session_id=await _session(), plan=plan, workspace=str(tmp_path))
