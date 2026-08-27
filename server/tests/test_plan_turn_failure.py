"""plan turn 未生成文档不弹计划卡测试（plan-88 任务 F）。

覆盖：
- _resolve_plan_doc：文档命中且内容非空 → (path, source)；
  文档缺失 / 内容为空 → (None, "")。
- 判定语义：out.kind != "message"（AI 异常结束）同样视为未生成。
"""
from pathlib import Path

from app.orchestration.engine import _resolve_plan_doc


def test_resolve_plan_doc_hit(workspace):
    (workspace / "ai").mkdir()
    (workspace / "ai" / "chatcoder-plan-7.md").write_text("# 方案\n步骤...", encoding="utf-8")
    path, source = _resolve_plan_doc(str(workspace), 7, "message")
    assert path is not None
    assert source.startswith("# 方案")


def test_resolve_plan_doc_missing(workspace):
    path, source = _resolve_plan_doc(str(workspace), 7, "message")
    assert path is None and source == ""


def test_resolve_plan_doc_empty_content(workspace):
    (workspace / "ai").mkdir()
    (workspace / "ai" / "chatcoder-plan-7.md").write_text("   \n", encoding="utf-8")
    path, source = _resolve_plan_doc(str(workspace), 7, "message")
    assert path is None and source == ""  # 空内容视为未生成


def test_resolve_plan_doc_whitespace_only(workspace):
    (workspace / "ai").mkdir()
    (workspace / "ai" / "chatcoder-plan-7.md").write_text("\n\n", encoding="utf-8")
    path, source = _resolve_plan_doc(str(workspace), 7, "message")
    assert path is None and source == ""


def test_resolve_plan_doc_relative_path_usable(workspace):
    (workspace / "ai").mkdir()
    (workspace / "ai" / "chatcoder-plan-7-20260801_100000.md").write_text("plan body", encoding="utf-8")
    path, source = _resolve_plan_doc(str(workspace), 7, "message")
    assert path is not None
    rel = path.relative_to(Path(str(workspace)).resolve())
    assert rel.as_posix().startswith("ai/chatcoder-plan")
