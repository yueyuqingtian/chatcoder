"""plan-20261013-wf-order：工作流卡与节点子代理卡按创建序固化（服务端部分）。

覆盖点：
- agent.started 对工作流节点子代理附带所属程序编号与派发时刻（线程首条消息 id）；
  普通子代理不附带；解析失败不阻塞启动。
- start / resume 由工作流工具调用时，把节点归属轮次更新为当前轮（节点卡因此落在启动所在轮）。
"""
import sys
from pathlib import Path
from types import SimpleNamespace

from app.orchestration import agent_loop as agent_loop_mod
from app.orchestration.workflow import store as workflow_store
from app.orchestration.workflow.executor import NodeResult
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.gateway.routers import turns as turns_router
from app.persistence.database import run_write_locked
from app.persistence.models.agent import Agent as AgentRow
from app.persistence.models.message import Message as MessageRow

# 复用工作流运行时测试的脚本化执行器与夹具（同一套真实数据库驱动）；
# 测试目录未必在 sys.path 中（importlib 导入模式），故显式加入。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_workflow_runtime import (  # noqa: E402
    ScriptedExecutor,
    _backend_plan,
    _program_status,
    _runtime,
    _session,
)


async def test_agent_started_extra_carries_program_of_node_agent(monkeypatch):
    seen: list[list[int]] = []

    async def fake_briefs(node_ids):
        seen.append(list(node_ids))
        return {5: {"node_id": 5, "program_id": 9}}

    monkeypatch.setattr(workflow_store, "node_briefs", fake_briefs)
    extra = await agent_loop_mod._agent_started_extra(SimpleNamespace(workflow_node_id=5))
    assert extra == {"workflow_program_id": 9}
    assert seen == [[5]]


async def test_agent_started_extra_is_empty_for_plain_subagent(monkeypatch):
    async def must_not_be_called(node_ids):
        raise AssertionError("普通子代理不应查询工作流节点")

    monkeypatch.setattr(workflow_store, "node_briefs", must_not_be_called)
    assert await agent_loop_mod._agent_started_extra(SimpleNamespace(workflow_node_id=None)) == {}


async def test_agent_started_extra_without_program_adds_nothing(monkeypatch):
    async def orphan_node(node_ids):
        return {5: {"node_id": 5, "program_id": None}}

    monkeypatch.setattr(workflow_store, "node_briefs", orphan_node)
    assert await agent_loop_mod._agent_started_extra(SimpleNamespace(workflow_node_id=5)) == {}


async def test_agent_started_extra_never_blocks_startup(monkeypatch):
    async def broken(node_ids):
        raise RuntimeError("数据库暂不可用")

    monkeypatch.setattr(workflow_store, "node_briefs", broken)
    assert await agent_loop_mod._agent_started_extra(SimpleNamespace(workflow_node_id=5)) == {}


async def test_start_attributes_nodes_to_the_starting_turn(tmp_path):
    executor = ScriptedExecutor()
    rt = _runtime(executor)
    pid = await rt.create_program(
        session_id=await _session(),
        plan=_backend_plan(),
        workspace=str(tmp_path),
        origin_turn_id=10,
    )
    await rt.start(pid, turn_id=11)
    await rt.cancel(pid)
    await rt.run_until_idle(timeout=5)

    snap = await workflow_store.load_program(pid)
    assert snap["run"]["origin_turn_id"] == 10, "规划轮不变"
    assert snap["run"]["last_turn_id"] == 11, "节点归属本次启动所在的轮"


