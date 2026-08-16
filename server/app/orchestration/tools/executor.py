"""v0.3: ToolExecutor — agent loop 与具体执行位置解耦。

- 抽象 ToolExecutor:execute(tool_call, agent, ctx) -> ToolResult
- 本期实现 ServerToolExecutor:服务端进程内执行 + 审批门。
- v0.5 可加 ClientToolExecutor:WS 下发 tool_call.request 等客户端回结果。
"""
import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from app.orchestration.approval import approval_manager
from app.orchestration.tools.base import ToolContext, ToolResult
from app.orchestration.tools.registry import tool_registry

if TYPE_CHECKING:
    from app.persistence.models.agent import Agent

logger = logging.getLogger(__name__)


def _is_plan_doc_path(ctx: ToolContext, args: dict) -> bool:
    """规划模式放行判定：写入目标为 workspace/ai/ 下的 .md 计划文档。"""
    raw = args.get("path") or args.get("file_path") or ""
    if not isinstance(raw, str) or not raw.strip():
        return False
    try:
        from pathlib import Path
        from app.orchestration.tools.safe_path import safe_resolve, safe_resolve_parent
        root = Path(ctx.workspace_root).resolve()
        target = safe_resolve(str(root), raw) or safe_resolve_parent(str(root), raw)
        if target is None:
            return False
        rel = target.resolve().relative_to(root) if target.is_absolute() else target
        parts = rel.parts
        return len(parts) == 2 and parts[0] == "ai" and parts[1].lower().endswith(".md")
    except Exception:
        return False


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
    1. 工具存在性 + agent 白名单校验
    2. risk != low -> 发起审批(阻塞)
    3. 执行工具
    """

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

        # agent 白名单校验已由调用方（agent_loop）控制，此处只做风险审批门

        whitelist: list[str] | None = None
        if getattr(agent, "template_id", None):
            # 注意:调用方在事务中读取 tpl;此处 agent 已是 ORM 对象
            # 白名单字段可能需调用方预注入到 ctx.data;这里宽松处理:
            pass
        # ctx.data 不存在;为简化,白名单校验放调用方(agent_runtime)做
        # 这里只做风险审批门

        # 风险审批门
        if tool.risk_level != "low":
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
                "summary": f"{ctx.agent_name} 申请执行 {tool_name}({tool.risk_level} 风险)",
            }
            # v2.2 (对齐 zcode 3.12): 权限模式三态 + 命令安全分级 + 工具级规则
            skip, deny_reason = await self._precheck_approval(tool, tool_name, args, ctx, detail)
            if deny_reason:
                return ToolResult(
                    ok=False, output="",
                    error=f"[权限策略] {deny_reason}",
                    data={"denied": True, "reason": deny_reason},
                )
            if skip:
                logger.info(
                    "跳过审批(权限模式/安全分级/工具规则): %s pm=%s",
                    tool_name, getattr(ctx, "permission_mode", "default"),
                )
            else:
                # 注册 on_request 回调(由 agent_runtime 传入,负责入库 + WS 广播)
                if on_approval_request is not None:
                    approval_manager.set_on_request(on_approval_request)
                approved = await approval_manager.request(approval_id=approval_id, detail=detail)
                if not approved:
                    return ToolResult(
                        ok=False, output="",
                        error=f"审批未通过/已超时({tool.risk_level} 风险:{tool_name})",
                        data={"approved": False, "approval_id": approval_id},
                    )

        # 执行
        try:
            # v4.8.2: 工具执行加 60 秒超时，防止同步 I/O 挂起
            # v2.2: ask_user_question 需要等用户回答，超时放宽到审批超时 + 30s
            import asyncio
            _timeout = 60.0
            if tool_name == "ask_user_question":
                from app.core.config import settings as _settings
                _timeout = float(_settings.approval_timeout_sec) + 30.0
            result = await asyncio.wait_for(tool.run(args, ctx), timeout=_timeout)
            return result
        except asyncio.TimeoutError:
            logger.error("工具执行超时 %s", tool_name)
            return ToolResult(ok=False, output="", error=f"工具执行超时(60s): {tool_name}")
        except Exception as e:
            logger.exception("工具执行异常 %s", tool_name)
            return ToolResult(ok=False, output="", error=f"工具异常: {e}")

    async def _precheck_approval(self, tool, tool_name: str, args: dict, ctx: ToolContext,
                                 detail: dict) -> tuple[bool, str]:
        """v2.2 (对齐 zcode 3.12): 审批门前决策。

        返回 (skip_approval, deny_reason)：deny_reason 非空 = 直接拒绝；
        否则 skip_approval=True 时免审批执行。
        决策顺序：命令安全分级(工具钩子) → 权限模式 → exec_policy 工具/命令规则。
        """
        # 写盘工具集合（accept_edits 免审 / plan 拒绝）
        _WRITE_TOOLS = ("fs_write", "editor_apply_diff", "multi_file_edit")

        # 1. 工具自身的安全分级钩子（terminal_exec 只读命令免审）
        try:
            skip, reason = tool.approval_precheck(args, ctx)
            if skip:
                return True, ""
        except Exception:
            logger.warning("工具 %s approval_precheck 异常(忽略)", tool_name, exc_info=True)

        # 2. 权限模式三态
        pm = getattr(ctx, "permission_mode", "default") or "default"
        if pm in ("plan", "readonly") and tool_name in _WRITE_TOOLS:
            # 规划模式放行计划文档写入（ai/*.md），其余写盘仍拒绝
            if pm == "plan" and tool_name == "fs_write" and _is_plan_doc_path(ctx, args):
                return True, ""
            return False, f"{'规划' if pm == 'plan' else '只读'}模式不允许写盘工具"
        if pm == "accept_edits" and tool_name in _WRITE_TOOLS:
            return True, ""

        # 3. exec_policy 规则（工具级 + terminal 命令级；需要 ctx.db）
        if ctx.db is not None:
            try:
                from app.services import exec_policy_service
                rules = await exec_policy_service.list_rules(ctx.db, session_id=ctx.session_id)
                decision, just = exec_policy_service.match_tool_rule(rules, tool_name)
                if decision is None and tool_name == "terminal_exec":
                    decision, just = exec_policy_service.match_rule(
                        rules, str(args.get("command", "") or ""),
                    )
                if decision == "allow":
                    return True, ""
                if decision == "deny":
                    return False, just or f"执行策略已禁止 {tool_name}"
            except Exception:
                logger.warning("exec_policy 规则匹配异常(忽略)", exc_info=True)

        return False, ""


# 全局单例(本期服务端执行)
tool_executor = ServerToolExecutor()
