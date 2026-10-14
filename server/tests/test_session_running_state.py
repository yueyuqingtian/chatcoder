"""会话执行态：后台工作流算作「执行中」、停止按钮全量停止、执行清单续接（todo_carry）。"""

from app.orchestration import engine, todo_carry
from app.orchestration.workflow import service as workflow_service
from app.orchestration.workflow import store
from app.persistence.database import async_session_factory
from app.persistence.models.task import Task
from app.persistence.models.turn import Turn
from app.services import session_service
from tests.test_workflow_runtime import (
    ScriptedExecutor,
    _backend_plan,
    _program_status,
    _runtime,
    _session,
)


async def _busy_by_workflow(sid: int) -> bool:
    async with async_session_factory() as db:
        return await session_service.has_running_workflow(db, sid)


async def test_a_running_workflow_makes_the_session_busy_without_any_turn(tmp_path):
    rt = _runtime(ScriptedExecutor())
    sid = await _session()
    pid = await rt.create_program(session_id=sid, plan=_backend_plan(), workspace=str(tmp_path))
    assert await _busy_by_workflow(sid) is False  # 草稿不算执行中

    await rt.start(pid)
    assert await _program_status(pid) == "running"
    assert await _busy_by_workflow(sid) is True  # 启动后即使没有运行中的 turn，会话也是执行中

    await rt.pause_session_programs(sid, kind="turn_stop", reason="已停止")
    assert await _busy_by_workflow(sid) is False


async def test_stop_session_execution_pauses_the_running_workflows_and_reports_counts(tmp_path, monkeypatch):
    rt = _runtime(ScriptedExecutor())
    monkeypatch.setattr(workflow_service, "get_runtime", lambda: rt)
    sid = await _session()
    pid = await rt.create_program(session_id=sid, plan=_backend_plan(), workspace=str(tmp_path))
    await rt.start(pid)

    result = await engine.stop_session_execution(sid)

    # wakeup_cancelled（plan-20261014-actions-wait）：待送达的工作流唤醒被取消的数量；这里没有待送达的唤醒
    assert result == {"turns": 0, "agent_managers": 0, "workflows_paused": 1, "wakeup_cancelled": 0}
    assert await _program_status(pid) == "paused"


async def _add_turn(sid: int, status: str) -> int:
    async with async_session_factory() as db:
        turn = Turn(session_id=sid, status=status)
        db.add(turn)
        await db.commit()
        await db.refresh(turn)
        return turn.id


async def _add_todo_group(sid: int, turn_id: int, items: list[tuple[str, str]]) -> None:
    """items: (标题, 状态)，状态取 done / pending / cancelled 等落库值。"""
    async with async_session_factory() as db:
        group = Task(session_id=sid, turn_id=turn_id, title="任务清单", kind="group", status="running", priority=0)
        db.add(group)
        await db.flush()
        for index, (title, status) in enumerate(items):
            db.add(Task(
                session_id=sid, turn_id=turn_id, parent_task_id=group.id, kind="step",
                title=title, priority=index, status=status,
            ))
        await db.commit()


async def test_unfinished_list_of_the_previous_turn_is_carried_into_the_next_turn():
    sid = await _session()
    previous = await _add_turn(sid, "interrupted")
    await _add_todo_group(sid, previous, [("读取配置", "done"), ("改写调度器", "cancelled"), ("补充测试", "pending")])
    current = await _add_turn(sid, "running")

    async with async_session_factory() as db:
        steps = await todo_carry.load_carry_steps(db, session_id=sid, turn_id=current, mid_turn=False)

    assert [s.title for s in steps] == ["读取配置", "改写调度器", "补充测试"]
    text = todo_carry.reminder_text(steps, mid_turn=False)
    assert "改写调度器" in text and "todo_write" in text
    assert [t["status"] for t in todo_carry.as_todos(steps)] == ["completed", "pending", "pending"]


async def test_a_fully_finished_previous_list_is_not_carried():
    sid = await _session()
    previous = await _add_turn(sid, "completed")
    await _add_todo_group(sid, previous, [("A", "done"), ("B", "done")])
    current = await _add_turn(sid, "running")

    async with async_session_factory() as db:
        assert await todo_carry.load_carry_steps(db, session_id=sid, turn_id=current, mid_turn=False) is None


async def test_a_mid_turn_supplement_checks_the_current_turn_list_before_the_previous_one():
    sid = await _session()
    previous = await _add_turn(sid, "completed")
    await _add_todo_group(sid, previous, [("旧项", "pending")])
    current = await _add_turn(sid, "running")
    await _add_todo_group(sid, current, [("本轮已完成", "done")])

    async with async_session_factory() as db:
        # 本轮清单已全部完成：不再回头续接更早一轮的未完成项
        assert await todo_carry.load_carry_steps(db, session_id=sid, turn_id=current, mid_turn=True) is None
