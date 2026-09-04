"""plan-671: 目标模式（对齐 zcode goal-continuation）单测。

覆盖：
- Goal API：设定/查询/取消/用户确认完成的状态流转。
- goal_complete 工具：激活时标记完成；未激活时错误返回；registry 注册。
- 续跑判定矩阵：completed+active→续跑；cancelled/failed/interrupted→不续跑；
  轮次耗尽→不续跑；goal_mode_enabled=False→不续跑；cancel_event→不续跑。
- _continue_goal_turn：创建带 goal_continuation 标记的消息 + 新 turn + 计数递增。
- 上下文注入：有目标时 Current Goal 段为目标文本，本轮消息降级为 Current Task。
"""
import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.gateway.routers.sessions import cancel_goal, get_goal, set_goal
from app.gateway.schemas import GoalSetBody
from app.orchestration import engine as engine_mod
from app.orchestration.context_manager import build_main_context
from app.orchestration.tools.base import ToolContext
from app.orchestration.tools.goal import GoalCompleteTool
from app.orchestration.tools.registry import get_tool_registry
from app.persistence.database import Base
from app.persistence.models import Turn  # noqa: F401 注册全部模型到 Base.metadata
from app.persistence.models.message import Session as SessionModel


@pytest.fixture
async def db_env():
    """内存库 + 会话工厂（工厂供 _continue_goal_turn 的独立连接路径复用）。"""
    eng = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(eng, expire_on_commit=False)
    async with factory() as session:
        yield session, factory
    await eng.dispose()


@pytest.fixture
def goal_settings(monkeypatch):
    monkeypatch.setattr(settings, "goal_mode_enabled", True, raising=False)
    monkeypatch.setattr(settings, "goal_max_continuation_turns", 3, raising=False)
    monkeypatch.setattr(settings, "goal_continuation_interval_sec", 0.0, raising=False)


async def _mk_session(db, **kw):
    s = SessionModel(id=kw.pop("id", 1), **kw)
    db.add(s)
    await db.commit()
    return s


# ───────────────── Goal API 状态流转 ─────────────────


async def test_goal_api_set_get_cancel_complete(db_env):
    db, _ = db_env
    await _mk_session(db)

    out = await set_goal(1, GoalSetBody(text="修复登录页深色主题样式"), db)
    assert out.status == "active"
    assert out.text == "修复登录页深色主题样式"
    assert out.turns_used == 0
    assert out.max_turns == settings.goal_max_continuation_turns

    # 查询回读
    got = await get_goal(1, db)
    assert got.status == "active"

    # 替换目标：状态保持 active、计数清零
    out2 = await set_goal(1, GoalSetBody(text="新目标"), db)
    assert out2.status == "active" and out2.text == "新目标"

    # 用户确认完成（complete=true）
    out3 = await cancel_goal(1, complete=True, db=db)
    assert out3.status == "completed"
    assert (await db.get(SessionModel, 1)).goal_status == "completed"

    # 已完成后再取消：状态不再变化（非 active 不处理）
    out4 = await cancel_goal(1, db=db)
    assert out4.status == "completed"


async def test_goal_api_cancel(db_env):
    db, _ = db_env
    await _mk_session(db)
    await set_goal(1, GoalSetBody(text="目标A"), db)
    out = await cancel_goal(1, db=db)
    assert out.status == "cancelled"
    assert (await db.get(SessionModel, 1)).goal_status == "cancelled"


async def test_goal_api_empty_text_rejected(db_env):
    db, _ = db_env
    await _mk_session(db)
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        await set_goal(1, GoalSetBody(text="   "), db)
    assert ei.value.status_code == 400


# ───────────────── goal_complete 工具 ─────────────────


def _tool_ctx(db, session_id=1, turn_id=10):
    return ToolContext(
        workspace_root=".", session_id=session_id, task_id=turn_id,
        agent_id=1, agent_name="main", db=db,
    )


async def test_goal_complete_tool_marks_completed(db_env):
    db, _ = db_env
    await _mk_session(db, goal_status="active", goal_text="目标A")
    tool = GoalCompleteTool()
    res = await tool.run({"summary": "已修复并通过验证"}, _tool_ctx(db))
    assert res.ok
    assert (await db.get(SessionModel, 1)).goal_status == "completed"


