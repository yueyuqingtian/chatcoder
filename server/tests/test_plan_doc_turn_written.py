"""plan-85-379 回归测试：计划文档归属以「本 turn 实际写盘记录」为准。

背景（用户实测 Bug）：
- session 83 / turn 377 的规划轮，模型把方案文档命名为
  `ai/chatcoder-plan-75-334.md`（沿用历史文档编号，未按约定用 83-377）；
- 旧逻辑按文件名首段数字判定归属（75 != 83）→ 被串会话防护误判为
  「别的会话的文档」而丢弃 → 明明文档已写出，却报「计划文档未生成」且不弹计划卡。
修复：turn 结束时先查本 turn 的写盘记录（rollback_writes），命中
`ai/chatcoder-plan*.md` 即作为解析候选，优先于文件名编号推断；
无写盘记录时旧行为完全不变（防护仍生效、新鲜度约束仍生效）。
"""
import time

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.orchestration.engine import (
    _find_plan_document,
    _resolve_plan_doc,
    _turn_written_plan_docs,
)
from app.persistence import models  # noqa: F401  （确保全部模型注册进 Base.metadata）
from app.persistence.database import Base
from app.persistence.models.rollback import RollbackWrite


@pytest.fixture
async def plan_db(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path}/plan_doc.db"
    _engine = create_async_engine(db_url)
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(_engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await _engine.dispose()


# ── 纯函数层：命名错位场景（复现 + 修复 + 防护不回退）──

def test_bug_repro_mismatched_name_dropped_without_written(workspace):
    """复现根因：模型命名错位 + 无写盘记录 → 旧逻辑把本会话文档当别的会话丢弃。"""
    (workspace / "ai").mkdir()
    (workspace / "ai" / "chatcoder-plan-75-334.md").write_text(
        "# 性能优化方案\n正文", encoding="utf-8")

    path = _find_plan_document(str(workspace), 83, turn_id=377)
    assert path is None  # 首段 75 != 83 → 被误判为会话 75 的文档


def test_written_record_recovers_mismatched_document(workspace):
    """修复：本 turn 写盘记录命中 → 命名错位也能解析到文档并返回内容。"""
    (workspace / "ai").mkdir()
    doc = workspace / "ai" / "chatcoder-plan-75-334.md"
    doc.write_text("# 性能优化方案\n正文", encoding="utf-8")

    path = _find_plan_document(str(workspace), 83, turn_id=377, turn_written=[doc])
    assert path is not None
    assert path.name == "chatcoder-plan-75-334.md"

    p2, source = _resolve_plan_doc(str(workspace), 83, "message", turn_id=377,
                                   turn_written=[doc])
    assert p2 is not None
    assert "性能优化方案" in source


def test_written_record_still_respects_freshness(workspace):
    """写盘记录同样受「本轮更新」约束：陈旧候选不会被采用（防复用旧文档）。"""
    (workspace / "ai").mkdir()
    doc = workspace / "ai" / "chatcoder-plan-75-334.md"
    doc.write_text("# 上一轮遗留", encoding="utf-8")

    future = time.time() + 3600  # since_ts 晚于文件 mtime → 非本轮产出
    path = _find_plan_document(str(workspace), 83, turn_id=377, since_ts=future,
                               turn_written=[doc])
    assert path is None


def test_other_session_doc_still_guarded(workspace):
    """无本 turn 写盘记录时，别的会话的文档仍被丢弃——串会话防护不变。"""
    (workspace / "ai").mkdir()
    (workspace / "ai" / "chatcoder-plan-9-1.md").write_text("# 别的会话", encoding="utf-8")

    path = _find_plan_document(str(workspace), 83, turn_id=377)
    assert path is None


def test_convention_name_still_takes_priority(workspace):
    """约定名（<sid>-<tid>）仍为第一优先：同名同时存在时先命中它。"""
    (workspace / "ai").mkdir()
    (workspace / "ai" / "chatcoder-plan-83-377.md").write_text("# 约定名文档", encoding="utf-8")
    other = workspace / "ai" / "chatcoder-plan-75-334.md"
    other.write_text("# 命名错位文档", encoding="utf-8")

    path = _find_plan_document(str(workspace), 83, turn_id=377, turn_written=[other])
    assert path is not None
    assert path.name == "chatcoder-plan-83-377.md"


# ── DB 层：写盘记录筛选 ──

async def test_turn_written_plan_docs_returns_only_current_turn_plan_files(plan_db, workspace):
    """只返回「本 turn 写入 + ai/chatcoder-plan*.md + 文件真实存在」的候选。"""
    (workspace / "ai").mkdir()
    (workspace / "ai" / "chatcoder-plan-75-334.md").write_text("# 本 turn 文档", encoding="utf-8")
    (workspace / "ai" / "chatcoder-plan-99-1.md").write_text("# 其他 turn 文档", encoding="utf-8")
    (workspace / "server").mkdir()
    (workspace / "server" / "app.py").write_text("x", encoding="utf-8")

    plan_db.add(RollbackWrite(session_id=83, turn_id=377, tool="fs_write",
                              path="ai/chatcoder-plan-75-334.md"))
    plan_db.add(RollbackWrite(session_id=83, turn_id=377, tool="fs_write",
                              path="server/app.py"))  # 非计划文档 → 剔除
    plan_db.add(RollbackWrite(session_id=83, turn_id=400, tool="fs_write",
                              path="ai/chatcoder-plan-99-1.md"))  # 其他 turn → 剔除
    await plan_db.commit()

    out = await _turn_written_plan_docs(plan_db, 83, 377, str(workspace))
    assert [p.name for p in out] == ["chatcoder-plan-75-334.md"]


async def test_turn_written_plan_docs_skips_missing_file(plan_db, workspace):
    """写盘记录指向的文件已被删除 → 不返回（解析时回退旧逻辑）。"""
    (workspace / "ai").mkdir()
    plan_db.add(RollbackWrite(session_id=83, turn_id=377, tool="fs_write",
                              path="ai/chatcoder-plan-75-334.md"))
    await plan_db.commit()

    out = await _turn_written_plan_docs(plan_db, 83, 377, str(workspace))
    assert out == []


async def test_turn_written_plan_docs_backslash_path_normalized(plan_db, workspace):
    """写盘记录为反斜杠路径（Windows）时同样能命中。"""
    (workspace / "ai").mkdir()
    doc = workspace / "ai" / "chatcoder-plan-75-334.md"
    doc.write_text("# 方案", encoding="utf-8")
    plan_db.add(RollbackWrite(session_id=83, turn_id=377, tool="fs_write",
                              path="ai\\chatcoder-plan-75-334.md"))
    await plan_db.commit()

    out = await _turn_written_plan_docs(plan_db, 83, 377, str(workspace))
    assert [p.name for p in out] == ["chatcoder-plan-75-334.md"]
