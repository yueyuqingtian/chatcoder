"""fs_read 窗口口径、未变化短路与工具错误类别的单测。"""
from types import SimpleNamespace

import pytest

from app.orchestration.tools.base import ERROR_CODES, ToolContext, ToolResult
from app.orchestration.tools.fs_read import FsReadTool
from app.orchestration.tools.fs_write import FsWriteTool
from app.orchestration.tools.read_cache import UNCHANGED_STUB, ReadWindowCache, norm_path, read_cache_for


def _ctx(workspace, cache=None) -> ToolContext:
    return ToolContext(
        workspace_root=str(workspace),
        session_id=1, task_id=1, agent_id=1, agent_name="tester",
        read_cache=cache,
    )


def _msg(role, content="", tool_call_id=""):
    return SimpleNamespace(role=role, content=content, tool_call_id=tool_call_id)


# ───────────────── 窗口与页脚 ─────────────────


@pytest.mark.asyncio
async def test_default_window_footer_gives_exact_next_offset(workspace):
    f = workspace / "long.txt"
    f.write_text("\n".join(f"row {i}" for i in range(1, 1501)), encoding="utf-8")
    r = await FsReadTool().run({"path": "long.txt"}, _ctx(workspace))
    assert r.ok is True
    assert "显示第 1-600 行" in r.output
    assert "offset=601" in r.output
    assert r.data["shown_lines"] == "1-600"


@pytest.mark.asyncio
async def test_limit_is_capped_at_2000_lines(workspace):
    f = workspace / "many.txt"
    f.write_text("\n".join(f"l{i}" for i in range(1, 2501)), encoding="utf-8")
    r = await FsReadTool().run({"path": "many.txt", "limit": 5000}, _ctx(workspace))
    assert r.data["shown_lines"] == "1-2000"
    assert "offset=2001" in r.output


@pytest.mark.asyncio
async def test_byte_budget_cuts_window_and_says_so(workspace):
    f = workspace / "wide.txt"
    f.write_text("\n".join("x" * 200 for _ in range(1000)), encoding="utf-8")
    r = await FsReadTool().run({"path": "wide.txt", "limit": 600}, _ctx(workspace))
    assert len(r.output.encode("utf-8")) < 32 * 1024 + 2048  # 窗口 + 页脚与头部余量
    assert "单次输出上限" in r.output
    assert "offset=" in r.output


@pytest.mark.asyncio
async def test_overlong_line_is_truncated_with_original_length(workspace):
    f = workspace / "huge_line.txt"
    f.write_text("Y" * 5000, encoding="utf-8")
    r = await FsReadTool().run({"path": "huge_line.txt"}, _ctx(workspace))
    assert "行截断，原长 5000 字符" in r.output
    assert r.data["total_lines"] == 1


@pytest.mark.asyncio
async def test_offset_past_end_is_reported_not_crashed(workspace):
    f = workspace / "short.txt"
    f.write_text("a\nb\n", encoding="utf-8")
    r = await FsReadTool().run({"path": "short.txt", "offset": 99}, _ctx(workspace))
    assert r.ok is True
    assert "已超出文件末尾" in r.output


@pytest.mark.asyncio
async def test_missing_file_carries_not_found_code(workspace):
    r = await FsReadTool().run({"path": "nope.txt"}, _ctx(workspace))
    assert r.ok is False
    assert r.error_code == "NOT_FOUND"


# ───────────────── 未变化短路 ─────────────────


@pytest.mark.asyncio
async def test_unchanged_reread_returns_stub_when_earlier_result_visible(workspace):
    f = workspace / "a.txt"
    f.write_text("hello\nworld\n", encoding="utf-8")
    messages: list = []
    cache = read_cache_for(1, lambda: messages)
    tool = FsReadTool()

    first = await tool.run({"path": "a.txt"}, _ctx(workspace, cache))
    assert first.ok and "hello" in first.output
    # 模拟 agent_loop：工具结果进入上下文后登记
    messages.append(_msg("tool", first.output, tool_call_id="call-1"))
    cache.record(tuple(first.data["read_key"]), tuple(first.data["fingerprint"]), "call-1")

    second = await tool.run({"path": "a.txt"}, _ctx(workspace, cache))
    assert second.output == UNCHANGED_STUB
    assert second.data.get("unchanged") is True