async def test_goal_complete_tool_rejects_inactive(db_env):
    db, _ = db_env
    await _mk_session(db)  # goal_status 默认 none
    tool = GoalCompleteTool()
    res = await tool.run({"summary": "x"}, _tool_ctx(db))
    assert not res.ok
    assert "没有激活的目标" in res.error


async def test_goal_complete_tool_empty_summary(db_env):
    db, _ = db_env
    await _mk_session(db, goal_status="active", goal_text="目标A")
    tool = GoalCompleteTool()
    res = await tool.run({"summary": "  "}, _tool_ctx(db))
    assert not res.ok


def test_goal_complete_registered():
    assert get_tool_registry().get("goal_complete") is not None


# ───────────────── 续跑判定矩阵 ─────────────────


def _sess(goal_status="active", turns_used=0):
    return SimpleNamespace(goal_status=goal_status, goal_turns_used=turns_used)


def test_should_continue_goal_matrix(goal_settings):
    cancel_event = asyncio.Event()

    # completed + active + 轮次未耗尽 → 续跑
    assert engine_mod._should_continue_goal(_sess(), "completed", cancel_event) is True

    # 非 completed 状态 → 不续跑
    for status in ("cancelled", "failed", "interrupted"):
        assert engine_mod._should_continue_goal(_sess(), status, cancel_event) is False

    # 目标非 active → 不续跑
    for gs in ("none", "completed", "cancelled"):
        assert engine_mod._should_continue_goal(_sess(goal_status=gs), "completed", cancel_event) is False

    # 轮次耗尽 → 不续跑
    assert engine_mod._should_continue_goal(_sess(turns_used=3), "completed", cancel_event) is False
    assert engine_mod._should_continue_goal(_sess(turns_used=2), "completed", cancel_event) is True

    # cancel_event 已设置 → 不续跑
    cancel_event.set()
    assert engine_mod._should_continue_goal(_sess(), "completed", cancel_event) is False


def test_should_continue_goal_disabled(monkeypatch):
    monkeypatch.setattr(settings, "goal_mode_enabled", False, raising=False)
    cancel_event = asyncio.Event()
    assert engine_mod._should_continue_goal(_sess(), "completed", cancel_event) is False


# ───────────────── _continue_goal_turn ─────────────────


async def test_continue_goal_turn_creates_flagged_turn(db_env, goal_settings, monkeypatch):
    db, factory = db_env
    await _mk_session(db, goal_status="active", goal_text="修复登录页", goal_turns_used=0)

    # 独立连接路径指向测试工厂；start_turn 替换为记录桩（不跑真实 agent loop）
    monkeypatch.setattr("app.persistence.database.async_session_factory", factory)
    started: list[int] = []

    async def fake_start_turn(s, *, turn_id, **kw):
        started.append(turn_id)
        return {"ok": True}

    monkeypatch.setattr(engine_mod, "start_turn", fake_start_turn)

    await engine_mod._continue_goal_turn(1, 100)
    # 间隔 0s + 后台 _run 任务完成
    await asyncio.sleep(0.05)
    for tid, task in list(engine_mod._turn_tasks.items()):
        if not task.done():
            await asyncio.wait_for(task, timeout=2)
        engine_mod._turn_tasks.pop(tid, None)

    session = await db.get(SessionModel, 1)
    assert session.goal_turns_used == 1

    from sqlalchemy import select
    from app.persistence.models.message import Message
    msgs = (await db.execute(select(Message).where(Message.session_id == 1))).scalars().all()
    cont = [m for m in msgs if (m.content or {}).get("goal_continuation") is True]
    assert len(cont) == 1
    assert cont[0].sender_type == "user"
    assert "当前目标：修复登录页" in cont[0].content["text"]
    assert cont[0].content["goal_turn"] == 1
    assert cont[0].turn_id is not None
    assert started == [cont[0].turn_id]


async def test_continue_goal_turn_skips_when_not_active(db_env, goal_settings, monkeypatch):
    db, factory = db_env
    await _mk_session(db, goal_status="cancelled", goal_text="目标A")
    monkeypatch.setattr("app.persistence.database.async_session_factory", factory)

    await engine_mod._continue_goal_turn(1, 100)
    await asyncio.sleep(0.02)
    session = await db.get(SessionModel, 1)
    assert session.goal_turns_used == 0  # 未创建续跑


