"""plan-644: 计划模式多轮迭代不丢计划--状态生命周期 + Plan History 注入测试。

覆盖：
- _stamp_plan_doc：头部状态行插入 / 幂等 / 路径边界（仅 ai/ 下 chatcoder-plan*.md）。
- _read_plan_document_exact：精确路径读取 / 越界与非法名拒绝（回退信号）。
- _supersede_stale_proposed：新一轮方案出现只取代 proposed，done/confirmed 不动。
- _collect_plan_history：多轮需求全集注入（状态标注 / 用户需求 / 预算降级）。
- TurnOut：plan_doc_path / plan_status 字段透出。
"""
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.orchestration.context_manager import _collect_plan_history
from app.orchestration.engine import (
    _read_plan_document_exact, _stamp_plan_doc, _supersede_stale_proposed,
)
from app.gateway.schemas import TurnOut
from app.persistence.database import Base
from app.persistence.models import Turn  # noqa: F401 注册全部模型到 Base.metadata
from app.persistence.models.turn import Turn as TurnModel


# ───────────────── 文件系统级：_stamp_plan_doc ─────────────────


def _mk_plan(ws, name="chatcoder-plan-1-1.md", text="# 方案A\n步骤..."):
    (ws / "ai").mkdir(exist_ok=True)
    p = ws / "ai" / name
    p.write_text(text, encoding="utf-8")
    return p


def test_stamp_plan_doc_prepends_line(workspace):
    p = _mk_plan(workspace)
    _stamp_plan_doc(str(workspace), "ai/chatcoder-plan-1-1.md", "proposed")
    content = p.read_text(encoding="utf-8")
    assert content.startswith("<!-- plan-status: proposed @ ")
    assert "# 方案A" in content  # 原文保留


def test_stamp_plan_doc_idempotent_same_status(workspace):
    p = _mk_plan(workspace)
    _stamp_plan_doc(str(workspace), "ai/chatcoder-plan-1-1.md", "proposed")
    first = p.read_text(encoding="utf-8")
    _stamp_plan_doc(str(workspace), "ai/chatcoder-plan-1-1.md", "proposed")
    assert p.read_text(encoding="utf-8") == first  # 同状态重复标注幂等


def test_stamp_plan_doc_status_chain(workspace):
    """状态流转在头部累积轨迹（proposed -> confirmed -> done）。"""
    p = _mk_plan(workspace)
    _stamp_plan_doc(str(workspace), "ai/chatcoder-plan-1-1.md", "proposed")
    _stamp_plan_doc(str(workspace), "ai/chatcoder-plan-1-1.md", "confirmed")
    _stamp_plan_doc(str(workspace), "ai/chatcoder-plan-1-1.md", "done")
    lines = p.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("<!-- plan-status: done @ ")
    assert any(ln.startswith("<!-- plan-status: confirmed @ ") for ln in lines[:3])
    assert any(ln.startswith("<!-- plan-status: proposed @ ") for ln in lines[:3])


def test_stamp_plan_doc_rejects_out_of_scope(workspace):
    """仅 ai/ 下 chatcoder-plan*.md 可标注：普通文件与目录外路径静默跳过。"""
    _mk_plan(workspace)
    (workspace / "notes.md").write_text("x", encoding="utf-8")
    before = (workspace / "notes.md").read_text(encoding="utf-8")
    _stamp_plan_doc(str(workspace), "notes.md", "proposed")  # 非 ai/ 目录
    assert (workspace / "notes.md").read_text(encoding="utf-8") == before
    _stamp_plan_doc(str(workspace), "../outside.md", "proposed")  # 越界
    _stamp_plan_doc(str(workspace), "ai/other.md", "proposed")  # 非 plan 文档名
    _stamp_plan_doc(str(workspace), None, "proposed")  # 空路径
    assert (workspace / "ai" / "chatcoder-plan-1-1.md").read_text(encoding="utf-8").startswith("# 方案A")


# ───────────────── 文件系统级：_read_plan_document_exact ─────────────────


def test_read_plan_document_exact_hit(workspace):
    _mk_plan(workspace, text="# 完整方案\n内容" * 100)
    out = _read_plan_document_exact(str(workspace), "ai/chatcoder-plan-1-1.md")
    assert out.startswith("# 完整方案")


def test_read_plan_document_exact_truncated_to_16000(workspace):
    _mk_plan(workspace, text="x" * 30000)
    out = _read_plan_document_exact(str(workspace), "ai/chatcoder-plan-1-1.md")
    assert len(out) == 16000


def test_read_plan_document_exact_rejects_bad_path(workspace):
    _mk_plan(workspace)
    (workspace / "secret.txt").write_text("top", encoding="utf-8")
    # 越界 / 非计划文档名 / 不存在 -> 空串（调用方回退 mtime 逻辑）
    assert _read_plan_document_exact(str(workspace), "../../etc/passwd") == ""
    assert _read_plan_document_exact(str(workspace), "secret.txt") == ""
    assert _read_plan_document_exact(str(workspace), "ai/chatcoder-plan-9-9.md") == ""
    assert _read_plan_document_exact(str(workspace), None) == ""


