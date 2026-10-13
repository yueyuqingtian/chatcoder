"""进程被强制结束后的恢复（设计 §6.8）：已完成的节点保留，在途节点续跑到完成。"""

import httpx
import pytest
from fastapi import FastAPI

from app.gateway.routers import workflows as workflows_router
from app.orchestration.workflow import service, store
from app.orchestration.workflow.executor import NodeResult
from app.orchestration.workflow.runtime import WorkflowModelUnavailableError
from tests.test_workflow_runtime import (
    ScriptedExecutor,
    _backend_plan,
    _model_down,
    _node_status,
    _program_status,
    _runtime,
    _session,
)


async def _node_id(program_id: int, flow_key: str, node_key: str) -> int:
    snap = await store.load_program(program_id)
    flow = next(f for f in snap["flows"] if f["run"]["package_key"] == flow_key)
    return next(n["id"] for n in flow["nodes"] if n["key"] == node_key)


async def test_killed_mid_node_resumes_on_a_fresh_runtime_without_rerunning_done_nodes(tmp_path):
    # 第一个进程：探索节点已完成，实现节点执行中被强制结束。
    # 数据库停留在进程被杀时的状态（运行中的程序与节点），之后由新的运行时实例接管。
    first = _runtime(ScriptedExecutor())
    pid = await first.create_program(
        session_id=await _session(), plan=_backend_plan(), workspace=str(tmp_path)
    )
    explore_id = await _node_id(pid, "backend", "explore")
    impl_id = await _node_id(pid, "backend", "impl")
    await store.update_node(
        explore_id, status="done", finished_at=store.now_iso(), output_json={"plan": []}
    )
    await store.update_node(impl_id, status="running", started_at=store.now_iso())
    snap = await store.load_program(pid)
    await store.update_run(snap["flows"][0]["run"]["id"], status="running")
    await store.update_run(pid, status="running", started_at=store.now_iso())

    # 第二个进程：启动恢复扫描 → 续跑
    executor = ScriptedExecutor({"impl": [NodeResult(status="done", output={"summary": "ok"})]})
    second = _runtime(executor)
    assert await second.recover_after_restart() == 1

    snap = await store.load_program(pid)
    assert snap["run"]["status"] == "paused"
    assert snap["run"]["pause_kind"] == "restart"
    assert await _node_status(pid, "backend", "impl") == "interrupted"

    await second.resume(pid, note="")
    await second.run_until_idle(timeout=20)

    assert await _program_status(pid) == "succeeded"
    assert [c for c in executor.calls if c[0] == "explore"] == [], "已完成的探索节点不得重跑"
    assert [c[1] for c in executor.calls if c[0] == "impl"] == ["checkpoint"]


# ── 续跑 / 重试前的模型可达性预检（设计 §6.3 第 2 步）──────────


def _flippable_probe(state: dict):
    async def probe(session_id):
        return (True, "") if state["up"] else (False, "模拟：连接被拒绝")

    return probe


async def test_resume_is_refused_while_the_model_is_unreachable_and_keeps_the_pause(tmp_path):
    rt = _runtime(ScriptedExecutor(), probe=_model_down())
    pid = await rt.create_program(
        session_id=await _session(), plan=_backend_plan(), workspace=str(tmp_path)
    )
    await rt.start(pid)
    await rt.pause(pid)

    with pytest.raises(WorkflowModelUnavailableError, match="已保留进度"):
        await rt.resume(pid)

    snap = await store.load_program(pid)
    assert snap["run"]["status"] == "paused"
    assert snap["run"]["resume_count"] == 0, "预检失败不应计为一次续跑"


async def test_resume_goes_ahead_once_the_model_answers_again(tmp_path):
    state = {"up": False}
    rt = _runtime(ScriptedExecutor(), probe=_flippable_probe(state))
    pid = await rt.create_program(
        session_id=await _session(), plan=_backend_plan(), workspace=str(tmp_path)
    )
    await rt.start(pid)
    await rt.pause(pid)
    with pytest.raises(WorkflowModelUnavailableError):
        await rt.resume(pid)

    state["up"] = True
    await rt.resume(pid)
    await rt.run_until_idle(timeout=20)

    assert await _program_status(pid) == "succeeded"


async def test_automatic_resume_stays_paused_when_the_model_is_unreachable(tmp_path):
    rt = _runtime(ScriptedExecutor(), probe=_model_down())
    pid = await rt.create_program(
        session_id=await _session(), plan=_backend_plan(), workspace=str(tmp_path)
    )
    await rt.start(pid)
    await rt.pause(pid)

    await rt.resume(pid, note="网络恢复后自动续跑", automatic=True)

    assert await _program_status(pid) == "paused"


async def test_retry_is_refused_while_the_model_is_unreachable_and_the_node_stays_failed(tmp_path):
    state = {"up": True}
    executor = ScriptedExecutor({"impl": [NodeResult(status="failed", error="鉴权失败")]})
    rt = _runtime(executor, probe=_flippable_probe(state))
    pid = await rt.create_program(
        session_id=await _session(), plan=_backend_plan(), workspace=str(tmp_path)
    )
    await rt.start(pid)
    await rt.run_until_idle(timeout=20)
    assert await _program_status(pid) == "failed"

    state["up"] = False
    with pytest.raises(WorkflowModelUnavailableError):
        await rt.retry_node(pid, "backend", "impl", "fresh")

    assert await _node_status(pid, "backend", "impl") == "failed"
    assert await _program_status(pid) == "failed"


async def test_resume_route_answers_an_unreachable_model_with_a_structured_409(
    monkeypatch, tmp_path
):
    rt = _runtime(ScriptedExecutor(), probe=_model_down())
    pid = await rt.create_program(
        session_id=await _session(), plan=_backend_plan(), workspace=str(tmp_path)
    )
    await rt.start(pid)
    await rt.pause(pid)
    monkeypatch.setattr(service, "_runtime", rt)
    app = FastAPI()
    app.include_router(workflows_router.router, prefix="/api")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        res = await client.post(
            f"/api/workflows/{pid}/resume", json={"note": "", "accept_drift": False}
        )

    assert res.status_code == 409
    detail = res.json()["detail"]
    assert detail["code"] == "model_unavailable"
    assert "已保留进度" in detail["message"]
    assert await _program_status(pid) == "paused"
