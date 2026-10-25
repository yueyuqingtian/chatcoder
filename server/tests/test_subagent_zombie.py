"""plan-565-2323 G7/G9：子代理残留状态的收尾回归。

事故背景（用户实测）：子代理因凭据错误失败、自动重试三次后仍显示「执行中」；
主代理已停止，界面没有终止入口；**关闭软件重启后依然显示子代理在运行**，该会话彻底不可用。
根因有三条：保活循环没有上限、终态不落库、启动修复只扫 `Turn.status == "running"`。

本文件守住两条收尾路径：
1. `cancel_all` 把僵尸 handle（内存仍 running、task 早已结束）的终态写进库；
2. 启动的残留收尾把「所属轮次已结束、行仍非终态」的子任务与子代理一次性收掉。
"""
import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.persistence.database import Base
from app.persistence.models.agent import Agent
from app.persistence.models.task import Task
from app.persistence.models.turn import Turn


@pytest.fixture
async def db():
    """用全局 session factory（conftest 的 autouse fixture 已建表并清空数据）。

    为什么不自建隔离 engine：被测路径 `_sync_task` / `_persist_agent_state` 走的是全局
    `async_session_factory`，而 `run_write_locked` 的写入连接又从全局异步引擎派生 URL——
    三者必须指向同一个库文件，测试看到的才是真实落库结果。
    """
    from app.persistence.database import async_session_factory

    async with async_session_factory() as session:
        yield session


async def _seed_turn(db, status: str = "completed"):
    """建会话 + 轮次（不依赖会话服务的完整字段，只写状态相关列）。"""
    from app.persistence.models.message import Session

    sess = Session(title="回归")
    db.add(sess)
    await db.flush()
    turn = Turn(session_id=sess.id, status=status)
    db.add(turn)
    await db.commit()
    return sess.id, turn.id


async def _repair() -> tuple[int, int]:
    """与启动流程同路径调用：repair_stale_rows 设计上跑在 WriteEngine 的单写线程里（同步 Session）。"""
    from app.main import repair_stale_rows
    from app.persistence.database import run_write_locked

    return await run_write_locked(repair_stale_rows, label="test.repair_residue")


async def _reload_task(db, task_id: int) -> Task:
    # expire_all：被测路径在 WriteEngine 的独立连接里提交，本 session 的 identity map
    # 里还是 add 时的旧对象（expire_on_commit=False），不刷新就会读到陈旧值。
    db.expire_all()
    return (await db.execute(select(Task).where(Task.id == task_id))).scalar_one()


async def _reload_agent(db, agent_id: int) -> Agent:
    db.expire_all()
    return (await db.execute(select(Agent).where(Agent.id == agent_id))).scalar_one()


# ── G9：启动残留收尾 ──

async def test_repair_stale_rows_closes_tasks_under_finished_turn(db):
    """turn 已终态、任务仍 in_progress：启动收尾必须把它置 cancelled。"""
    sid, tid = await _seed_turn(db, status="interrupted")
    agent = Agent(kind="sub", name="子代理", session_id=sid, turn_id=tid, status="running")
    db.add(agent)
    await db.flush()
    task = Task(session_id=sid, turn_id=tid, title="卡住的任务", agent_id=agent.id,
                kind="step", status="in_progress")
    db.add(task)
    await db.commit()
    # expire_all 后属性会惰性加载（异步会话里会抛 MissingGreenlet），先取出主键
    aid, task_row_id = agent.id, task.id

    tasks_fixed, agents_fixed = await _repair()

    assert tasks_fixed == 1
    assert agents_fixed == 1
    assert (await _reload_task(db, task_row_id)).status == "cancelled"
    ag = await _reload_agent(db, aid)
    assert ag.status == "terminated"


async def test_repair_stale_rows_leaves_running_turn_alone(db):
    """正在跑的轮次不受影响——收尾只针对「轮次已结束」的残留，不能误杀在跑的子任务。"""
    sid, tid = await _seed_turn(db, status="running")
    agent = Agent(kind="sub", name="子代理", session_id=sid, turn_id=tid, status="running")
    db.add(agent)
    await db.flush()
    task = Task(session_id=sid, turn_id=tid, title="在跑的任务", agent_id=agent.id,
                kind="step", status="in_progress")
    db.add(task)
    await db.commit()
    aid, task_row_id = agent.id, task.id

    tasks_fixed, agents_fixed = await _repair()

    assert (tasks_fixed, agents_fixed) == (0, 0)
    assert (await _reload_task(db, task_row_id)).status == "in_progress"
    ag = await _reload_agent(db, aid)
    assert ag.status == "running"


