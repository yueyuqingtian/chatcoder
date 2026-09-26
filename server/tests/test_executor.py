"""ToolExecutor 集成单测。

验证:
- 裁决为 allow 的工具直接执行不经审批
- 裁决为 ask 的工具需 ApprovalManager 同意才执行
- 被拒绝则失败
- 未知工具返回错误
- plan-75-332：执行模式边界（只读/计划）与权限模式矩阵（询问/自动/完全访问）
"""
import asyncio
from typing import Any

import pytest

from app.orchestration.approval import ApprovalManager
from app.orchestration.tools.base import Tool, ToolContext, ToolResult
from app.orchestration.tools.executor import ServerToolExecutor
from app.orchestration.tools.registry import ToolRegistry


class _FakeLowTool(Tool):
    name = "fake.low"
    risk_level = "low"
    description = "test low"

    def function_schema(self) -> dict:
        return {"type": "function", "function": {"name": self.name, "description": "", "parameters": {"type": "object", "properties": {}}}}

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult(ok=True, output="low-ok")


class _FakeHighTool(Tool):
    name = "fake.high"
    risk_level = "high"
    description = "test high"

    def function_schema(self) -> dict:
        return {"type": "function", "function": {"name": self.name, "description": "", "parameters": {"type": "object", "properties": {}}}}

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult(ok=True, output="high-ok")


@pytest.fixture
def isolated_executor(monkeypatch):
    """每个测试用独立的 ToolRegistry 与 ApprovalManager,避免全局污染。"""
    reg = ToolRegistry()
    reg.register(_FakeLowTool())
    reg.register(_FakeHighTool())

    # monkeypatch tool_registry.get 让 executor 用我们的 reg
    from app.orchestration.tools import executor as exec_mod
    monkeypatch.setattr(exec_mod, "tool_registry", reg)

    # monkeypatch approval_manager 为新实例
    fresh = ApprovalManager()
    monkeypatch.setattr(exec_mod, "approval_manager", fresh)

    return exec_mod.ServerToolExecutor(), fresh


def _ctx(tmp_path) -> ToolContext:
    return ToolContext(workspace_root=str(tmp_path), session_id=1, task_id=1, agent_id=1, agent_name="t")


class _FakeAgent:
    """足够满足 executor 内 getattr 检查的假 agent。"""
    template_id = None


@pytest.mark.asyncio
async def test_low_risk_no_approval(tmp_path, isolated_executor):
    executor, _ = isolated_executor
    r = await executor.execute(
        tool_name="fake.low", args={}, call_key="k1",
        agent=_FakeAgent(), ctx=_ctx(tmp_path),
    )
    assert r.ok is True
    assert r.output == "low-ok"


@pytest.mark.asyncio
async def test_high_risk_with_approval(tmp_path, isolated_executor):
    executor, mgr = isolated_executor

    async def approver():
        await asyncio.sleep(0.02)
        # 找到 pending 的 approval_id 并 approve
        for aid in list(mgr._pending.keys()):
            mgr.resolve(aid, True)

    asyncio.create_task(approver())
    r = await executor.execute(
        tool_name="fake.high", args={}, call_key="k2",
        agent=_FakeAgent(), ctx=_ctx(tmp_path),
        on_approval_request=lambda aid, detail: None,
    )
    assert r.ok is True
    assert r.output == "high-ok"


@pytest.mark.asyncio
async def test_high_risk_rejected(tmp_path, isolated_executor):
    executor, mgr = isolated_executor

    async def rejector():
        await asyncio.sleep(0.02)
        for aid in list(mgr._pending.keys()):
            mgr.resolve(aid, False)

    asyncio.create_task(rejector())
    r = await executor.execute(
        tool_name="fake.high", args={}, call_key="k3",
        agent=_FakeAgent(), ctx=_ctx(tmp_path),
        on_approval_request=lambda aid, detail: None,
    )
    assert r.ok is False
    assert "审批未通过" in r.error


