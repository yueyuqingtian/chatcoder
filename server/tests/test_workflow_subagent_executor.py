"""工作流节点的真实执行器：节点以子代理运行，与普通子代理共用线程与面板，且不推送主代理。

用假的 run_agent_loop 驱动 SubagentManager，避免依赖真实模型。
"""

import pytest
from sqlalchemy import select

from app.orchestration import subagent as subagent_mod
from app.orchestration.agent_loop import AgentOutput
from app.orchestration.workflow.conflicts import WriteLeaseTable
from app.orchestration.workflow.executor_subagent import SubagentNodeExecutor
from app.orchestration.workflow.runtime import WorkflowRuntime
from app.persistence.database import async_session_factory
from app.persistence.models.agent import Agent as AgentRow
from app.persistence.models.message import Message
from tests.test_workflow_runtime import (
    _backend_plan,
    _no_commands,
    _noop_broadcast,
    _program_status,
    _session,
)

VERDICT = (
    '{"decision": "pass", "summary": "通过", '
    '"criteria": [{"id": "C1", "result": "met", "evidence": "ok"}], "issues": []}'
)


@pytest.fixture
def fake_loop(monkeypatch):
    names: list[str] = []

    async def _fake_run_agent_loop(db, **kwargs):
        agent = kwargs.get("agent")
        name = agent.name if agent is not None else ""
        names.append(name)
        if name.endswith("·review"):
            return AgentOutput(kind="message", text=VERDICT)
        return AgentOutput(kind="message", text=f"{name} 已完成")

    monkeypatch.setattr(subagent_mod, "run_agent_loop", _fake_run_agent_loop)
    return names


async def test_workflow_nodes_run_as_subagents_without_notifying_main_agent(fake_loop, tmp_path):
    session_id = await _session()
    rt = WorkflowRuntime(
        SubagentNodeExecutor(),
        leases=WriteLeaseTable(),
        command_runner=_no_commands,
        retry_plan=lambda: [],
        broadcast=_noop_broadcast,
    )
    pid = await rt.create_program(
        session_id=session_id, plan=_backend_plan(), workspace=str(tmp_path)
    )
    await rt.start(pid)
    await rt.run_until_idle(timeout=120)

    assert await _program_status(pid) == "succeeded"
    assert fake_loop == ["backend·explore", "backend·impl", "backend·review"]

    async with async_session_factory() as s:
        agents = (
            (
                await s.execute(
                    select(AgentRow).where(
                        AgentRow.session_id == session_id, AgentRow.workflow_node_id.is_not(None)
                    )
                )
            )
            .scalars()
            .all()
        )
        agent_ids = {a.id: a.name for a in agents}
        assert sorted(agent_ids.values()) == ["backend·explore", "backend·impl", "backend·review"]
        # 每个节点子代理的线程首条消息是任务说明（与普通子代理面板一致）
        thread_msgs = (
            (
                await s.execute(
                    select(Message.thread_id).where(Message.thread_id.in_(list(agent_ids)))
                )
            )
            .scalars()
            .all()
        )
        assert set(thread_msgs) == set(agent_ids)

    manager = subagent_mod.get_subagent_manager(session_id)
    for agent_id in agent_ids:
        handle = manager.get(agent_id)
        assert handle is not None and handle.internal is True
    # 节点结果不进入主代理的完成通知队列，也不触发唤醒
    assert manager._completion_queue == []
    assert manager.spawned_count(None) == 0
