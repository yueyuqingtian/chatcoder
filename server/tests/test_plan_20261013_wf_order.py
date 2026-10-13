"""plan-20261013-wf-order：工作流卡与节点子代理卡按创建序固化（服务端部分）。

覆盖点：
- agent.started 对工作流节点子代理附带所属程序编号；普通子代理不附带；解析失败不阻塞启动。
- start / resume 由工作流工具调用时，把节点归属轮次更新为当前轮（节点卡因此落在启动所在轮）。
"""
import sys
from pathlib import Path
from types import SimpleNamespace

from app.orchestration import agent_loop as agent_loop_mod
from app.orchestration.workflow import store as workflow_store
from app.orchestration.workflow.executor import NodeResult

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
