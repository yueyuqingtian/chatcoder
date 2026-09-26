"""plan-75-332 R5/R6 回归：计划卡「确认执行」路径（execute_confirmed_plan）。

用户实测 bug：点击计划卡「确认执行」立即报
`任务执行出错：执行异常: name '_approval_mode' is not defined`。
根因：该函数引用了 start_turn 的局部变量 `_approval_mode`（跨函数作用域），
Python 解析不到 → 必抛 NameError，确认执行整条路径不可用。

本测试覆盖：
1. 确认执行能跑通（不再抛 NameError），返回 ok=True；
2. 会话的 approval_mode 正确透传子代理上下文（子代理与主会话同口径）；
3. 执行模式在确认执行阶段锁定为 agent（智能体语义）。

同类问题（同一轮静态扫描发现，一并回归）：
- agent_loop 浏览器工具分支的 `tool_args` → 应为同循环内的 `args`；
- goal 续跑异常日志的 `turn.id` → 应为 `turn_id_new`；
- ta3 非 kimi 思考预算的 `max_tokens` → 应为本次下发的 body["max_tokens"]。
"""
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.orchestration import engine as engine_mod
from app.persistence.database import Base
from app.persistence.models import Turn  # noqa: F401 注册全部模型到 Base.metadata
from app.persistence.models.agent import Agent
from app.persistence.models.message import Session as SessionModel
from app.persistence.models.project import Project
from app.persistence.models.turn import Turn as TurnModel


@pytest.fixture
async def db_env(tmp_path):
    """临时文件库 + 会话工厂（写引擎同源，供 _patch_turn / _patch_task 落库）。"""
    db_url = f"sqlite+aiosqlite:///{tmp_path}/confirm_plan.db"
    eng = create_async_engine(db_url)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    from app.persistence import write_engine as _we
    _we.configure(db_url, foreign_keys=False)
    factory = async_sessionmaker(eng, expire_on_commit=False)
    async with factory() as session:
        yield session, factory
    await eng.dispose()
    _we.configure(None)


class _FakeManager:
    """子代理管理器桩：只记录注册，不启动真实子代理。"""

    def __init__(self):
        self.leader_notify = None
        self.finish_notify = None

    def set_leader_notify(self, cb):
        self.leader_notify = cb

    def set_finish_notify(self, cb):
        self.finish_notify = cb

    def pending_count(self):
        return 0


async def _seed(db, workspace, approval_mode: str):
    project = Project(name="p", path=str(workspace))
    db.add(project)
    await db.flush()
    session = SessionModel(
        project_id=project.id, title="确认执行", approval_mode=approval_mode,
        permission_mode="plan",
    )
    db.add(session)
    await db.flush()
    turn = TurnModel(session_id=session.id, status="awaiting_confirmation")
    db.add(turn)
    await db.flush()
    db.add(Agent(kind="main", name="主代理"))
    await db.commit()
    return turn.id


def _patch_engine(monkeypatch, captured: dict, loop_result=None):
    """把确认执行路径的外部依赖替换为桩（只留 db / 服务层真实逻辑）。"""

    async def fake_build(*_a, **_kw):
        return SimpleNamespace(
            reply_language="zh", reply_language_source="session",
            developer_parts=[], rules_anchor="", instruction="",
            to_messages=lambda: [],
        )

    async def fake_loop(*_a, **kw):
        captured.update(kw.get("subagent_context") or {})
        return loop_result or SimpleNamespace(
            text="done", kind="message", artifact_ids=[], error=None,
        )

    async def _noop(*_a, **_kw):
        return None

    monkeypatch.setattr(engine_mod, "build_main_context", fake_build)
    monkeypatch.setattr(engine_mod, "run_agent_loop", fake_loop)
    monkeypatch.setattr(engine_mod, "broadcast", _noop)
    monkeypatch.setattr(engine_mod, "broadcast_turn_updated", _noop)
    monkeypatch.setattr(engine_mod, "broadcast_session_completed", _noop)
    monkeypatch.setattr(engine_mod, "_flush_turn_buffer", _noop)
    monkeypatch.setattr(engine_mod, "_spawn_memory_extract", _noop)
    monkeypatch.setattr(engine_mod, "_maybe_run_checkpoint_gc", _noop)
    monkeypatch.setattr(engine_mod, "get_subagent_manager", lambda _sid: _FakeManager())
    # peek_subagent_manager 在 finally 里是函数内 import，需 patch 源模块属性
    monkeypatch.setattr("app.orchestration.subagent.peek_subagent_manager", lambda _sid: None)


@pytest.mark.asyncio
@pytest.mark.parametrize("approval_mode", ["full", "ask", "auto"])
async def test_confirm_plan_runs_and_passes_approval_mode(db_env, monkeypatch, tmp_path, approval_mode):
    """确认执行跑通，且权限模式原样透传子代理上下文（修复前必抛 NameError）。"""
    db, _factory = db_env
    turn_id = await _seed(db, tmp_path, approval_mode)

    captured: dict = {}
    _patch_engine(monkeypatch, captured)

    res = await engine_mod.execute_confirmed_plan(db, turn_id=turn_id)

    assert res["ok"] is True, res
    assert captured["approval_mode"] == approval_mode, captured
    # 确认执行阶段主代理即智能体语义（执行模式锁定 agent）
    assert captured["permission_mode"] == "agent"


@pytest.mark.asyncio
async def test_confirm_plan_failure_path_reports_original_error(db_env, monkeypatch, tmp_path):
    """运行期异常走 catch 分支：错误可诊断（不得被日志里的 NameError 二次覆盖）。"""
    db, _factory = db_env
    turn_id = await _seed(db, tmp_path, "full")

    captured: dict = {}
    _patch_engine(monkeypatch, captured)

    async def boom(*_a, **_kw):
        raise RuntimeError("模型返回异常")

    monkeypatch.setattr(engine_mod, "run_agent_loop", boom)

    res = await engine_mod.execute_confirmed_plan(db, turn_id=turn_id)

    assert res["ok"] is False
    # 原始原因必须透出（而非 "_approval_mode' is not defined" 这类自身缺陷）
    assert "模型返回异常" in res["error"]
