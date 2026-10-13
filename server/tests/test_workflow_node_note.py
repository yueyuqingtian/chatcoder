"""运行中步骤的补充说明（设计 §7.6）：经子代理的指令通道送达，未在运行则拒绝。"""

import pytest

from app.orchestration import subagent as subagent_module
from app.orchestration.workflow import store
from app.persistence.database import async_session_factory
from app.persistence.models.agent import Agent
from tests.test_workflow_runtime import ScriptedExecutor, _backend_plan, _runtime, _session


async def _agent(session_id: int) -> int:
    async with async_session_factory() as db:
        row = Agent(kind="sub", name="实现 子代理", session_id=session_id, status="running")
        db.add(row)
        await db.flush()
        agent_id = int(row.id)
        await db.commit()
    return agent_id


async def _impl_id(program_id: int) -> int:
    snap = await store.load_program(program_id)
    flow = next(f for f in snap["flows"] if f["run"]["package_key"] == "backend")
    return next(n["id"] for n in flow["nodes"] if n["key"] == "impl")


async def test_a_note_is_refused_when_the_step_is_not_running(tmp_path):
    rt = _runtime(ScriptedExecutor())
    pid = await rt.create_program(
        session_id=await _session(), plan=_backend_plan(), workspace=str(tmp_path)
    )

    with pytest.raises(ValueError, match="不在运行"):
        await rt.add_node_note(pid, "backend", "impl", "改用方案 B")


async def test_a_running_step_receives_the_note_through_the_subagent_channel(tmp_path, monkeypatch):
    rt = _runtime(ScriptedExecutor())
    pid = await rt.create_program(
        session_id=await _session(), plan=_backend_plan(), workspace=str(tmp_path)
    )
    agent_id = await _agent(await _session())
    await store.update_node(await _impl_id(pid), status="running", agent_id=agent_id)
    delivered: list[tuple[int, str]] = []

    class FakeManager:
        def push_agent_input(self, agent_id: int, message: str) -> bool:
            delivered.append((agent_id, message))
            return True

    monkeypatch.setattr(subagent_module, "get_subagent_manager", lambda sid: FakeManager())

    await rt.add_node_note(pid, "backend", "impl", "  改用方案 B  ")

    assert delivered == [(agent_id, "改用方案 B")]
    events = await store.list_events(pid)
    assert any("补充说明" in str(e.get("payload")) for e in events)


async def test_a_note_the_channel_cannot_deliver_is_reported_as_refused(tmp_path, monkeypatch):
    rt = _runtime(ScriptedExecutor())
    pid = await rt.create_program(
        session_id=await _session(), plan=_backend_plan(), workspace=str(tmp_path)
    )
    agent_id = await _agent(await _session())
    await store.update_node(await _impl_id(pid), status="running", agent_id=agent_id)

    class ClosedManager:
        def push_agent_input(self, agent_id: int, message: str) -> bool:
            return False

    monkeypatch.setattr(subagent_module, "get_subagent_manager", lambda sid: ClosedManager())

    with pytest.raises(ValueError, match="无法送达"):
        await rt.add_node_note(pid, "backend", "impl", "改用方案 B")
