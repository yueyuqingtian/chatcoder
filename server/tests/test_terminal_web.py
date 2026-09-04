"""terminal.exec 与 web.fetch 单测。

terminal:用跨平台命令验证(echo / cd 等),不做命令白名单(v0.3 仅审批门)。
web.fetch:用 http server fixture 或 mock,避免外网依赖。
plan-153-705: waitForCompletion=false 后台执行闭环 + timeout 参数用例。
"""
import asyncio

import pytest

from app.orchestration.tools.base import ToolContext
from app.orchestration.tools.bg_process import (
    TerminalBgKillTool, TerminalBgStatusTool, bg_process_registry,
)
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
async def test_terminal_timeout_param_kills_long_command(workspace):
    """plan-153-705: timeout 参数生效——短超时 + 长命令 → 超时报错且提示后台模式。"""
    tool = TerminalExecTool()
    r = await tool.run(
        {"command": 'python -c "import time; time.sleep(60)"', "timeout": 5},
        _ctx(workspace),
    )
    assert r.ok is False
    assert "超时(>5s)" in r.error
    assert "waitForCompletion" in r.error  # 错误信息引导转后台


@pytest.mark.asyncio
async def test_terminal_timeout_clamped_to_config(workspace, monkeypatch):
    """timeout 钳制：超过 settings.tool_exec_timeout_sec 的值被压到上限。"""
    from app.core.config import settings
    from app.orchestration.tools.terminal import _parse_timeout
    monkeypatch.setattr(settings, "tool_exec_timeout_sec", 600)
    assert _parse_timeout(99999) == 600
    assert _parse_timeout(1) == 5          # 下限 5
    assert _parse_timeout(None) == 120     # 默认 120
    assert _parse_timeout("60") == 60      # 字符串数字容错


@pytest.mark.asyncio
async def test_terminal_background_lifecycle(workspace):
    """plan-153-705: waitForCompletion=false 后台闭环——启动→查状态→终止→已退出。"""
    tool = TerminalExecTool()
    r = await tool.run(
        {"command": 'python -c "print(\'bg_started\', flush=True); import time; time.sleep(120)"',
         "waitForCompletion": False},
        _ctx(workspace),
    )
    assert r.ok is True
    assert r.data.get("background") is True
    shell_id = r.data["shell_id"]
    assert shell_id.startswith("bg_")

    # 状态查询：运行中 + 日志含启动标记
    status_tool = TerminalBgStatusTool()
    s1 = await status_tool.run({"shell_id": shell_id}, _ctx(workspace))
    assert s1.ok is True
    assert s1.data["running"] is True
    # 输出收集是异步的，轮询等待日志出现
    for _ in range(50):
        s1 = await status_tool.run({"shell_id": shell_id}, _ctx(workspace))
        if "bg_started" in s1.data["log"]:
            break
        await asyncio.sleep(0.1)
    assert "bg_started" in s1.data["log"]
    assert s1.data["next_offset"] > 0

    # 增量读取：从 next_offset 起无新日志
    s2 = await status_tool.run(
        {"shell_id": shell_id, "offset": s1.data["next_offset"]}, _ctx(workspace),
    )
    assert s2.ok is True
    assert s2.data["log"] == ""

    # 终止
    kill_tool = TerminalBgKillTool()
    k = await kill_tool.run({"shell_id": shell_id}, _ctx(workspace))
    assert k.ok is True

    # 终止后状态：已退出（轮询等 _collect 任务记录退出码）
    for _ in range(50):
        s3 = await status_tool.run({"shell_id": shell_id}, _ctx(workspace))
        if not s3.data["running"]:
            break
        await asyncio.sleep(0.1)
    assert s3.data["running"] is False
    assert s3.data["returncode"] is not None


@pytest.mark.asyncio
async def test_terminal_background_natural_exit(workspace):
    """plan-153-705: 后台进程自然退出后 status 返回退出码与尾部日志。"""
    tool = TerminalExecTool()
    r = await tool.run(
        {"command": 'python -c "print(\'bg_done\', flush=True)"',
         "waitForCompletion": "false"},   # 字符串 "false" 容错
        _ctx(workspace),
    )
    assert r.ok is True
    shell_id = r.data["shell_id"]

    status_tool = TerminalBgStatusTool()
    for _ in range(50):
        s = await status_tool.run({"shell_id": shell_id}, _ctx(workspace))
        if not s.data["running"]:
            break
        await asyncio.sleep(0.1)
    assert s.ok is True
    assert s.data["running"] is False
    assert s.data["returncode"] == 0
    assert "bg_done" in s.data["log"]


@pytest.mark.asyncio
async def test_terminal_bg_tools_unknown_shell_id(workspace):
    """未知 shell_id → 明确报错（服务重启后记录失效场景）。"""
    status_tool = TerminalBgStatusTool()
    s = await status_tool.run({"shell_id": "bg_nope"}, _ctx(workspace))
    assert s.ok is False
    assert "未知 shell_id" in s.error

    kill_tool = TerminalBgKillTool()
    k = await kill_tool.run({"shell_id": "bg_nope"}, _ctx(workspace))
    assert k.ok is False
    assert "未知 shell_id" in k.error


def test_terminal_function_schema():
    tool = TerminalExecTool()
    schema = tool.function_schema()
    assert schema["type"] == "function"
    assert schema["function"]["parameters"]["required"] == ["command"]
    # plan-153-705: 新参数暴露
    props = schema["function"]["parameters"]["properties"]
    assert "waitForCompletion" in props
    assert "timeout" in props
    assert "cwd" in props


def test_terminal_risk_level_is_high():
    tool = TerminalExecTool()
    assert tool.risk_level == "high"


def test_bg_tools_risk_level_low():
    """后台查询/终止为 low 风险（免审批，加快 AI 迭代）。"""
    assert TerminalBgStatusTool().risk_level == "low"
    assert TerminalBgKillTool().risk_level == "low"


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
