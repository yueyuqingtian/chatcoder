"""退化工具调用过滤与 fs_write 缺参防御测试（v35）。

背景（真实案例 msg 46408/46410）：模型输出总结正文时误发
fs_write(path="工具写文件**")、fs_write(path="写入才算完成")——
参数为正文片段且无 content 键，执行后 0 字节文件被真实创建，
并在消息流渲染出误导性工具卡片（用户误以为"文字被当成工具调用"）。

覆盖：
- _filter_degenerate_tool_calls：写工具缺必需参数 → 丢弃；带全参数 → 保留；
  只读工具正文片段 → 丢弃（v34 行为回归保护）。
- FsWriteTool.run：content 键缺失 → 报错不写盘；显式空串 → 正常写空文件。
"""
import pytest

from app.orchestration.agent_loop import _filter_degenerate_tool_calls
from app.orchestration.tools.base import ToolContext
from app.orchestration.tools.fs_write import FsWriteTool


class _Resp:
    """模拟 provider ChatResponse 的最小接口。"""

    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


def _ctx(workspace) -> ToolContext:
    return ToolContext(
        workspace_root=str(workspace),
        session_id=1, task_id=1, agent_id=1, agent_name="tester",
    )


# ───────────────── 过滤器：写工具缺必需参数 ─────────────────


def test_filter_drops_fs_write_missing_content():
    """fs_write 缺 content 键（schema required）→ 丢弃，不执行不渲染。"""
    resp = _Resp(
        content="修复完成。只有通过 fs_write 把内容写入文件才算完成。",
        tool_calls=[{"id": "c1", "name": "fs_write", "arguments": {"path": "写入才算完成"}}],
    )
    _filter_degenerate_tool_calls(resp)
    assert resp.tool_calls == []


def test_filter_drops_fs_write_missing_content_among_valid():
    """同响应混合正常与退化调用时只丢弃退化者。"""
    resp = _Resp(
        tool_calls=[
            {"id": "c1", "name": "fs_write", "arguments": {"path": "写入才算完成"}},
            {"id": "c2", "name": "fs_write", "arguments": {"path": "a.txt", "content": "hi"}},
            {"id": "c3", "name": "fs_read", "arguments": {"path": "a.txt"}},
        ],
    )
    _filter_degenerate_tool_calls(resp)
    assert [tc["id"] for tc in resp.tool_calls] == ["c2", "c3"]


def test_filter_keeps_fs_write_with_empty_string_content():
    """显式 content=""（创建空文件）是合法意图，不错杀。"""
    resp = _Resp(
        tool_calls=[{"id": "c1", "name": "fs_write", "arguments": {"path": ".gitkeep", "content": ""}}],
    )
    _filter_degenerate_tool_calls(resp)
    assert len(resp.tool_calls) == 1


def test_filter_keeps_other_tools_without_path():
    """terminal_exec 等无 content 语义的工具不受影响。"""
    resp = _Resp(
        tool_calls=[{"id": "c1", "name": "terminal_exec", "arguments": {"command": "dir"}}],
    )
    _filter_degenerate_tool_calls(resp)
    assert len(resp.tool_calls) == 1


# ───────────────── 过滤器：只读工具正文片段（v34 回归） ─────────────────


def test_filter_drops_read_tool_verbatim_fragment():
    resp = _Resp(
        content="视角(工作区根解析相对路径): 文件不存在 → 即原失败根因",
        tool_calls=[{"id": "c1", "name": "fs_read",
                     "arguments": {"path": "视角(工作区根解析相对路径): 文件不存在"}}],
    )
    _filter_degenerate_tool_calls(resp)
    assert resp.tool_calls == []


def test_filter_keeps_read_tool_real_path():
    """真实路径（带分隔符/扩展名）不命中正文片段判据。"""
    resp = _Resp(
        content="先读取 server/app/main.py 了解结构",
        tool_calls=[{"id": "c1", "name": "fs_read", "arguments": {"path": "server/app/main.py"}}],
    )
    _filter_degenerate_tool_calls(resp)
    assert len(resp.tool_calls) == 1


# ───────────────── fs_write 执行层：缺 content 报错 ─────────────────


@pytest.mark.asyncio
async def test_fs_write_missing_content_rejected(workspace):
    """content 键缺失 → 报错，不创建文件（纵深防御第二层）。"""
    tool = FsWriteTool()
    r = await tool.run({"path": "accident.md"}, _ctx(workspace))
    assert r.ok is False
    assert "content" in r.error
    assert not (workspace / "accident.md").exists()


@pytest.mark.asyncio
async def test_fs_write_explicit_empty_content_writes(workspace):
    """显式 content="" 是合法的创建空文件意图。"""
    tool = FsWriteTool()
    r = await tool.run({"path": "keep.txt", "content": ""}, _ctx(workspace))
    assert r.ok is True
    assert (workspace / "keep.txt").read_text(encoding="utf-8") == ""
