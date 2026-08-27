"""v0.3: 工具抽象基类与数据结构。"""
import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

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
    # v2.2 (对齐 zcode 3.12): 会话权限模式 default / accept_edits / plan
    permission_mode: str = "default"
    # v3.0 (plan-88): 沙箱模式 read-only / workspace-write / danger-full-access
    # （P0：executor 审批门消费；P1/P2：进程与文件系统隔离，见 docs/sandbox-design.md）
    sandbox_mode: str = "workspace-write"


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

        返回 (skip_approval, reason)：
        - skip_approval=True 时 executor 跳过审批直接执行（安全命令免审）；
        - 默认 (False, "")，维持原有 risk_level 审批流程。
        """
        return False, ""
