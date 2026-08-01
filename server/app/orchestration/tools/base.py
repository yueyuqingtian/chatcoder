"""v0.3: 工具抽象基类与数据结构。"""
import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

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
