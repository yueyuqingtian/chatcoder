"""editor_apply_diff 的 replace_all 行为测试（plan-609）。

覆盖：
- replace_all=true 且 old_text 多匹配 → 全部替换成功；
- replace_all=false（默认）多匹配 → 维持报错且文件不变；
- 唯一匹配 → 行为不变（单次替换成功）。
"""
import asyncio

from app.orchestration.tools.base import ToolContext
from app.orchestration.tools.editor import EditorApplyDiffTool


def _ctx(workspace) -> ToolContext:
    return ToolContext(
        workspace_root=str(workspace),
        session_id=1, task_id=1, agent_id=1, agent_name="tester",
    )


def test_replace_all_replaces_all_matches(tmp_path):
    """replace_all=true 且 old_text 多匹配 → 全部替换。"""
    f = tmp_path / "a.txt"
    f.write_text("xx\nxx\nxx", encoding="utf-8")
    tool = EditorApplyDiffTool()
    r = asyncio.run(tool.run(
        {"path": str(f), "old_text": "xx", "new_text": "yy", "replace_all": True},
        _ctx(tmp_path),
    ))
    assert r.ok is True
    assert f.read_text(encoding="utf-8") == "yy\nyy\nyy"


def test_replace_all_false_multiple_matches_fails(tmp_path):
    """replace_all=false（默认）且多匹配 → 报错，文件保持不变。"""
    f = tmp_path / "a.txt"
    f.write_text("xx\nxx", encoding="utf-8")
    tool = EditorApplyDiffTool()
    r = asyncio.run(tool.run(
        {"path": str(f), "old_text": "xx", "new_text": "yy"},
        _ctx(tmp_path),
    ))
    assert r.ok is False
    assert "匹配" in r.error
    assert f.read_text(encoding="utf-8") == "xx\nxx"


def test_unique_match_unchanged(tmp_path):
    """唯一匹配 → 单次替换成功，行为与 replace_all 无关。"""
    f = tmp_path / "a.txt"
    f.write_text("hello world", encoding="utf-8")
    tool = EditorApplyDiffTool()
    r = asyncio.run(tool.run(
        {"path": str(f), "old_text": "world", "new_text": "coder"},
        _ctx(tmp_path),
    ))
    assert r.ok is True
    assert f.read_text(encoding="utf-8") == "hello coder"
