"""确定性检查命令：权限门控与执行（设计 §4.5 / §5A.1 第 8 点）。

plan-20261014-fix4：检查命令曾只认 graph 中的内置白名单，其余命令一律「未执行」——与会话的权限模式
无关，完全访问下照样被拦，检查节点必然失败。现在内置常用检查（graph.command_allowed）仍直接运行；
其余命令与终端命令走同一套统一裁决（approval_policy.decide），按会话**当前**的执行模式与权限模式
放行、发起审批或拒绝。执行器本身只负责运行与截断输出。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from app.orchestration.workflow.graph import command_allowed

logger = logging.getLogger(__name__)

OUTPUT_TAIL_CHARS = 2000
# 审批卡与「始终允许」规则按终端命令处理：用户看到的是同一张命令审批卡，规则也同源
GATE_TOOL = "terminal_exec"
GATE_AGENT_NAME = "工作流检查"


@dataclass(frozen=True)
class CommandGate:
    allowed: bool
    reason: str = ""  # 不运行时写入检查结果的原因


async def session_modes(session_id: int | None) -> tuple[str, str]:
    """会话当前的（执行模式, 权限模式）。

    独立连接读取列值：节点所在协程可能持有更早的快照，读不到用户刚在输入框切换的模式
    （与 tools/executor._refresh_approval_mode 同一口径）。读不到时取最保守的「询问审批」。
    """
    from app.orchestration.approval_policy import (
        normalize_approval_mode,
        normalize_execution_mode,
    )

    if not session_id:
        return normalize_execution_mode(None), normalize_approval_mode(None)
    try:
        from sqlalchemy import select

        from app.persistence.database import async_session_factory
        from app.persistence.models.message import Session as SessionRow

        async with async_session_factory() as db:
            row = (
                await db.execute(
                    select(SessionRow.permission_mode, SessionRow.approval_mode).where(
                        SessionRow.id == session_id
                    )
                )
            ).first()
    except Exception:  # noqa: BLE001 — 读取失败按最保守口径处理，不阻断工作流
        logger.debug("[workflow] 读取会话权限模式失败，按询问审批处理", exc_info=True)
        row = None
    if row is None:
        return normalize_execution_mode(None), normalize_approval_mode(None)
    return normalize_execution_mode(row[0]), normalize_approval_mode(row[1])


async def session_rules(session_id: int | None) -> list[Any] | None:
    """会话可见的执行策略规则（审批卡「始终允许」落库的规则同样生效）；失败按无规则处理。"""
    try:
        from app.persistence.database import async_session_factory
        from app.services import exec_policy_service

        async with async_session_factory() as db:
            return await exec_policy_service.list_rules(db, session_id=session_id)
    except Exception:  # noqa: BLE001
        logger.debug("[workflow] 读取执行策略规则失败（按无规则处理）", exc_info=True)
        return None


def decide_command(
    command: str, *, execution_mode: str, approval_mode: str, workspace: str, rules: list[Any] | None
):
    """统一裁决一条检查命令（同步，供规划期提示与运行期门控共用）。内置常用检查直接放行。"""
    from app.orchestration.approval_policy import VERDICT_ALLOW, Decision, decide

    if command_allowed(command):
        return Decision(VERDICT_ALLOW, "exec_normal", label="运行预设检查")
    ctx = SimpleNamespace(
        permission_mode=execution_mode,
        approval_mode=approval_mode,
        workspace_root=workspace or None,
    )
    return decide(GATE_TOOL, {"command": command}, ctx, risk_level="high", rules=rules)


_APPROVAL_LABELS = {"ask": "询问审批", "auto": "自动审批", "full": "完全访问"}


async def command_note_for_session(
    session_id: int | None, workspace: str
) -> Callable[[str], tuple[str, str] | None]:
    """规划期命令提示（graph.validate_plan 的 command_note）：按会话当前模式如实判定。

    完全访问下不产生任何提示；需要审批或被执行模式禁止的命令才提示，并写明是哪种模式。
    """
    from app.orchestration.approval_policy import VERDICT_ALLOW, VERDICT_DENY

    execution_mode, approval_mode = await session_modes(session_id)
    rules = await session_rules(session_id)
    mode_label = _APPROVAL_LABELS.get(approval_mode, approval_mode)

    def note(command: str) -> tuple[str, str] | None:
        decision = decide_command(
            command,
            execution_mode=execution_mode,
            approval_mode=approval_mode,
            workspace=workspace,
            rules=rules,
        )
        if decision.verdict == VERDICT_ALLOW:
            return None
        if decision.verdict == VERDICT_DENY:
            return (
                "command_blocked",
                f"命令『{command}』在当前执行模式下不能运行：{decision.reason or decision.label}",
            )
        return (
            "command_needs_approval",
            f"命令『{command}』在当前权限模式（{mode_label}）下执行前需要用户确认",
        )

    return note


async def gate_check_command(
    command: str, *, session_id: int | None, workspace: str, origin: str = ""
) -> CommandGate:
    """运行前的权限门控：放行 / 经审批卡询问用户 / 按执行模式拒绝。"""
    from app.orchestration.approval import approval_manager
    from app.orchestration.approval_policy import VERDICT_ALLOW, VERDICT_DENY

    if command_allowed(command):
        return CommandGate(True)
    execution_mode, approval_mode = await session_modes(session_id)
    rules = await session_rules(session_id)
    decision = decide_command(
        command,
        execution_mode=execution_mode,
        approval_mode=approval_mode,
        workspace=workspace,
        rules=rules,
    )
    if decision.verdict == VERDICT_ALLOW:
        return CommandGate(True)
    if decision.verdict == VERDICT_DENY:
        return CommandGate(False, f"当前模式不允许执行：{decision.reason or decision.label}")
    detail = {
        "kind": "tool_call",
        "tool": GATE_TOOL,
        "args": {"command": command, "cwd": workspace},
        "risk_level": "high",
        "session_id": session_id,
        "agent_id": None,
        "agent_name": GATE_AGENT_NAME,
        "action": decision.action,
        "action_label": decision.label,
        "risk_note": decision.risk_note,
        "execution_mode": execution_mode,
        "approval_mode": approval_mode,
        "summary": f"{origin or '工作流'}的检查步骤申请执行命令",
    }
    approved = await approval_manager.request(detail=detail)
    if approved:
        return CommandGate(True)
    return CommandGate(False, "审批未通过或已超时，未执行")


async def run_check_command(
    command: str, cwd: str, timeout_sec: float = 600.0
) -> dict[str, object]:
    """执行一条检查命令（调用方已完成权限门控）：输出截断保存，供评估角色与时间线查看。"""
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {
            "cmd": command,
            "ok": False,
            "exit_code": None,
            "output": f"超时（{int(timeout_sec)} 秒）",
        }
    except asyncio.CancelledError:
        if proc.returncode is None:
            proc.kill()
        raise
    text = (out or b"").decode("utf-8", "replace")
    return {
        "cmd": command,
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "output": text[-OUTPUT_TAIL_CHARS:],
    }