async def test_resume_from_tool_moves_nodes_to_the_resuming_turn(tmp_path):
    transient = NodeResult(status="failed", transient=True, error="网关超时")
    executor = ScriptedExecutor(
        {"impl": [transient, transient, NodeResult(status="done", output={"summary": "ok"})]}
    )
    rt = _runtime(executor, retry_plan=lambda: [0.0])
    pid = await rt.create_program(
        session_id=await _session(),
        plan=_backend_plan(),
        workspace=str(tmp_path),
        origin_turn_id=10,
    )
    await rt.start(pid, turn_id=11)
    await rt.run_until_idle(timeout=20)
    assert (await workflow_store.load_program(pid))["run"]["status"] == "paused"

    await rt.resume(pid, turn_id=12)
    await rt.run_until_idle(timeout=20)

    assert await _program_status(pid) == "succeeded"
    assert (await workflow_store.load_program(pid))["run"]["last_turn_id"] == 12


async def _node_agent(session_id: int, node_id: int, *, with_task_message: bool) -> tuple[int, int | None]:
    """建一个工作流节点子代理的 Agent 行；可选再建其线程首条消息（任务说明，派发时刻的锚点）。"""

    def _op(s):
        agent = AgentRow(
            kind="sub",
            name="audit·spawn-test",
            model_id=None,
            session_id=session_id,
            turn_id=None,
            parent_agent_id=None,
            workflow_node_id=node_id,
            status="running",
        )
        s.add(agent)
        s.flush()
        first_id = None
        if with_task_message:
            first = MessageRow(
                session_id=session_id,
                turn_id=None,
                thread_id=agent.id,
                sender_type="user",
                sender_id=None,
                msg_type="text",
                content={"text": "任务说明"},
            )
            s.add(first)
            s.flush()
            first_id = first.id
        s.commit()
        return agent.id, first_id

    return await run_write_locked(_op, label="test.wf_order.node_agent")


async def test_agent_started_extra_reports_spawn_watermark(monkeypatch):
    async def fake_briefs(node_ids):
        return {node_ids[0]: {"node_id": node_ids[0], "program_id": 9}}

    monkeypatch.setattr(workflow_store, "node_briefs", fake_briefs)
    session_id = await _session()
    agent_id, first_msg_id = await _node_agent(session_id, node_id=5, with_task_message=True)

    extra = await agent_loop_mod._agent_started_extra(SimpleNamespace(id=agent_id, workflow_node_id=5))

    assert extra == {"workflow_program_id": 9, "spawn_message_id": first_msg_id}


async def test_agent_started_extra_without_thread_messages_has_no_watermark(monkeypatch):
    async def fake_briefs(node_ids):
        return {node_ids[0]: {"node_id": node_ids[0], "program_id": 9}}

    monkeypatch.setattr(workflow_store, "node_briefs", fake_briefs)
    session_id = await _session()
    agent_id, _ = await _node_agent(session_id, node_id=6, with_task_message=False)

    extra = await agent_loop_mod._agent_started_extra(SimpleNamespace(id=agent_id, workflow_node_id=6))

    assert extra == {"workflow_program_id": 9}


async def test_subagent_listing_carries_spawn_watermark_for_reload(monkeypatch):
    """刷新后的 REST 子代理列表与实时事件口径一致：节点子代理带派发时刻，普通子代理没有。"""

    async def fake_briefs(node_ids):
        return {nid: {"node_id": nid, "program_id": 9} for nid in node_ids}

    monkeypatch.setattr(workflow_store, "node_briefs", fake_briefs)
    app = FastAPI()
    app.include_router(turns_router.router, prefix="/api")
    client = TestClient(app)
    session_id = await _session()
    node_agent_id, first_msg_id = await _node_agent(session_id, node_id=7, with_task_message=True)
    plain_agent_id, _ = await _node_agent(session_id, node_id=None, with_task_message=True)

    resp = client.get(f"/api/turns/sessions/{session_id}/subagents")

    assert resp.status_code == 200
    rows = {row["agent_id"]: row for row in resp.json()}
    assert rows[node_agent_id]["spawn_message_id"] == first_msg_id
    assert rows[node_agent_id]["workflow"]["program_id"] == 9
    assert rows[plain_agent_id]["spawn_message_id"] is None
