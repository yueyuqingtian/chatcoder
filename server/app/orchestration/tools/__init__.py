"""v0.3: Agent 工具包(服务端执行)。

工具按风险等级:
- low:fs.read / fs.list — 免审批,但仍做路径防穿越。
- medium:editor.apply_diff / web.fetch — 走审批。
- high:fs.write / terminal.exec — 走审批。

ToolExecutor 抽象接口:本期实现 ServerToolExecutor;
v0.5 可加 ClientToolExecutor(WS 下发)不改调用方。
"""
from app.orchestration.tools.base import Tool, ToolContext, ToolResult
from app.orchestration.tools.executor import ServerToolExecutor, ToolExecutor, tool_executor
from app.orchestration.tools.registry import tool_registry

__all__ = [
    "Tool",
    "ToolContext",
    "ToolResult",
    "ToolExecutor",
    "ServerToolExecutor",
    "tool_executor",
    "tool_registry",
]
