"""取消流程回归测试。"""
import asyncio

import pytest

from app.orchestration.subagent import SubagentHandle, SubagentManager


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
