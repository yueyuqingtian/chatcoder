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
            import asyncio
            result = await asyncio.wait_for(tool.run(args, ctx), timeout=60.0)
            return result
        except asyncio.TimeoutError:
            logger.error("工具执行超时 %s", tool_name)
            return ToolResult(ok=False, output="", error=f"工具执行超时(60s): {tool_name}")
        except Exception as e:
            logger.exception("工具执行异常 %s", tool_name)
            return ToolResult(ok=False, output="", error=f"工具异常: {e}")


# 全局单例(本期服务端执行)
tool_executor = ServerToolExecutor()