async def test_repair_stale_rows_is_idempotent(db):
    """重复启动不重复计数，也不把已终态的行改回去。"""
    sid, tid = await _seed_turn(db, status="completed")
    agent = Agent(kind="sub", name="子代理", session_id=sid, turn_id=tid, status="terminated")
    db.add(agent)
    await db.flush()
    task = Task(session_id=sid, turn_id=tid, title="残留", agent_id=agent.id,
                kind="step", status="pending")
    db.add(task)
    await db.commit()
    task_row_id = task.id

    assert await _repair() == (1, 0)
    assert await _repair() == (0, 0)
    assert (await _reload_task(db, task_row_id)).status == "cancelled"


async def test_repair_stale_rows_leaves_main_tasks_alone(db):
    """收尾范围限定子代理任务：主任务的 pending/running 一个不动（plan-565-2323 实测校准）。

    真实库里「轮次已结束仍非终态」的 169 条中，167 条是主任务步骤——它们前端本来就不算
    「子代理运行中」，清了只会把历史会话里一批无关任务改成「已取消」。
    """
    sid, tid = await _seed_turn(db, status="interrupted")
    main_agent = Agent(kind="main", name="主代理", session_id=sid, turn_id=tid, status="running")
    db.add(main_agent)
    await db.flush()
    main_task = Task(session_id=sid, turn_id=tid, title="主任务步骤", agent_id=main_agent.id,
                     kind="step", status="pending")
    orphan_task = Task(session_id=sid, turn_id=tid, title="无归属任务", kind="step",
                       status="running")
    db.add_all([main_task, orphan_task])
    await db.commit()
    main_id, orphan_id = main_task.id, orphan_task.id

    tasks_fixed, _agents_fixed = await _repair()

    assert tasks_fixed == 0, "主任务与无归属任务不应被收尾"
    assert (await _reload_task(db, main_id)).status == "pending"
    assert (await _reload_task(db, orphan_id)).status == "running"


# ── G7：cancel_all 把僵尸 handle 的终态落库 ──

async def test_cancel_all_persists_terminal_state_for_zombie_handle(db):
    """内存 handle 仍是 running、task 早已 done（僵尸）：cancel_all 必须补写库里的终态。

    旧实现只改内存 status、从不落库，所以重启后 agents/tasks 仍是 running，会话永远显示运行中。
    """
    sid, tid = await _seed_turn(db, status="interrupted")
    agent = Agent(kind="sub", name="僵尸子代理", session_id=sid, turn_id=tid, status="running")
    db.add(agent)
    await db.flush()
    task = Task(session_id=sid, turn_id=tid, title="僵尸任务", agent_id=agent.id,
                kind="step", status="in_progress")
    db.add(task)
    await db.commit()
    aid, task_row_id = agent.id, task.id

    from app.orchestration.subagent import SubagentHandle, SubagentManager

    mgr = SubagentManager(session_id=sid)
    handle = SubagentHandle(agent_id=aid, turn_id=tid, task_title="僵尸任务")
    # 关键：task 为 None（等价于“早已结束”），状态仍停在 running
    handle.task = None
    handle.status = "running"
    handle.run_spec = {"task_id": task_row_id}
    mgr._handles[aid] = handle

    await mgr.cancel_all()

    assert handle.status == "cancelled"
    assert (await _reload_task(db, task_row_id)).status == "cancelled"
    ag = await _reload_agent(db, aid)
    assert ag.status == "cancelled"


async def test_cancel_reports_not_running_but_heals_db(db):
    """内存已是终态时 cancel 返回 not_running，同时把库里残留的非终态行补写成该终态（自愈，不报错）。"""
    sid, tid = await _seed_turn(db, status="interrupted")
    agent = Agent(kind="sub", name="已结束子代理", session_id=sid, turn_id=tid, status="idle")
    db.add(agent)
    await db.flush()
    task = Task(session_id=sid, turn_id=tid, title="残留任务", agent_id=agent.id,
                kind="step", status="in_progress")
    db.add(task)
    await db.commit()
    aid, task_row_id = agent.id, task.id

    from app.orchestration.subagent import SubagentHandle, SubagentManager

    mgr = SubagentManager(session_id=sid)
    handle = SubagentHandle(agent_id=aid, turn_id=tid, task_title="残留任务")
    handle.task = None
    handle.status = "done"
    handle.run_spec = {"task_id": task_row_id}
    mgr._handles[aid] = handle

    result = await mgr.cancel(aid)

    assert result == "not_running"
    assert (await _reload_task(db, task_row_id)).status == "done"


# ── G6：永久性错误判定 ──

def test_credential_error_is_not_transient():
    """凭据类错误必须判永久——否则子代理会进入保活循环，永远不会失败收场。"""
    from app.orchestration.agent_loop import _is_transient_failure_text

    assert not _is_transient_failure_text("provider_credential_unavailable")
    assert not _is_transient_failure_text("provider_credentials_all_disabled")
    assert not _is_transient_failure_text("invalid api key")
    # 对照：真正的瞬时故障仍要保活
    assert _is_transient_failure_text("read timeout after 30s")
    assert _is_transient_failure_text("HTTP 503 service unavailable")
