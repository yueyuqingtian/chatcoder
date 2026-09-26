"""v0.3: 工具抽象基类与数据结构。"""
import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Literal

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

RiskLevel = Literal["low", "medium", "high"]


@dataclass
class ToolContext:
    """工具执行上下文。"""

    workspace_root: str
    session_id: int
    task_id: int
    agent_id: int
    agent_name: str
    # v0.9: 任务中断事件,set 后 agent loop 与长工具应主动退出
    cancel_event: asyncio.Event | None = None
    # P0 修复: agent 主循环同连接 db 会话,供工具直接落库（避免跨连接 SQLite 写锁）
    db: "AsyncSession | None" = None
    # plan-75-332: 执行模式（readonly / plan / agent）——"能做什么"，越界即拒绝
    permission_mode: str = "agent"
    # plan-75-332: 权限模式（ask / auto / full）——"要不要问"，与执行模式正交
    approval_mode: str = "ask"
    # plan-88 引入的沙箱模式字段。plan-75-332 起不再有任何判定消费它
    # （read-only / danger-full-access 的硬边界已由执行模式×权限模式取代）；
    # 保留仅为兼容 agent_loop / subagent_context 的既有赋值，避免改动面外溢。
    sandbox_mode: str = "workspace-write"
    # plan-75-332: 审批卡「解释」专用思考深度默认取本 turn 的档位
    # （未指定时回落全局 agent_reasoning_effort，见 approval_explain_service）。
    reasoning_effort: str | None = None
    # v7(B): 运行时输出回调——长命令（terminal_exec 同步模式）执行期间逐帧上报增量输出，
    # 由 agent_loop 注入闭包并广播 tool.output WS 事件（前端运行中实时展示）。
    on_tool_output: Callable[[str], Awaitable[None]] | None = None


@dataclass
class ToolResult:
    """工具执行结果(转 OpenAI tool message content)。"""

    ok: bool
    output: str  # 给 LLM 看的文本输出
    data: dict[str, Any] = field(default_factory=dict)  # 结构化附加信息
    error: str = ""

    def to_dict(self) -> dict:
        return {"ok": self.ok, "output": self.output, "data": self.data, "error": self.error}


class Tool(ABC):
    """所有工具的抽象基类。"""

    name: str = "base"
    risk_level: RiskLevel = "low"
    description: str = ""

    @abstractmethod
    def function_schema(self) -> dict:
        """返回 OpenAI function-calling 的 tools 元素 dict。"""

    @abstractmethod
    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """执行工具。"""

    def approval_precheck(self, args: dict[str, Any], ctx: ToolContext) -> tuple[bool, str]:
        """v2.2 (对齐 zcode 3.12 命令安全分级): 审批门前置检查。

        plan-75-332 **已废弃**：工具不再自行决定是否免审——那等于工具在替用户
        做审批决策，与统一策略重复。是否需询问用户一律由
        `approval_policy.decide()` 按「执行模式 × 权限模式」判定。
        保留默认实现仅为兼容既有子类与测试的签名约定。
        """
        return False, ""
