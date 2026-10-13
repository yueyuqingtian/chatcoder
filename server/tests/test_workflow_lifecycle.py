"""工作流生命周期：程序级集成验收（after）、token 预算、取消的结算防覆盖、停止当前轮暂停、
关键状态通知主代理、停滞看门狗与并发池快照（设计 §4.5、§5.4、§5.5、§5.8、§6.1）。"""

import asyncio
import contextlib

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import settings as app_settings
from app.gateway.routers import workflows as workflows_router
from app.orchestration import engine
from app.orchestration.workflow import executor_subagent as executor_module
from app.orchestration.workflow import runtime as runtime_module
from app.orchestration.workflow import service, store
from app.orchestration.workflow.executor import NodeRequest, NodeResult
from app.orchestration.workflow.executor_subagent import SubagentNodeExecutor
from app.orchestration.workflow.graph import parse_plan, validate_plan
from tests.test_workflow_runtime import (
    REWORK_TEXT,
    ScriptedExecutor,
    _backend_plan,
    _node_status,
    _program_status,
    _runtime,
    _session,
)


def _plan_with_after(after: dict | None) -> dict:
    plan = _backend_plan()
    if after is not None:
        plan["after"] = after
    return plan


# ── 程序级集成验收（after）──────────────────────────────────


def test_after_expands_into_a_final_integration_review_flow():
    spec, diags = parse_plan(
        _plan_with_after({"acceptance": ["兑换流程端到端可用"], "commands": ["pytest -q"]})
    )
    assert spec is not None, diags

    res = validate_plan(spec)

    assert res.ok, res.errors
    flows = res.normalized.flows
    assert [f.key for f in flows] == ["backend", "integration"]
    integration = flows[-1]
    assert integration.depends_on == ["backend"]
    assert [n.kind for n in integration.nodes] == ["review"]
    assert integration.nodes[0].acceptance == ["兑换流程端到端可用"]
    assert integration.nodes[0].commands == ["pytest -q"]
    assert res.normalized.after is None, "展开后的规格不再保留 after，避免重复展开"


def test_after_with_only_commands_gets_a_default_acceptance():
    spec, _ = parse_plan(_plan_with_after({"commands": ["pytest -q"]}))

    res = validate_plan(spec)

    assert res.ok, res.errors
    assert res.normalized.flows[-1].nodes[0].acceptance == ["集成检查命令全部通过"]


def test_empty_after_is_not_expanded():
    spec, _ = parse_plan(_plan_with_after({}))

    res = validate_plan(spec)

    assert res.ok
    assert [f.key for f in res.normalized.flows] == ["backend"]


def test_integration_key_avoids_collisions_with_model_flows():
    plan = _plan_with_after({"acceptance": ["端到端通过"]})
    plan["flows"].append(
        {
            "key": "integration",
            "title": "前端",
            "nodes": [{"key": "explore", "kind": "explore", "task": "x"}],
        }
    )
    spec, _ = parse_plan(plan)

    res = validate_plan(spec)

    assert res.ok, res.errors
    assert [f.key for f in res.normalized.flows][-1] == "integration2"


def test_after_commands_outside_allowlist_still_warn_on_the_original_plan():
    spec, _ = parse_plan(_plan_with_after({"commands": ["rm -rf build"]}))

    res = validate_plan(spec)

    assert any(
        w.code == "command_needs_approval" and w.path == "after.commands" for w in res.warnings
    )


# ── token 预算 ──────────────────────────────────────────────


