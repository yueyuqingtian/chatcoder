"""ToolExecutor 集成单测。

验证:
- low risk 工具直接执行不经审批
- high risk 工具需 ApprovalManager 同意才执行
- high risk 工具被拒绝则失败
- 未知工具返回错误
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


# ───────────────── v3.0 (plan-88): 沙箱模式审批门决策 ─────────────────


@pytest.mark.asyncio
async def test_readonly_sandbox_denies_write_tool(tmp_path, isolated_executor):
    executor, _ = isolated_executor
    ctx = _ctx(tmp_path)
    ctx.sandbox_mode = "read-only"
    skip, reason = await executor._precheck_approval(
        _FakeHighTool(), "fs_write", {"path": "x.py"}, ctx, {})
    assert skip is False
    assert "只读沙箱" in reason


@pytest.mark.asyncio
async def test_readonly_sandbox_denies_risky_command(tmp_path, isolated_executor):
    executor, _ = isolated_executor
    from app.orchestration.tools.terminal import TerminalExecTool
    ctx = _ctx(tmp_path)
    ctx.sandbox_mode = "read-only"
    skip, reason = await executor._precheck_approval(
        TerminalExecTool(), "terminal_exec", {"command": "del /f C:\\x"}, ctx, {})
    assert skip is False
    assert "只读沙箱" in reason


@pytest.mark.asyncio
async def test_readonly_sandbox_allows_safe_command(tmp_path, isolated_executor):
    executor, _ = isolated_executor
    from app.orchestration.tools.terminal import TerminalExecTool
    ctx = _ctx(tmp_path)
    ctx.sandbox_mode = "read-only"
    skip, _ = await executor._precheck_approval(
        TerminalExecTool(), "terminal_exec", {"command": "git status"}, ctx, {})
    assert skip is True


@pytest.mark.asyncio
async def test_full_access_sandbox_skips_approval(tmp_path, isolated_executor):
    executor, _ = isolated_executor
    ctx = _ctx(tmp_path)
    ctx.sandbox_mode = "danger-full-access"
    skip, reason = await executor._precheck_approval(_FakeHighTool(), "fake.high", {}, ctx, {})
    assert skip is True
    assert reason == ""


@pytest.mark.asyncio
async def test_full_access_runs_without_approval_card(tmp_path, isolated_executor):
    executor, mgr = isolated_executor
    ctx = _ctx(tmp_path)
    ctx.sandbox_mode = "danger-full-access"
    r = await executor.execute(
        tool_name="fake.high", args={}, call_key="k5",
        agent=_FakeAgent(), ctx=ctx,
    )
    assert r.ok is True
    assert r.output == "high-ok"
    assert not mgr._pending  # 未产生审批卡


@pytest.mark.asyncio
async def test_full_access_respects_force_approval_list(tmp_path, isolated_executor, monkeypatch):
    """v32 (plan-89): danger-full-access 与"始终需要审批的工具"冲突修复——
    显式 force_approval_tools 列表内的工具即使全访问沙箱仍保留审批卡。"""
    executor, mgr = isolated_executor
    ctx = _ctx(tmp_path)
    ctx.sandbox_mode = "danger-full-access"
    from app.core.config import settings
    monkeypatch.setattr(settings, "force_approval_tools", "fake.high")
    skip, reason = await executor._precheck_approval(_FakeHighTool(), "fake.high", {}, ctx, {})
    assert skip is False
    assert reason == ""  # 保留审批（不 deny 不 skip），走 approval_manager.request


@pytest.mark.asyncio
async def test_workspace_write_keeps_approval(tmp_path, isolated_executor):
    executor, mgr = isolated_executor
    ctx = _ctx(tmp_path)
    ctx.sandbox_mode = "workspace-write"  # 默认：高危工具仍走审批

    async def approver():
        await asyncio.sleep(0.02)
        for aid in list(mgr._pending.keys()):
            mgr.resolve(aid, True)

    asyncio.create_task(approver())
    r = await executor.execute(
        tool_name="fake.high", args={}, call_key="k6",
        agent=_FakeAgent(), ctx=ctx,
        on_approval_request=lambda aid, detail: None,
    )
    assert r.ok is True
    assert r.output == "high-ok"