@pytest.mark.asyncio
async def test_unknown_tool_returns_error(tmp_path, isolated_executor):
    executor, _ = isolated_executor
    r = await executor.execute(
        tool_name="no.such.tool", args={}, call_key="k4",
        agent=_FakeAgent(), ctx=_ctx(tmp_path),
    )
    assert r.ok is False
    assert "未知工具" in r.error


# ───────────── plan-75-332: 统一裁决内核（执行模式 × 权限模式） ─────────────


def _decide(tool, tool_name, args, ctx):
    """轻量调用唯一裁决点（同步，无需跑完整 executor）。"""
    from app.orchestration.approval_policy import decide
    return decide(tool_name, args, ctx, risk_level=tool.risk_level)


def test_readonly_mode_denies_write_tool(tmp_path, isolated_executor):
    """只读模式：写文件属能力边界，直接拒绝（不进入审批）。"""
    from app.orchestration.approval_policy import VERDICT_DENY
    from app.orchestration.tools.fs_write import FsWriteTool

    ctx = _ctx(tmp_path)
    ctx.permission_mode = "readonly"
    d = _decide(FsWriteTool(), "fs_write", {"path": "x.py", "content": "a"}, ctx)
    assert d.verdict == VERDICT_DENY
    assert "只读模式" in d.reason


def test_readonly_mode_denies_risky_command(tmp_path, isolated_executor):
    """只读模式：修改类命令一律拒绝。"""
    from app.orchestration.approval_policy import VERDICT_DENY
    from app.orchestration.tools.terminal import TerminalExecTool

    ctx = _ctx(tmp_path)
    ctx.permission_mode = "readonly"
    d = _decide(TerminalExecTool(), "terminal_exec", {"command": "del /f C:\\x"}, ctx)
    assert d.verdict == VERDICT_DENY
    assert "只读模式" in d.reason


def test_readonly_mode_asks_readonly_command(tmp_path, isolated_executor):
    """只读模式不拦只读命令；是否询问由权限模式决定（默认 ask）。"""
    from app.orchestration.approval_policy import VERDICT_ASK
    from app.orchestration.tools.terminal import TerminalExecTool

    ctx = _ctx(tmp_path)
    ctx.permission_mode = "readonly"
    ctx.approval_mode = "ask"
    d = _decide(TerminalExecTool(), "terminal_exec", {"command": "git status"}, ctx)
    assert d.verdict == VERDICT_ASK


def test_readonly_mode_allows_read_tool(tmp_path, isolated_executor):
    """只读模式：纯读工具三档权限模式都放行（读取不构成风险）。"""
    from app.orchestration.approval_policy import VERDICT_ALLOW
    from app.orchestration.tools.fs_read import FsReadTool

    ctx = _ctx(tmp_path)
    ctx.permission_mode = "readonly"
    ctx.approval_mode = "ask"
    d = _decide(FsReadTool(), "fs_read", {"path": "a.txt"}, ctx)
    assert d.verdict == VERDICT_ALLOW


def test_plan_mode_only_allows_plan_doc(tmp_path, isolated_executor):
    """计划模式：仅 ai/*.md 计划文档可写，其余写操作一律拒绝。"""
    from app.orchestration.approval_policy import VERDICT_ALLOW, VERDICT_DENY
    from app.orchestration.tools.fs_write import FsWriteTool

    ctx = _ctx(tmp_path)
    ctx.permission_mode = "plan"
    ctx.approval_mode = "auto"
    ok = _decide(FsWriteTool(), "fs_write", {"path": "ai/plan-x.md", "content": "# 计划"}, ctx)
    assert ok.verdict == VERDICT_ALLOW
    bad = _decide(FsWriteTool(), "fs_write", {"path": "src/a.py", "content": "x"}, ctx)
    assert bad.verdict == VERDICT_DENY
    assert "计划模式" in bad.reason