# ───────────────── DB 级：异步内存库 ─────────────────


@pytest.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


async def _mk_plan_turn(db, session_id, turn_id, plan_status, doc_path=None):
    t = TurnModel(id=turn_id, session_id=session_id, status="awaiting_confirmation",
                  plan_doc_path=doc_path or f"ai/chatcoder-plan-{session_id}-{turn_id}.md",
                  plan_status=plan_status)
    db.add(t)
    await db.flush()
    return t


async def test_supersede_stale_proposed_only_targets_proposed(db, workspace):
    """新一轮方案出现：仅 proposed 被标记 superseded；done/confirmed 不受影响。"""
    _mk_plan(workspace, "chatcoder-plan-1-10.md", "# 方案1")
    _mk_plan(workspace, "chatcoder-plan-1-20.md", "# 方案2")
    _mk_plan(workspace, "chatcoder-plan-1-30.md", "# 方案3")
    await _mk_plan_turn(db, 1, 10, "proposed", "ai/chatcoder-plan-1-10.md")
    await _mk_plan_turn(db, 1, 20, "done", "ai/chatcoder-plan-1-20.md")
    await _mk_plan_turn(db, 1, 30, "confirmed", "ai/chatcoder-plan-1-30.md")
    await db.commit()

    await _supersede_stale_proposed(db, 1, 40, str(workspace))
    await db.commit()

    t10 = await db.get(TurnModel, 10)
    t20 = await db.get(TurnModel, 20)
    t30 = await db.get(TurnModel, 30)
    assert t10.plan_status == "superseded"
    assert t20.plan_status == "done"       # 已完成不重复取代
    assert t30.plan_status == "confirmed"  # 执行中不动
    # 文档头部写入取代元数据（含取代来源 turn）
    head = (workspace / "ai" / "chatcoder-plan-1-10.md").read_text(encoding="utf-8").splitlines()[0]
    assert head.startswith("<!-- plan-status: superseded @ ")
    assert "by turn 40" in head


async def test_collect_plan_history_full_and_budget(db, workspace, monkeypatch):
    """Plan History：状态标注 + 用户需求 + 最近轮全文；预算超限降级早轮正文。"""
    from app.core.config import settings as app_settings
    from app.persistence.models.task import Task

    # 三个计划轮：1=done（已完成，仅标题）、2=superseded（全文）、3=proposed（全文）
    # turn2/3 正文加长至 >500 字符：预算 1200 时最新轮全文保留、更早轮降级
    _mk_plan(workspace, "chatcoder-plan-5-1.md", "# 方案一\n已完成的需求")
    _mk_plan(workspace, "chatcoder-plan-5-2.md", "# 方案二\n被取代的需求：优化A\n优化B\n" + "详" * 600)
    _mk_plan(workspace, "chatcoder-plan-5-3.md", "# 方案三\n新增需求：特性C\n" + "细" * 600)
    for tid, status in [(1, "done"), (2, "superseded"), (3, "proposed")]:
        t = TurnModel(id=tid, session_id=5, status="completed", plan_doc_path=f"ai/chatcoder-plan-5-{tid}.md", plan_status=status)
        db.add(t)
        db.add(Task(session_id=5, turn_id=tid, kind="request", title=f"请求{tid}", description=f"第{tid}轮需求"))
    await db.commit()

    sess = SimpleNamespace(id=5)
    history = await _collect_plan_history(db, sess, str(workspace))
    assert "### Turn 1 [done]" in history
    assert "已执行完成" in history          # 状态语义标注
    assert "请求2" in history and "第2轮需求" in history  # 用户需求注入
    assert "特性C" in history               # proposed 轮全文
    assert "优化B" in history               # superseded 轮全文

    # 小预算：从最早轮次开始降级（正文截断提示），最新轮全文保留
    monkeypatch.setattr(app_settings, "plan_history_inject_chars", 1200, raising=False)
    history_small = await _collect_plan_history(db, sess, str(workspace))
    assert "特性C" in history_small          # 最新轮保全文
    assert "注入预算截断" in history_small    # 早轮降级提示

    # 无计划轮 -> 空串
    empty = await _collect_plan_history(db, SimpleNamespace(id=6), str(workspace))
    assert empty == ""


def test_turn_out_exposes_plan_fields():
    """TurnOut schema 透出 plan_doc_path / plan_status（前端恢复数据源）。"""
    out = TurnOut(id=1, session_id=1, status="completed",
                  plan_doc_path="ai/chatcoder-plan-1-1.md", plan_status="done")
    assert out.plan_doc_path == "ai/chatcoder-plan-1-1.md"
    assert out.plan_status == "done"
    # 字段可缺省（旧客户端兼容）
    legacy = TurnOut(id=2, session_id=1, status="running")
    assert legacy.plan_doc_path is None and legacy.plan_status is None


def test_config_plan_history_inject_chars():
    """plan-644: Plan History 注入预算配置存在且默认 8000。"""
    from app.core.config import settings
    assert hasattr(settings, "plan_history_inject_chars")
    assert settings.plan_history_inject_chars == 8000