@pytest.mark.asyncio
async def test_stub_not_returned_after_result_is_folded(workspace):
    f = workspace / "b.txt"
    f.write_text("content\n", encoding="utf-8")
    messages: list = []
    cache = read_cache_for(2, lambda: messages)
    tool = FsReadTool()
    first = await tool.run({"path": "b.txt"}, _ctx(workspace, cache))
    messages.append(_msg("tool", "[内容已折叠: 工具 fs_read 原始输出 900 字符]", tool_call_id="call-2"))
    cache.record(tuple(first.data["read_key"]), tuple(first.data["fingerprint"]), "call-2")

    again = await tool.run({"path": "b.txt"}, _ctx(workspace, cache))
    assert again.output != UNCHANGED_STUB
    assert "content" in again.output


@pytest.mark.asyncio
async def test_changed_file_is_read_again_in_full(workspace):
    f = workspace / "c.txt"
    f.write_text("v1\n", encoding="utf-8")
    messages: list = []
    cache = read_cache_for(3, lambda: messages)
    tool = FsReadTool()
    first = await tool.run({"path": "c.txt"}, _ctx(workspace, cache))
    messages.append(_msg("tool", first.output, tool_call_id="call-3"))
    cache.record(tuple(first.data["read_key"]), tuple(first.data["fingerprint"]), "call-3")

    f.write_text("v2 changed content\n", encoding="utf-8")
    again = await tool.run({"path": "c.txt"}, _ctx(workspace, cache))
    assert again.output != UNCHANGED_STUB
    assert "v2 changed content" in again.output


def test_cache_respects_entry_limit():
    cache = ReadWindowCache()
    for i in range(2100):
        cache.record(("p", i, 600), (1, i), f"id-{i}")
    assert len(cache._seen) <= 2000


# ───────────────── 先读后写 ─────────────────


@pytest.mark.asyncio
async def test_overwrite_unread_existing_file_is_refused(workspace):
    (workspace / "keep.txt").write_text("original\n", encoding="utf-8")
    messages: list = []
    cache = read_cache_for(4, lambda: messages)
    # 本会话已有其它读取记录（否则“先读后写”不生效）
    other = workspace / "other.txt"
    other.write_text("x\n", encoding="utf-8")
    await FsReadTool().run({"path": "other.txt"}, _ctx(workspace, cache))
    cache.record(
        (norm_path(workspace / "other.txt"), 1, 600),
        (1, 2), "call-other",
    )
    r = await FsWriteTool().run({"path": "keep.txt", "content": "replaced\n"}, _ctx(workspace, cache))
    assert r.ok is False
    assert r.error_code == "STALE_READ"
    assert (workspace / "keep.txt").read_text(encoding="utf-8") == "original\n"


# ───────────────── 工具错误类别 ─────────────────


def test_failure_text_includes_code_and_hint_for_failures_only():
    bad = ToolResult(ok=False, output="", error="文件不存在: x", error_code="NOT_FOUND")
    assert "[错误类别: NOT_FOUND]" in bad.failure_text()
    assert "fs_list" in bad.failure_text()  # NOT_FOUND 的指引引导先确认路径
    ok = ToolResult(ok=True, output="fine", error="")
    assert ok.failure_text() == ""


def test_unlabelled_failure_is_classified_as_exec_failed():
    r = ToolResult(ok=False, output="", error="boom")
    r.classify_if_missing()
    assert r.error_code == "EXEC_FAILED"
    assert r.error_code in ERROR_CODES


def test_unknown_code_falls_back_to_exec_failed_hint():
    r = ToolResult(ok=False, output="", error="boom", error_code="WHATEVER")
    assert "[错误类别: EXEC_FAILED]" in r.failure_hint_line()
