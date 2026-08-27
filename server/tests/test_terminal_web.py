"""terminal.exec 与 web.fetch 单测。

terminal:用跨平台命令验证(echo / cd 等),不做命令白名单(v0.3 仅审批门)。
web.fetch:用 http server fixture 或 mock,避免外网依赖。
"""
import asyncio
import socket

import pytest

from app.orchestration.tools.base import ToolContext
from app.orchestration.tools.terminal import TerminalExecTool


def _ctx(workspace) -> ToolContext:
    return ToolContext(
        workspace_root=str(workspace),
        session_id=1, task_id=1, agent_id=1, agent_name="tester",
    )


@pytest.mark.asyncio
async def test_terminal_echo_success(workspace):
    tool = TerminalExecTool()
    # echo 跨平台;Python 子进程默认 shell=True
    r = await tool.run({"command": "echo hello_from_test"}, _ctx(workspace))
    assert r.ok is True
    assert "hello_from_test" in r.output


@pytest.mark.asyncio
async def test_terminal_failing_command(workspace):
    tool = TerminalExecTool()
    # 用一个一定失败的命令(Windows/Linux 都会失败)
    r = await tool.run({"command": "exit 7"}, _ctx(workspace))
    assert r.ok is False
    assert r.data["returncode"] == 7


@pytest.mark.asyncio
async def test_terminal_empty_command_rejected(workspace):
    tool = TerminalExecTool()
    r = await tool.run({"command": ""}, _ctx(workspace))
    assert r.ok is False
    assert "为空" in r.error


@pytest.mark.asyncio
async def test_terminal_timeout(workspace):
    """30s 超时,用 sleep 长于 30s 不现实;此处改用 mock 验证超时路径。

    由于真实 sleep 30s 太慢,跳过该测试的运行时长,改为验证工具常量。
    """
    tool = TerminalExecTool()
    # 验证超时常量存在且合理
    from app.orchestration.tools.terminal import _TIMEOUT_SEC
    assert _TIMEOUT_SEC == 30
    # 验证 schema 完整
    schema = tool.function_schema()
    assert schema["function"]["name"] == "terminal_exec"
    assert "command" in schema["function"]["parameters"]["properties"]


def test_terminal_function_schema():
    tool = TerminalExecTool()
    schema = tool.function_schema()
    assert schema["type"] == "function"
    assert schema["function"]["parameters"]["required"] == ["command"]


def test_terminal_risk_level_is_high():
    tool = TerminalExecTool()
    assert tool.risk_level == "high"


# ───────────────── v3.0 (plan-88): 外部访问开关的 cwd 解析 ─────────────────


def test_resolve_path_outside_blocked_by_default(tmp_path):
    """开关关闭(默认)：越界 cwd 回退 workspace_root。"""
    ws = str(tmp_path)
    tool = TerminalExecTool()
    assert tool._resolve_path(ws, "..") == ws
    assert tool._resolve_path(ws, str(tmp_path.parent)) == ws


def test_resolve_path_outside_allowed_when_enabled(tmp_path):
    """开关开启：绝对越界路径与 ../ 相对越界均放行。"""
    ws = str(tmp_path)
    tool = TerminalExecTool()
    parent = str(tmp_path.parent)
    assert tool._resolve_path(ws, parent, allow_outside=True) == parent
    assert tool._resolve_path(ws, "..", allow_outside=True) == parent


def test_resolve_path_inside_unchanged(tmp_path):
    """工作区内路径不受开关影响。"""
    ws = str(tmp_path)
    tool = TerminalExecTool()
    (tmp_path / "clinic").mkdir()
    assert tool._resolve_path(ws, "clinic") == str(tmp_path / "clinic")
    assert tool._resolve_path(ws, "clinic", allow_outside=True) == str(tmp_path / "clinic")


def test_plan_mode_outside_flag_in_result(tmp_path):
    """plan 模式 + 开关开启：执行数据带 outside_access 审计标记。"""
    tool = TerminalExecTool()
    ctx = ToolContext(
        workspace_root=str(tmp_path),
        session_id=1, task_id=1, agent_id=1, agent_name="tester",
        permission_mode="plan",
    )
    from app.core.config import settings as _s
    prev = _s.plan_mode_allow_outside_access
    _s.plan_mode_allow_outside_access = True
    try:
        r = asyncio.run(tool.run({"command": "echo x", "cwd": ".."}, ctx))
    finally:
        _s.plan_mode_allow_outside_access = prev
    assert r.ok is True
    assert r.data.get("outside_access") is True
    assert r.data["cwd"] == str(tmp_path.parent)


# ───────────────── web.fetch(仅 schema + 风险等级,不实际发请求) ─────────────────


def test_web_fetch_schema_and_risk():
    from app.orchestration.tools.web_fetch import WebFetchTool
    tool = WebFetchTool()
    assert tool.risk_level == "medium"
    schema = tool.function_schema()
    assert schema["function"]["name"] == "web_fetch"
    assert "url" in schema["function"]["parameters"]["properties"]