def test_full_access_mode_allows_everything(tmp_path, isolated_executor):
    """完全访问：无视风险等级，全部自动执行。"""
    from app.orchestration.approval_policy import VERDICT_ALLOW
    from app.orchestration.tools.terminal import TerminalExecTool

    ctx = _ctx(tmp_path)
    ctx.permission_mode = "agent"
    ctx.approval_mode = "full"
    d = _decide(TerminalExecTool(), "terminal_exec", {"command": "del /f C:\\x"}, ctx)
    assert d.verdict == VERDICT_ALLOW


def test_auto_mode_allows_write_inside_workspace(tmp_path, isolated_executor):
    """自动审批：工作区内新增/编辑文件属常规操作，直接放行。"""
    from app.orchestration.approval_policy import VERDICT_ALLOW
    from app.orchestration.tools.fs_write import FsWriteTool

    ctx = _ctx(tmp_path)
    ctx.permission_mode = "agent"
    ctx.approval_mode = "auto"
    d = _decide(FsWriteTool(), "fs_write", {"path": "src/a.py", "content": "x"}, ctx)
    assert d.verdict == VERDICT_ALLOW


def test_auto_mode_asks_outside_write(tmp_path, isolated_executor):
    """自动审批：写工作区外仍要问（越出项目范围）。"""
    from app.orchestration.approval_policy import VERDICT_ASK
    from app.orchestration.tools.fs_write import FsWriteTool

    ctx = _ctx(tmp_path)
    ctx.permission_mode = "agent"
    ctx.approval_mode = "auto"
    d = _decide(FsWriteTool(), "fs_write", {"path": "../outside.py", "content": "x"}, ctx)
    assert d.verdict == VERDICT_ASK


def test_auto_mode_asks_risky_command(tmp_path, isolated_executor):
    """自动审批：风险命令仍要问。"""
    from app.orchestration.approval_policy import VERDICT_ASK
    from app.orchestration.tools.terminal import TerminalExecTool

    ctx = _ctx(tmp_path)
    ctx.permission_mode = "agent"
    ctx.approval_mode = "auto"
    d = _decide(TerminalExecTool(), "terminal_exec", {"command": "del /f C:\\x"}, ctx)
    assert d.verdict == VERDICT_ASK
    assert d.risk_note  # 给出风险说明供审批卡渲染


def test_ask_mode_asks_every_write_and_command(tmp_path, isolated_executor):
    """询问审批：写文件与执行命令每次都问（只有纯读不带问题通过）。"""
    from app.orchestration.approval_policy import VERDICT_ASK
    from app.orchestration.tools.fs_write import FsWriteTool
    from app.orchestration.tools.terminal import TerminalExecTool

    ctx = _ctx(tmp_path)
    ctx.permission_mode = "agent"
    ctx.approval_mode = "ask"
    assert _decide(FsWriteTool(), "fs_write", {"path": "src/a.py", "content": "x"}, ctx).verdict == VERDICT_ASK
    assert _decide(TerminalExecTool(), "terminal_exec", {"command": "git status"}, ctx).verdict == VERDICT_ASK


@pytest.mark.asyncio
async def test_executor_denies_in_readonly_mode(tmp_path, isolated_executor):
    """只读模式经 executor 全链路：直接拒绝，不产生审批卡。"""
    executor, mgr = isolated_executor
    ctx = _ctx(tmp_path)
    ctx.permission_mode = "readonly"
    r = await executor.execute(
        tool_name="fake.high", args={}, call_key="k5",
        agent=_FakeAgent(), ctx=ctx,
    )
    assert r.ok is False
    assert "权限策略" in r.error
    assert not mgr._pending


