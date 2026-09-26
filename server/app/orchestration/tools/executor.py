"""v0.3: ToolExecutor — agent loop 与具体执行位置解耦。

- 抽象 ToolExecutor:execute(tool_call, agent, ctx) -> ToolResult
- 本期实现 ServerToolExecutor:服务端进程内执行 + 审批门。
- v0.5 可加 ClientToolExecutor:WS 下发 tool_call.request 等客户端回结果。

plan-75-332：审批门改造为**单一裁决点**。改造前本文件内嵌五段硬编码判定
（沙箱 read-only → 工具钩子 approval_precheck → 权限模式 plan/readonly →
模式白名单 → exec_policy 规则 → danger-full-access 免审），并只对
`risk_level != "low"` 的工具走审批——导致"读工作区外文件"这类 low 风险操作
必须在工具内部（outside_access）另开一个审批入口。

现在：每个工具调用都交给 `approval_policy.decide()` 裁决一次，
返回 allow（直接执行）/ ask（弹审批卡）/ deny（拒绝，仅执行模式边界与用户规则）。
工具自身不再做任何拦截，也不再自行决定免审。
"""
import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from app._diag import log_tool_error  # v36: 审批/执行异常诊断日志
from app.orchestration.approval import approval_manager
from app.orchestration.approval_policy import (
    VERDICT_ASK,
    VERDICT_DENY,
    decide,
    normalize_approval_mode,
    normalize_execution_mode,
)
from app.orchestration.tools.base import ToolContext, ToolResult
from app.orchestration.tools.registry import tool_registry

if TYPE_CHECKING:
    from app.persistence.models.agent import Agent

logger = logging.getLogger(__name__)


# plan-248-1258 M3.1: 写盘类工具名 → 变更文件路径参数名（多个时按 args.edits[].path）
_WRITE_TOOLS = {
    "fs_write": ("path", "file_path", "filepath"),
    "editor_apply_diff": ("path", "file_path", "filepath"),
    "multi_file_edit": (),  # 路径在 edits[].path
    "apply_patch": ("path", "file_path", "filepath"),
}


def _notify_symbol_index(tool_name: str, args: dict, ctx: ToolContext) -> None:
    """写盘成功 → 通知符号索引失效该文件（已开启索引的工作区会在下一轮自动增量）。

    同步、无异常外抛：索引维护绝不能影响工具结果返回。
    """
    workspace = getattr(ctx, "workspace_root", None)
    if not workspace:
        return
    try:
        if tool_name not in _WRITE_TOOLS:
            return
        from app.services import symbol_index_manager as sim

        paths: list[str] = []
        if tool_name == "multi_file_edit":
            edits = args.get("edits")
            if isinstance(edits, list):
                paths = [str(e.get("path")) for e in edits
                         if isinstance(e, dict) and e.get("path")]
        else:
            for key in _WRITE_TOOLS[tool_name]:
                v = args.get(key)
                if isinstance(v, str) and v.strip():
                    paths.append(v.strip())
                    break
        if not paths:
            sim.notify_file_changed(workspace, None)
            return
        for p in paths:
            sim.notify_file_changed(workspace, p)
    except Exception:  # noqa: BLE001
        logger.debug("[symbols] 写盘钩子通知失败(非阻塞)", exc_info=True)


class ToolExecutor(ABC):
    @abstractmethod
    async def execute(
        self,
        *,
        tool_name: str,
        args: dict[str, Any],
        call_key: str,
        agent: "Agent",
        ctx: ToolContext,
        on_approval_request: Any = None,
    ) -> ToolResult:
        """执行一次工具调用。"""