async def test_token_budget_pauses_before_new_nodes_and_resumes_when_raised(tmp_path, monkeypatch):
    monkeypatch.setattr(app_settings, "workflow_token_budget_per_run", 500)
    executor = ScriptedExecutor(
        {
            "explore": [
                NodeResult(status="done", output={"summary": "探索完成"}, text="done", tokens=600)
            ]
        }
    )
    rt = _runtime(executor)
    pid = await rt.create_program(
        session_id=await _session(), plan=_backend_plan(), workspace=str(tmp_path)
    )
    await rt.start(pid)
    await rt.run_until_idle(timeout=20)

    snap = await store.load_program(pid)
    assert snap["run"]["status"] == "paused"
    assert snap["run"]["pause_kind"] == "budget"
    assert snap["run"]["token_used"] == 600
    assert await _node_status(pid, "backend", "impl") == "pending", "预算用完后不得再启动新节点"

    with pytest.raises(ValueError, match="预算仍已用完"):
        await rt.resume(pid)

    monkeypatch.setattr(app_settings, "workflow_token_budget_per_run", 10_000)
    await rt.resume(pid)
    await rt.run_until_idle(timeout=20)
    assert await _program_status(pid) == "succeeded"


# ── 取消：在途结算不得改写节点状态 ─────────────────────────


class _BlockingExecutor:
    """节点一直运行到被取消为止，用于模拟“取消时节点仍在执行”。"""

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def run(self, req: NodeRequest) -> NodeResult:
        self.started.set()
        await asyncio.Event().wait()
        return NodeResult(status="done", text="done")  # 取消后不会执行到这里


async def test_cancel_while_a_node_runs_keeps_node_states_cancelled(tmp_path):
    executor = _BlockingExecutor()
    rt = _runtime(executor)
    pid = await rt.create_program(
        session_id=await _session(), plan=_backend_plan(), workspace=str(tmp_path)
    )
    await rt.start(pid)
    await rt.tick()
    await asyncio.wait_for(executor.started.wait(), timeout=5)

    await rt.cancel(pid)
    await rt.run_until_idle(timeout=10)

    assert await _program_status(pid) == "cancelled"
    assert await _node_status(pid, "backend", "explore") == "cancelled"
    assert await _node_status(pid, "backend", "impl") == "cancelled"


# ── 停止当前轮：只暂停该会话的运行中程序 ───────────────────


async def test_pause_for_turn_stop_only_touches_running_programs_of_that_session(tmp_path):
    rt = _runtime(ScriptedExecutor())
    sid = await _session()
    other = await _session()
    mine = await rt.create_program(session_id=sid, plan=_backend_plan(), workspace=str(tmp_path))
    theirs = await rt.create_program(
        session_id=other, plan=_backend_plan(), workspace=str(tmp_path)
    )
    await rt.start(mine)
    await rt.start(theirs)

    assert await rt.pause_session_turn(sid) == 1

    assert await _program_status(mine) == "paused"
    assert (await store.load_program(mine))["run"]["pause_kind"] == "turn_stop"
    assert await _program_status(theirs) == "running"


# ── 关键状态通知主代理（每次转换一次）──────────────────────


async def test_completion_notifies_the_main_agent_once(tmp_path):
    notes: list[tuple[int, str]] = []

    async def record(session_id: int, text: str) -> None:
        notes.append((session_id, text))

    rt = _runtime(ScriptedExecutor(), notify=record)
    sid = await _session()
    pid = await rt.create_program(session_id=sid, plan=_backend_plan(), workspace=str(tmp_path))
    await rt.start(pid)
    await rt.run_until_idle(timeout=20)

    assert await _program_status(pid) == "succeeded"
    assert len(notes) == 1
    assert notes[0][0] == sid
    assert "已全部完成" in notes[0][1]


async def test_failure_notifies_with_the_failing_node_reason(tmp_path):
    notes: list[str] = []

    async def record(session_id: int, text: str) -> None:
        notes.append(text)

    executor = ScriptedExecutor({"impl": [NodeResult(status="failed", error="鉴权失败")]})
    rt = _runtime(executor, notify=record)
    pid = await rt.create_program(
        session_id=await _session(), plan=_backend_plan(), workspace=str(tmp_path)
    )
    await rt.start(pid)
    await rt.run_until_idle(timeout=20)

    assert await _program_status(pid) == "failed"
    assert len(notes) == 1
    assert "执行失败" in notes[0] and "鉴权失败" in notes[0]


