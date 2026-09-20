"""计划模式与回滚增强测试（v26）。

覆盖本次修复：
- _read_plan_document：支持 AI 写出带时间戳的计划文档（chatcoder-plan-*.md），
  按修改时间取最新，避免"执行时读不到最新方案"。
- checkpoint：文件级检查点文件名嵌入原相对路径可反查；无精确写盘记录时
  按 checkpoint 兜底恢复（不依赖 git，非 git 仓库可用）。
- normalize_workspace_path：绝对/相对路径归一化与越界剔除。
"""
import os
from pathlib import Path

from app.orchestration.engine import _read_plan_document
from app.persistence.models.rollback import TurnSnapshot
from app.services.rollback_service import (
    _restore_checkpoints_for_turn,
    checkpoint_file,
    normalize_workspace_path,
)


# ── 计划文档读取（时间戳变体）──


def test_plan_document_prefers_bound_session_file(workspace):
    (workspace / "ai").mkdir()
    (workspace / "ai" / "chatcoder-plan.md").write_text("old generic", encoding="utf-8")
    (workspace / "ai" / "chatcoder-plan-7.md").write_text("bound session 7", encoding="utf-8")
    assert _read_plan_document(str(workspace), session_id=7) == "bound session 7"


def test_plan_document_falls_back_to_latest_timestamped(workspace):
    (workspace / "ai").mkdir()
    older = workspace / "ai" / "chatcoder-plan-7-20260801_100000.md"
    newer = workspace / "ai" / "chatcoder-plan-7-20260802_100000.md"
    older.write_text("old plan", encoding="utf-8")
    newer.write_text("new plan", encoding="utf-8")
    # 强制 older 的 mtime 更早（同目录同秒写盘时 mtime 可能一致）
    old_ts = newer.stat().st_mtime - 200
    os.utime(older, (old_ts, old_ts))
    assert _read_plan_document(str(workspace), session_id=7) == "new plan"


def test_plan_document_timestamped_without_session_id(workspace):
    (workspace / "ai").mkdir()
    (workspace / "ai" / "chatcoder-plan-20260801_100000.md").write_text("no sid plan", encoding="utf-8")
    assert _read_plan_document(str(workspace), session_id=7) == "no sid plan"


def test_plan_document_missing_returns_empty(workspace):
    assert _read_plan_document(str(workspace), session_id=7) == ""


# ── 串会话防护：不得读到别的会话的方案文档 ──


def test_plan_document_ignores_other_session_document(workspace):
    """别的会话写出的方案文档不得被本会话命中（串会话修复）。

    这是"当前会话拿到另一个会话的计划"的问题根源：旧实现扫描
    ai/chatcoder-plan*.md 且按 mtime 取最新，不区分归属。"""
    (workspace / "ai").mkdir()
    # 别的会话（id=9）的文档，且 mtime 更新（旧实现会优先命中它）
    other = workspace / "ai" / "chatcoder-plan-9-20260801_100000.md"
    other.write_text("other session plan", encoding="utf-8")
    mine = workspace / "ai" / "chatcoder-plan-7-20260801_100000.md"
    mine.write_text("my plan", encoding="utf-8")
    old_ts = other.stat().st_mtime - 200
    os.utime(other, (old_ts, old_ts))  # 让别的会话文档更新
    assert _read_plan_document(str(workspace), session_id=7) == "my plan"


def test_plan_document_only_other_session_returns_empty(workspace):
    """工作区里只有别的会话的方案文档时必须返回空，而不是借用它。"""
    (workspace / "ai").mkdir()
    (workspace / "ai" / "chatcoder-plan-9.md").write_text("other session", encoding="utf-8")
    assert _read_plan_document(str(workspace), session_id=7) == ""


def test_plan_doc_ownership_classifier():
    """归属判定单元：本会话 / 别的会话 / 无法归属（纯时间戳）三类。"""
    from app.orchestration.engine import _plan_doc_belongs_to_other

    # 本会话（含带时间戳、带 turn id 的变体）→ 不属于别人
    assert _plan_doc_belongs_to_other("chatcoder-plan-7.md", 7) is False
    assert _plan_doc_belongs_to_other("chatcoder-plan-7-123.md", 7) is False
    assert _plan_doc_belongs_to_other("chatcoder-plan-7-20260801_100000.md", 7) is False
    # 别的会话 → 属于别人（丢弃）
    assert _plan_doc_belongs_to_other("chatcoder-plan-9.md", 7) is True
    assert _plan_doc_belongs_to_other("chatcoder-plan-9-123.md", 7) is True
    assert _plan_doc_belongs_to_other("chatcoder-plan-70-1.md", 7) is True
    # 纯时间戳命名：无法归属 → 保留（不判给别人）
    assert _plan_doc_belongs_to_other("chatcoder-plan-20260801_100000.md", 7) is False


# ── checkpoint 文件名与恢复 ──


def test_checkpoint_file_name_embeds_rel_path(workspace):
    target = workspace / "src" / "main.py"
    target.parent.mkdir()
    target.write_text("v1", encoding="utf-8")
    ckpt = checkpoint_file(str(workspace), str(target))
    assert ckpt is not None
    name = Path(ckpt).name
    assert "src_main.py" in name  # 相对路径嵌入文件名，可人工反查


async def test_restore_checkpoints_for_turn(workspace):
    target = workspace / "src" / "main.py"
    target.parent.mkdir()
    target.write_text("v1", encoding="utf-8")
    ckpt = checkpoint_file(str(workspace), str(target))
    target.write_text("v2", encoding="utf-8")  # AI 改写后
    snap = TurnSnapshot(session_id=1, turn_id=1,
                        file_list=[{"ckpt": ckpt, "path": "src/main.py"}], new_files=[])
    result = await _restore_checkpoints_for_turn(str(workspace), snap)
    assert result["restored"] == 1
    assert target.read_text(encoding="utf-8") == "v1"  # 恢复为写盘前内容


async def test_restore_deletes_new_files(workspace):
    new_rel = "gen/out.txt"
    (workspace / "gen").mkdir()
    (workspace / new_rel).write_text("x", encoding="utf-8")
    snap = TurnSnapshot(session_id=1, turn_id=1, file_list=[], new_files=[new_rel])
    result = await _restore_checkpoints_for_turn(str(workspace), snap)
    assert result["deleted"] == 1
    assert not (workspace / new_rel).exists()


async def test_restore_skips_old_string_entries(workspace):
    # 旧版本 file_list 存的是裸字符串（无路径映射），跳过不崩溃
    snap = TurnSnapshot(session_id=1, turn_id=1,
                        file_list=["20260801_000000_main.py"], new_files=[])
    result = await _restore_checkpoints_for_turn(str(workspace), snap)
    assert result["restored"] == 0 and result["failed"] == 0


# ── 路径归一化 ──


def test_normalize_workspace_path(workspace):
    assert normalize_workspace_path(str(workspace), "src/main.py") == "src/main.py"
    abs_p = workspace / "src" / "abs.py"
    abs_p.parent.mkdir()
    abs_p.write_text("x", encoding="utf-8")
    assert normalize_workspace_path(str(workspace), str(abs_p)) == "src/abs.py"


def test_normalize_rejects_escape(workspace):
    outside = Path(workspace).parent / "outside.txt"
    assert normalize_workspace_path(str(workspace), str(outside)) is None
    assert normalize_workspace_path(str(workspace), "../escape.txt") is None
    assert normalize_workspace_path(str(workspace), "") is None
