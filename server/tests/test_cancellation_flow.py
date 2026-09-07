"""取消流程回归测试。"""
import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.orchestration import engine as turn_engine
from app.orchestration.subagent import SubagentHandle, SubagentManager
from app.persistence.database import Base
from app.persistence.models import Agent, Session, Task, Turn


@pytest.mark.asyncio
async def test_subagent_manager_cancel_all_waits_for_tasks():
    manager = SubagentManager(session_id=1)
    stopped = asyncio.Event()

    async def worker():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            stopped.set()
            raise

    handle = SubagentHandle(agent_id=1, task=asyncio.create_task(worker()))
    manager._handles[1] = handle
    await asyncio.sleep(0)
    await manager.cancel_all()

    assert stopped.is_set()
    assert handle.task.done()
    assert handle.status == "cancelled"


@pytest.mark.asyncio
async def test_repeated_cancel_all_is_idempotent():
    manager = SubagentManager(session_id=1)
    manager._handles[1] = SubagentHandle(agent_id=1)
    await manager.cancel_all()
    await manager.cancel_all()
    assert manager.pending_count() == 0


@pytest.mark.asyncio
async def test_cancel_finalizer_marks_active_work_and_preserves_completed(tmp_path, monkeypatch):
    """取消收尾应更新活动 turn，但不能把已正常完成的 turn 改回 interrupted。"""
    db_path = tmp_path / "cancel.db"
    db_engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    async with db_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    from app.persistence import write_engine as _we
    _we.configure(f"sqlite+aiosqlite:///{db_path}", foreign_keys=False)  # 写引擎（单写线程）与测试库同源
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    monkeypatch.setattr("app.persistence.database.async_session_factory", factory)

    async with factory() as db:
        session = Session(title="cancel-test")
        db.add(session)
        await db.flush()
        active = Turn(session_id=session.id, status="running")
        completed = Turn(session_id=session.id, status="completed")
        db.add_all([active, completed])
        await db.flush()
        db.add_all([
            Task(session_id=session.id, turn_id=active.id, title="active", status="running"),
            Task(session_id=session.id, turn_id=completed.id, title="done", status="done"),
            Agent(session_id=session.id, turn_id=active.id, name="a", status="running"),
        ])
        await db.commit()
        active_id = active.id
        completed_id = completed.id

    monkeypatch.setattr(turn_engine, "broadcast_turn_updated", _noop)
    monkeypatch.setattr(turn_engine, "broadcast", _noop)
    monkeypatch.setattr(turn_engine, "broadcast_session_completed", _noop)
    await turn_engine._finalize_cancelled_turn(active_id)
    await turn_engine._finalize_cancelled_turn(completed_id)

    async with factory() as db:
        active_row = await db.get(Turn, active_id)
        completed_row = await db.get(Turn, completed_id)
        active_tasks = (await db.execute(select(Task).where(Task.turn_id == active_id))).scalars().all()
        active_agents = (await db.execute(select(Agent).where(Agent.turn_id == active_id))).scalars().all()
        assert active_row.status == "interrupted"
        assert completed_row.status == "completed"
        assert all(t.status == "cancelled" for t in active_tasks)
        assert all(a.status == "terminated" for a in active_agents)
    await db_engine.dispose()


async def _noop(*_args, **_kwargs):
    return None