async def test_gate_notifies_and_tells_the_agent_not_to_decide_for_the_user(tmp_path):
    notes: list[str] = []

    async def record(session_id: int, text: str) -> None:
        notes.append(text)

    executor = ScriptedExecutor({"review": [NodeResult(status="done", text=REWORK_TEXT)]})
    rt = _runtime(executor, notify=record)
    pid = await rt.create_program(
        session_id=await _session(), plan=_backend_plan(max_rework=0), workspace=str(tmp_path)
    )
    await rt.start(pid)
    await rt.run_until_idle(timeout=20)

    assert await _program_status(pid) == "waiting_human"
    assert len(notes) == 1
    assert "需要用户决定" in notes[0]
    assert "不要自行替用户决定" in notes[0]


async def test_user_stop_schedules_pause_but_rollback_does_not(monkeypatch):
    calls: list[int] = []

    async def fake_stopped(session_id: int) -> int:
        calls.append(session_id)
        return 1

    monkeypatch.setattr(service, "on_turn_stopped", fake_stopped)
    engine._running_turn_session[424242] = 77
    engine._running_turn_session[424243] = 78
    try:
        await engine.cancel_turn(424242, source="ws")
        await engine.cancel_turn(424243, source="rollback")
        await asyncio.sleep(0.05)
    finally:
        engine._running_turn_session.pop(424242, None)
        engine._running_turn_session.pop(424243, None)

    assert calls == [77]


async def test_workflow_notices_are_queued_and_merged_into_one_wakeup(monkeypatch):
    monkeypatch.setattr(engine, "WORKFLOW_WAKEUP_DELAY_SEC", 60.0)  # 本用例不真正唤醒
    sid = 987654
    engine._workflow_notes.pop(sid, None)
    await engine.notify_workflow(sid, "第一条")
    await engine.notify_workflow(sid, "第二条")

    assert engine._workflow_notes[sid] == ["第一条", "第二条"]
    task = engine._workflow_wakeups[sid]
    assert not task.done()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    engine._workflow_notes.pop(sid, None)


# ── 停滞看门狗 ───────────────────────────────────────────


class _FakeHandle:
    def __init__(self, *, task: asyncio.Task, tokens_used: int = 0) -> None:
        self.task = task
        self.trajectory: list[dict] = []
        self.status = "running"
        self.waiting = False
        self.tokens_used = tokens_used
        self.pause_reason = ""
        self.error = ""
        self.summary = ""
        self.findings = ""
        self.files_touched: list[str] = []
        self.risks: list[str] = []
        self.verification: list[str] = []
        self.acceptance: list[str] = []


class _FakeManager:
    def __init__(self, handle: _FakeHandle) -> None:
        self._handle = handle

    def get(self, agent_id: int):
        return self._handle if agent_id == 7 else None


def _request() -> NodeRequest:
    return NodeRequest(
        run_id=1,
        session_id=1,
        flow_key="backend",
        node_key="impl",
        node_id=3,
        attempt_id=1,
        attempt_no=1,
        kind="execute",
        role=None,
        task="实现",
        acceptance=(),
        write_scope=("a.py",),
        mode="checkpoint",
        feedback="",
    )


@pytest.fixture
def fast_watchdog(monkeypatch):
    monkeypatch.setattr(executor_module, "POLL_SEC", 0.01)
    monkeypatch.setattr(executor_module, "STALL_TIMEOUT_SEC", 0.05)
    monkeypatch.setattr(executor_module, "HEARTBEAT_TOUCH_SEC", 1000.0)