async def test_continue_goal_turn_skips_when_exhausted(db_env, goal_settings, monkeypatch):
    db, factory = db_env
    await _mk_session(db, goal_status="active", goal_text="目标A", goal_turns_used=3)
    monkeypatch.setattr("app.persistence.database.async_session_factory", factory)

    await engine_mod._continue_goal_turn(1, 100)
    await asyncio.sleep(0.02)
    assert (await db.get(SessionModel, 1)).goal_turns_used == 3  # 达上限不再续跑


# ───────────────── plan-676: 创建会话随带目标 ─────────────────


async def test_create_session_with_goal(db_env):
    """plan-676: create_session(goal_text=...) → active + 时间戳 + 2000 截断。"""
    db, _ = db_env
    from app.services import session_service

    s = await session_service.create_session(
        db, project_id=None, goal_text="修复登录页深色主题样式",
    )
    assert s.goal_status == "active"
    assert s.goal_text == "修复登录页深色主题样式"
    assert s.goal_created_at  # 非空 ISO 时间戳

    # 超长截断 2000
    long_s = await session_service.create_session(db, project_id=None, goal_text="x" * 3000)
    assert len(long_s.goal_text) == 2000

    # 空白文本视为未设定
    blank = await session_service.create_session(db, project_id=None, goal_text="   ")
    assert blank.goal_status == "none"


async def test_create_session_without_goal_default(db_env):
    """plan-676: goal_text=None → 默认 none，既有创建路径不受影响。"""
    db, _ = db_env
    from app.services import session_service

    s = await session_service.create_session(db, project_id=None)
    assert s.goal_status == "none"
    assert s.goal_text is None
    assert s.goal_turns_used == 0


def test_session_create_schema_accepts_goal_text():
    """plan-676: SessionCreate schema 接受 goal_text 字段。"""
    from app.gateway.schemas import SessionCreate

    sc = SessionCreate(project_id=1, goal_text="首页设定的目标")
    assert sc.goal_text == "首页设定的目标"
    assert SessionCreate(project_id=2).goal_text is None


# ───────────────── 上下文注入 ─────────────────


async def test_build_main_context_goal_injection(db_env, workspace):
    db, _ = db_env
    agent = SimpleNamespace(id=1, model_id=None, name="main")
    session = SimpleNamespace(
        id=1, worktree_path=None, model_id=None,
        goal_status="active", goal_text="修复登录页深色主题样式", goal_turns_used=2,
    )
    project = SimpleNamespace(path=str(workspace), rules_docs=None)
    turn = SimpleNamespace(id=1)

    bundle = await build_main_context(
        db, agent=agent, session=session, project=project, turn=turn,
        user_message="继续推进", goal={"text": "修复登录页深色主题样式", "turns_used": 2},
    )
    goal_part = next(p for p in bundle.developer_parts if p.startswith("## Current Goal"))
    assert "修复登录页深色主题样式" in goal_part
    assert "已续跑 2 轮" in goal_part
    # 本轮用户消息降级为 Current Task 段
    task_part = next(p for p in bundle.developer_parts if p.startswith("## Current Task"))
    assert "继续推进" in task_part


async def test_build_main_context_without_goal(workspace, db_env):
    db, _ = db_env
    agent = SimpleNamespace(id=1, model_id=None, name="main")
    session = SimpleNamespace(id=1, worktree_path=None, model_id=None)
    project = SimpleNamespace(path=str(workspace), rules_docs=None)
    turn = SimpleNamespace(id=1)

    bundle = await build_main_context(
        db, agent=agent, session=session, project=project, turn=turn,
        user_message="最近一条消息即目标", goal=None,
    )
    # 无目标时维持现状：Current Goal 段为最近用户消息，无 Current Task 段
    goal_part = next(p for p in bundle.developer_parts if p.startswith("## Current Goal"))
    assert "最近一条消息即目标" in goal_part
    assert not any(p.startswith("## Current Task") for p in bundle.developer_parts)