class ServerToolExecutor(ToolExecutor):
    """服务端进程内执行。

    流程:
    1. 工具存在性校验
    2. approval_policy.decide() 裁决（allow / ask / deny）
    3. 执行工具
    """

    async def _load_rules(self, ctx: ToolContext) -> list | None:
        """取本次会话可见的 exec_policy 规则（失败按无规则处理，不阻断执行）。"""
        if getattr(ctx, "db", None) is None:
            return None
        try:
            from app.services import exec_policy_service

            return await exec_policy_service.list_rules(ctx.db, session_id=ctx.session_id)
        except Exception:
            logger.warning("exec_policy 规则查询异常(按无规则处理)", exc_info=True)
            return None

    async def execute(
        self,
        *,
        tool_name: str,
        args: dict[str, Any],
        call_key: str,
        agent: "Agent",
        ctx: ToolContext,
        on_approval_request: Any = None,
    ) -> ToolResult:
        tool = tool_registry.get(tool_name)
        if tool is None:
            return ToolResult(ok=False, output="", error=f"未知工具: {tool_name}")

        # agent 白名单校验已由调用方（agent_loop）控制，此处只做统一权限裁决

        # ── 统一裁决（plan-75-332）：所有工具调用都过一次策略，不再按 risk_level 分流 ──
        rules = await self._load_rules(ctx)
        decision = decide(
            tool_name, args or {}, ctx,
            risk_level=tool.risk_level, rules=rules,
        )

        if decision.verdict == VERDICT_DENY:
            logger.info(
                "[tool.gate] 拒绝 tool=%s action=%s pm=%s reason=%s",
                tool_name, decision.action,
                getattr(ctx, "permission_mode", "-"), decision.reason,
            )
            return ToolResult(
                ok=False, output="",
                error=f"[权限策略] {decision.reason}",
                data={"denied": True, "reason": decision.reason, "action": decision.action},
            )

        if decision.verdict == VERDICT_ASK:
            approval_id = approval_manager.new_id()
            detail = {
                "call_key": call_key,
                "tool": tool_name,
                "args": args,
                "risk_level": tool.risk_level,
                "agent_id": ctx.agent_id,
                "agent_name": ctx.agent_name,
                "task_id": ctx.task_id,
                "session_id": ctx.session_id,
                # plan-75-332: 审批卡展示用的动作短语与风险说明（前端直接渲染，不再拼工具名）
                "kind": "tool_call",
                "action": decision.action,
                "action_label": decision.label,
                "risk_note": decision.risk_note,
                "execution_mode": normalize_execution_mode(getattr(ctx, "permission_mode", None)),
                "approval_mode": normalize_approval_mode(getattr(ctx, "approval_mode", None)),
                # plan-75-332: 本 turn 的思考深度——审批卡「解释」默认沿用（未在设置中指定专用档位时）
                "reasoning_effort": getattr(ctx, "reasoning_effort", None),
                "summary": f"{ctx.agent_name} 申请{decision.label}",
            }
            # plan-230-1144: 不再注册全局单例回调——多会话并发时后注册者会覆盖前者，
            # 导致 A 会话的审批广播进 B 会话。approval_manager.request 现按
            # detail.session_id 精确路由到发起会话的 WS 通道。
            # （on_approval_request 保留形参以兼容既有调用方，其内部 emitter 已无用）
            _ = on_approval_request  # noqa: F841 兼容保留
            approved = await approval_manager.request(approval_id=approval_id, detail=detail)
            if not approved:
                return ToolResult(
                    ok=False, output="",
                    error=f"审批未通过/已超时（{decision.label}）",
                    data={"approved": False, "approval_id": approval_id},
                )
        else:
            logger.info(
                "[tool.gate] 放行 tool=%s action=%s pm=%s am=%s",
                tool_name, decision.action,
                getattr(ctx, "permission_mode", "-"), getattr(ctx, "approval_mode", "-"),
            )

        # 执行
        try:
            # v4.8.2: 工具执行加超时，防止同步 I/O 挂起
            # v1.0 (plan-153-705): 60s 硬编码 → settings.tool_exec_timeout_sec（默认 600s），
            # 与 agent_loop 外层超时同源；长编译/测试/安装不再被内层提前误杀。
            # plan-238-1188: ask_user_question 彻底不设超时——
            # approval.py 的 question 无超时只覆盖"审批等待"环节，工具 run() 本身
            # 仍被本层 wait_for(审批超时+30s) 杀掉，表现为"提问还是会超时"。
            # 现改为无限等待（用户取消 turn 会连带取消本协程，不存在悬挂）。
            import asyncio

            from app.core.config import settings as _settings
            if tool_name == "ask_user_question":
                result = await tool.run(args, ctx)
            else:
                _timeout = float(_settings.tool_exec_timeout_sec)
                result = await asyncio.wait_for(tool.run(args, ctx), timeout=_timeout)
            # plan-248-1258 M3.1: 写盘类工具成功后通知符号索引（失效该文件 + 标记待增量），
            # 使已开启索引的工作区在文件变更后自动更新（需求：修改文件时自动更新）。
            if result.ok:
                _notify_symbol_index(tool_name, args, ctx)
            return result
        except asyncio.TimeoutError:
            logger.error("工具执行超时 %s", tool_name)
            return ToolResult(ok=False, output="", error=f"工具执行超时({_timeout:.0f}s): {tool_name}")
        except Exception as e:
            # v36: 记录完整堆栈（含异常链），并保留异常类型——
            # 仅凭 "工具异常: <msg>" 无法定位抛错文件与行号。
            log_tool_error(
                turn_id=getattr(ctx, "task_id", None), step=None,
                tool_name=tool_name, call_key=call_key, exc=e,
                args=args, phase="run",
            )
            return ToolResult(ok=False, output="", error=f"工具异常: {type(e).__name__}: {e}")


# 全局单例(本期服务端执行)
tool_executor = ServerToolExecutor()