async def test_stalled_subagent_is_stopped_and_reported_as_transient(fast_watchdog):
    task = asyncio.get_running_loop().create_task(asyncio.sleep(3600))
    handle = _FakeHandle(task=task, tokens_used=120)

    result = await SubagentNodeExecutor()._wait(_FakeManager(handle), 7, _request(), 20)

    assert result.status == "failed"
    assert result.transient is True
    assert "停滞" in result.error
    assert result.tokens == 100, "本次尝试的用量 = 累计用量 - 尝试开始时的累计值"
    assert handle.task.cancelled()


async def test_progressing_subagent_is_not_treated_as_stalled(fast_watchdog):
    release = asyncio.Event()

    async def finish_when_released() -> None:
        await release.wait()

    handle = _FakeHandle(task=asyncio.get_running_loop().create_task(finish_when_released()))

    async def progress() -> None:
        for _ in range(20):
            handle.trajectory.append({"tool": "fs_read"})
            await asyncio.sleep(0.01)
        # 先写入终态，再放行子代理任务结束：保证结算时读到的是 done
        handle.status = "done"
        handle.summary = "完成"
        release.set()

    ticker = asyncio.get_running_loop().create_task(progress())
    result = await SubagentNodeExecutor()._wait(_FakeManager(handle), 7, _request(), 0)
    await ticker

    assert result.status == "done"
    assert result.transient is False


# ── 设置：开始前确认 / 网络中断自动续跑 ─────────────────────


async def test_plan_starts_immediately_when_confirmation_is_disabled(monkeypatch):
    from app.orchestration.workflow.tools import run_workflow_tool

    monkeypatch.setattr(service, "_runtime", None)
    monkeypatch.setattr(app_settings, "workflow_confirm_before_start", False)
    sid = await _session()
    plan = _backend_plan()
    text = await run_workflow_tool(
        "plan_workflow",
        {"title": plan["title"], "goal": plan["goal"], "flows": plan["flows"]},
        session_id=sid,
        turn_id=None,
        workspace="",
        readonly_only=False,
    )

    assert "已按设置直接开始执行" in text
    listed = await store.list_programs(sid)
    assert listed and listed[0]["status"] == "running"