@pytest.mark.asyncio
async def test_full_access_mode_runs_without_approval_card(tmp_path, isolated_executor):
    """完全访问经 executor 全链路：直接执行，未产生审批卡。"""
    executor, mgr = isolated_executor
    ctx = _ctx(tmp_path)
    ctx.approval_mode = "full"
    r = await executor.execute(
        tool_name="fake.high", args={}, call_key="k6",
        agent=_FakeAgent(), ctx=ctx,
    )
    assert r.ok is True
    assert r.output == "high-ok"
    assert not mgr._pending  # 未产生审批卡


@pytest.mark.asyncio
async def test_ask_mode_keeps_approval_card(tmp_path, isolated_executor):
    """询问审批（默认）经 executor 全链路：走审批卡，批准后执行。"""
    executor, mgr = isolated_executor
    ctx = _ctx(tmp_path)
    ctx.approval_mode = "ask"  # 默认：风险动作仍走审批

    async def approver():
        await asyncio.sleep(0.02)
        for aid in list(mgr._pending.keys()):
            mgr.resolve(aid, True)

    asyncio.create_task(approver())
    r = await executor.execute(
        tool_name="fake.high", args={}, call_key="k7",
        agent=_FakeAgent(), ctx=ctx,
        on_approval_request=lambda aid, detail: None,
    )
    assert r.ok is True
    assert r.output == "high-ok"


@pytest.mark.asyncio
async def test_auto_mode_allows_conventional_tool(tmp_path, isolated_executor):
    """自动审批经 executor 全链路：常规动作（low 风险读类）直接执行。"""
    executor, mgr = isolated_executor
    ctx = _ctx(tmp_path)
    ctx.approval_mode = "auto"
    r = await executor.execute(
        tool_name="fake.low", args={}, call_key="k8",
        agent=_FakeAgent(), ctx=ctx,
    )
    assert r.ok is True
    assert r.output == "low-ok"
    assert not mgr._pending


# ───────────────── plan-153-705: executor 超时读配置 ─────────────────


class _FakeSlowTool(Tool):
    """sleep 指定秒数后返回，用于验证 executor 超时。"""
    name = "fake.slow"
    risk_level = "low"
    description = "test slow"

    def function_schema(self) -> dict:
        return {"type": "function", "function": {"name": self.name, "description": "", "parameters": {"type": "object", "properties": {}}}}

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        await asyncio.sleep(float(args.get("sec", 10)))
        return ToolResult(ok=True, output="slow-done")


@pytest.mark.asyncio
async def test_executor_timeout_reads_settings(tmp_path, isolated_executor, monkeypatch):
    """executor 超时 = settings.tool_exec_timeout_sec（不再是 60s 硬编码）。"""
    from app.core.config import settings
    executor, _ = isolated_executor
    from app.orchestration.tools import executor as exec_mod
    reg = exec_mod.tool_registry
    reg.register(_FakeSlowTool())

    monkeypatch.setattr(settings, "tool_exec_timeout_sec", 1)
    r = await executor.execute(
        tool_name="fake.slow", args={"sec": 30}, call_key="k7",
        agent=_FakeAgent(), ctx=_ctx(tmp_path),
    )
    assert r.ok is False
    assert "工具执行超时(1s)" in r.error


@pytest.mark.asyncio
async def test_executor_long_tool_survives_old_60s_limit(tmp_path, isolated_executor, monkeypatch):
    """配置 600s 时，超过旧 60s 硬编码的工具不再被 executor 误杀（快速验证：sleep 2s 正常完成）。"""
    from app.core.config import settings
    executor, _ = isolated_executor
    from app.orchestration.tools import executor as exec_mod
    reg = exec_mod.tool_registry
    reg.register(_FakeSlowTool())

    monkeypatch.setattr(settings, "tool_exec_timeout_sec", 600)
    r = await executor.execute(
        tool_name="fake.slow", args={"sec": 2}, call_key="k8",
        agent=_FakeAgent(), ctx=_ctx(tmp_path),
    )
    assert r.ok is True
    assert r.output == "slow-done"
