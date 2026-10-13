"""续跑 / 返工的尝试分隔线（设计 §5.5）：同一子代理线程里，每次新的尝试前有一条系统消息。"""

from sqlalchemy import select

from app.core.enums import MsgType
from app.orchestration.workflow import executor_subagent
from app.orchestration.workflow.executor import NodeRequest
from app.persistence.database import async_session_factory
from app.persistence.models.agent import Agent
from app.persistence.models.message import Message
from tests.test_workflow_runtime import _session


async def _agent(session_id: int) -> int:
    async with async_session_factory() as db:
        row = Agent(kind="sub", name="实现 子代理", session_id=session_id, status="done")
        db.add(row)
        await db.flush()
        agent_id = int(row.id)
        await db.commit()
    return agent_id


def _request(session_id: int, agent_id: int, attempt_no: int, mode: str) -> NodeRequest:
    return NodeRequest(
        run_id=1,
        session_id=session_id,
        flow_key="backend",
        node_key="impl",
        node_id=1,
        attempt_id=1,
        attempt_no=attempt_no,
        kind="execute",
        role="implementer",
        task="实现扣减",
        acceptance=(),
        write_scope=(),
        mode=mode,
        feedback="",
        agent_id=agent_id,
    )


async def test_a_resumed_attempt_leaves_a_divider_in_its_thread(tmp_path):
    sid = await _session()
    agent_id = await _agent(sid)

    await executor_subagent._mark_attempt(_request(sid, agent_id, 2, "checkpoint"), "续跑")

    async with async_session_factory() as db:
        rows = (
            (await db.execute(select(Message).where(Message.thread_id == agent_id))).scalars().all()
        )
    assert [(m.msg_type, m.content.get("text")) for m in rows] == [
        (MsgType.SYSTEM.value, "第 2 次尝试 · 续跑")
    ]


async def test_each_new_attempt_adds_its_own_divider_in_order(tmp_path):
    sid = await _session()
    agent_id = await _agent(sid)

    await executor_subagent._mark_attempt(_request(sid, agent_id, 2, "rework"), "返工")
    await executor_subagent._mark_attempt(_request(sid, agent_id, 3, "checkpoint"), "续跑")

    async with async_session_factory() as db:
        rows = (
            (
                await db.execute(
                    select(Message).where(Message.thread_id == agent_id).order_by(Message.id.asc())
                )
            )
            .scalars()
            .all()
        )
    assert [m.content.get("text") for m in rows] == [
        "第 2 次尝试 · 返工",
        "第 3 次尝试 · 续跑",
    ]


async def test_a_missing_agent_adds_no_divider(tmp_path):
    sid = await _session()
    await executor_subagent._mark_attempt(_request(sid, None, 2, "checkpoint"), "续跑")  # type: ignore[arg-type]