async def _wait_for_status(pid: int, expected: str, timeout: float = 3.0) -> str:
    """自动续跑由定时器与写库队列完成，负载高时可能晚于固定的短暂等待；轮询到期望状态或超时。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    status = await _program_status(pid)
    while status != expected and loop.time() < deadline:
        await asyncio.sleep(0.02)
        status = await _program_status(pid)
    return status


async def test_network_pause_auto_resumes_once_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setattr(app_settings, "workflow_auto_resume_on_network", True)
    monkeypatch.setattr(runtime_module, "AUTO_RESUME_DELAY_SEC", 0.01)
    rt = _runtime(ScriptedExecutor())
    pid = await rt.create_program(
        session_id=await _session(), plan=_backend_plan(), workspace=str(tmp_path)
    )
    await rt.start(pid)

    await rt._pause_program(pid, "network", "网络中断，已保留进度")
    assert await _wait_for_status(pid, "running") == "running", "网络恢复后应自动续跑"

    # 自动续跑只尝试一次：再次因网络暂停时不再自动恢复
    await rt._pause_program(pid, "network", "网络中断，已保留进度")
    await asyncio.sleep(0.1)
    assert await _program_status(pid) == "paused"

    # 手动续跑会重新开放一次自动续跑
    await rt.resume(pid)
    assert await _program_status(pid) == "running"
    await rt._pause_program(pid, "network", "网络中断，已保留进度")
    assert await _wait_for_status(pid, "running") == "running"


# ── 并发池快照 API ───────────────────────────────────────


def test_pool_endpoint_reports_capacity(monkeypatch):
    monkeypatch.setattr(service, "_runtime", None)
    app = FastAPI()
    app.include_router(workflows_router.router, prefix="/api")

    body = TestClient(app).get("/api/workflows/pool").json()

    assert body["global"]["limit"] == 3
    assert body["global"]["in_use"] == 0


# ── 失败节点的重试（设计 §6.4）──────────────────────────────


async def test_failed_node_can_be_redone_from_scratch_and_the_program_finishes(tmp_path):
    executor = ScriptedExecutor({"impl": [NodeResult(status="failed", error="鉴权失败")]})
    rt = _runtime(executor)
    pid = await rt.create_program(
        session_id=await _session(), plan=_backend_plan(), workspace=str(tmp_path)
    )
    await rt.start(pid)
    await rt.run_until_idle(timeout=20)
    assert await _program_status(pid) == "failed"
    snap = await store.load_program(pid)
    impl = next(n for f in snap["flows"] for n in f["nodes"] if n["key"] == "impl")
    assert impl["reason"] == "鉴权失败", "失败原因要保存在节点上，界面才能直接显示"

    await rt.retry_node(pid, "backend", "impl", "fresh")
    await rt.run_until_idle(timeout=20)

    assert await _program_status(pid) == "succeeded"
    impl_modes = [mode for key, mode, _attempt, _feedback in executor.calls if key == "impl"]
    assert impl_modes == ["fresh", "fresh"]
    explore_runs = [key for key, *_ in executor.calls if key == "explore"]
    assert len(explore_runs) == 1, "已完成的步骤不应因为重试而重跑"


async def test_checkpoint_retry_hands_the_checkpoint_mode_to_the_executor(tmp_path):
    executor = ScriptedExecutor({"impl": [NodeResult(status="failed", error="模型输出为空")]})
    rt = _runtime(executor)
    pid = await rt.create_program(
        session_id=await _session(), plan=_backend_plan(), workspace=str(tmp_path)
    )
    await rt.start(pid)
    await rt.run_until_idle(timeout=20)

    await rt.retry_node(pid, "backend", "impl", "checkpoint")
    await rt.run_until_idle(timeout=20)

    assert await _program_status(pid) == "succeeded"
    impl_modes = [mode for key, mode, _attempt, _feedback in executor.calls if key == "impl"]
    assert impl_modes == ["fresh", "checkpoint"]


async def test_retry_is_refused_unless_a_failed_node_of_a_failed_program(tmp_path):
    executor = ScriptedExecutor({"impl": [NodeResult(status="failed", error="鉴权失败")]})
    rt = _runtime(executor)
    pid = await rt.create_program(
        session_id=await _session(), plan=_backend_plan(), workspace=str(tmp_path)
    )
    await rt.start(pid)
    await rt.run_until_idle(timeout=20)

    with pytest.raises(ValueError, match="只能重试已失败的节点"):
        await rt.retry_node(pid, "backend", "explore", "fresh")
    with pytest.raises(ValueError, match="重试方式"):
        await rt.retry_node(pid, "backend", "impl", "rework")

    healthy = await rt.create_program(
        session_id=await _session(), plan=_backend_plan(), workspace=str(tmp_path)
    )
    await rt.start(healthy)
    await rt.run_until_idle(timeout=20)
    with pytest.raises(ValueError, match="只有失败的工作流"):
        await rt.retry_node(healthy, "backend", "impl", "fresh")


async def test_retry_route_answers_a_refused_retry_with_409(monkeypatch, tmp_path):
    rt = _runtime(ScriptedExecutor())
    pid = await rt.create_program(
        session_id=await _session(), plan=_backend_plan(), workspace=str(tmp_path)
    )
    monkeypatch.setattr(service, "_runtime", rt)
    app = FastAPI()
    app.include_router(workflows_router.router, prefix="/api")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        refused = await client.post(
            f"/api/workflows/{pid}/nodes/backend/impl/retry", json={"mode": "fresh"}
        )
        invalid = await client.post(
            f"/api/workflows/{pid}/nodes/backend/impl/retry", json={"mode": "rework"}
        )

    assert refused.status_code == 409
    assert "只有失败的工作流" in refused.json()["detail"]
    assert invalid.status_code == 422
