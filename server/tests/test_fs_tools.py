"""文件系统工具单测(fs.read / fs.list / fs.write / editor.apply_diff)。

直接调 Tool.run(),不走审批(审批门在 executor 层)。
"""
import pytest

from app.orchestration.tools.base import ToolContext
from app.orchestration.tools.editor import EditorApplyDiffTool
from app.orchestration.tools.fs_list import FsListTool
from app.orchestration.tools.fs_read import FsReadTool
from app.orchestration.tools.fs_write import FsWriteTool


def _ctx(workspace) -> ToolContext:
    return ToolContext(
        workspace_root=str(workspace),
        session_id=1, task_id=1, agent_id=1, agent_name="tester",
    )


# ───────────────── fs.read ─────────────────


@pytest.mark.asyncio
async def test_fs_read_existing_file(workspace):
    f = workspace / "hello.txt"
    f.write_text("hello world", encoding="utf-8")
    tool = FsReadTool()
    r = await tool.run({"path": "hello.txt"}, _ctx(workspace))
    assert r.ok is True
    assert "hello world" in r.output


@pytest.mark.asyncio
async def test_fs_read_missing_file(workspace):
    tool = FsReadTool()
    r = await tool.run({"path": "nope.txt"}, _ctx(workspace))
    assert r.ok is False
    assert "不存在" in r.error


@pytest.mark.asyncio
async def test_fs_read_traversal_rejected(workspace):
    tool = FsReadTool()
    r = await tool.run({"path": "../../../etc/passwd"}, _ctx(workspace))
    assert r.ok is False
    assert "越界" in r.error


@pytest.mark.asyncio
async def test_fs_read_truncates_large_file(workspace):
    f = workspace / "big.txt"
    # v1.1: v15 起 tool_output_chars_read=16000，需超过该上限才能触发截断
    f.write_text("X" * 20000, encoding="utf-8")
    tool = FsReadTool()
    r = await tool.run({"path": "big.txt"}, _ctx(workspace))
    assert r.ok is True
    assert "已截断" in r.output
    assert r.data["total_lines"] == 1


@pytest.mark.asyncio
async def test_fs_read_offset_limit_window(workspace):
    """plan-645: offset/limit 生效——从指定行开始读指定行数。"""
    f = workspace / "multi.txt"
    f.write_text("line1\nline2\nline3\nline4\nline5\n", encoding="utf-8")
    tool = FsReadTool()
    r = await tool.run({"path": "multi.txt", "offset": 2, "limit": 2}, _ctx(workspace))
    assert r.ok is True
    assert "2: line2" in r.output
    assert "3: line3" in r.output
    assert "1: line1" not in r.output
    assert "4: line4" not in r.output
    assert r.data["shown_lines"] == "2-3"


@pytest.mark.asyncio
async def test_fs_read_offset_beyond_eof(workspace):
    """plan-645: offset 超出文件总行数时不崩溃，返回空窗口。"""
    f = workspace / "multi.txt"
    f.write_text("line1\nline2\nline3\n", encoding="utf-8")
    tool = FsReadTool()
    r = await tool.run({"path": "multi.txt", "offset": 100, "limit": 2}, _ctx(workspace))
    assert r.ok is True
    assert "1: line1" not in r.output


@pytest.mark.asyncio
async def test_fs_read_numeric_string_args(workspace):
    """plan-645: 模型输出字符串数字（"2"）时容错，不抛 TypeError。"""
    f = workspace / "multi.txt"
    f.write_text("line1\nline2\nline3\n", encoding="utf-8")
    tool = FsReadTool()
    r = await tool.run({"path": "multi.txt", "offset": "2", "limit": "1"}, _ctx(workspace))
    assert r.ok is True
    assert "2: line2" in r.output
    assert "1: line1" not in r.output


@pytest.mark.asyncio
async def test_fs_read_limit_clamped(workspace):
    """plan-645: limit 超上限钳到 400，传 0/负数钳到 1。"""
    f = workspace / "many.txt"
    f.write_text("\n".join(f"line{i}" for i in range(1, 451)) + "\n", encoding="utf-8")
    tool = FsReadTool()
    r = await tool.run({"path": "many.txt", "limit": 99999}, _ctx(workspace))
    assert r.ok is True
    assert r.data["shown_lines"] == "1-400"
    r0 = await tool.run({"path": "many.txt", "limit": 0}, _ctx(workspace))
    assert r0.ok is True
    assert r0.data["shown_lines"] == "1-1"


# ───────────────── fs.list ─────────────────


@pytest.mark.asyncio
async def test_fs_list_directory(workspace):
    (workspace / "a.txt").write_text("a")
    (workspace / "sub").mkdir()
    tool = FsListTool()
    r = await tool.run({"path": ""}, _ctx(workspace))
    assert r.ok is True
    assert "[file] a.txt" in r.output
    assert "[dir] sub" in r.output


@pytest.mark.asyncio
async def test_fs_list_nonexistent(workspace):
    tool = FsListTool()
    r = await tool.run({"path": "no-such-dir"}, _ctx(workspace))
    assert r.ok is False


# ───────────────── fs.write ─────────────────


@pytest.mark.asyncio
async def test_fs_write_creates_file(workspace):
    tool = FsWriteTool()
    r = await tool.run({"path": "out/result.txt", "content": "data"}, _ctx(workspace))
    assert r.ok is True
    assert (workspace / "out" / "result.txt").read_text(encoding="utf-8") == "data"


@pytest.mark.asyncio
async def test_fs_write_traversal_rejected(workspace):
    tool = FsWriteTool()
    r = await tool.run({"path": "../../escape.txt", "content": "x"}, _ctx(workspace))
    assert r.ok is False
    assert "越界" in r.error


@pytest.mark.asyncio
async def test_fs_write_overwrites_existing(workspace):
    f = workspace / "f.txt"
    f.write_text("old", encoding="utf-8")
    tool = FsWriteTool()
    r = await tool.run({"path": "f.txt", "content": "new"}, _ctx(workspace))
    assert r.ok is True
    assert f.read_text(encoding="utf-8") == "new"


# ───────────────── editor.apply_diff ─────────────────


@pytest.mark.asyncio
async def test_editor_apply_diff_unique_match(workspace):
    f = workspace / "code.py"
    f.write_text("def foo():\n    return 1\n", encoding="utf-8")
    tool = EditorApplyDiffTool()
    r = await tool.run({
        "path": "code.py",
        "old_text": "return 1",
        "new_text": "return 42",
    }, _ctx(workspace))
    assert r.ok is True
    assert "return 42" in f.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_editor_apply_diff_no_match(workspace):
    f = workspace / "code.py"
    f.write_text("def foo():\n    pass\n", encoding="utf-8")
    tool = EditorApplyDiffTool()
    r = await tool.run({
        "path": "code.py",
        "old_text": "nonexistent",
        "new_text": "x",
    }, _ctx(workspace))
    assert r.ok is False
    assert "未匹配" in r.error


@pytest.mark.asyncio
async def test_editor_apply_diff_multiple_matches_rejected(workspace):
    f = workspace / "code.py"
    f.write_text("a\na\n", encoding="utf-8")
    tool = EditorApplyDiffTool()
    r = await tool.run({
        "path": "code.py",
        "old_text": "a",
        "new_text": "b",
    }, _ctx(workspace))
    assert r.ok is False
    assert "匹配 2 处" in r.error
