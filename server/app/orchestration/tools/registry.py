"""v0.3: 工具注册表 — name → Tool 实例(单例)。"""
from functools import lru_cache

from app.orchestration.tools.base import Tool
from app.orchestration.tools.ask_user import AskUserQuestionTool
from app.orchestration.tools.ci import CiRunTool
from app.orchestration.tools.codebase_search import CodebaseSearchTool
from app.orchestration.tools.editor import EditorApplyDiffTool
from app.orchestration.tools.fs_list import FsListTool
from app.orchestration.tools.fs_read import FsReadTool
from app.orchestration.tools.fs_write import FsWriteTool
from app.orchestration.tools.git import GitTool
from app.orchestration.tools.git_diff import GitDiffTool
from app.orchestration.tools.grep import GrepTool
from app.orchestration.tools.memory_search import MemorySearchTool
from app.orchestration.tools.multi_edit import MultiFileEditTool
from app.orchestration.tools.read_attachment import ReadAttachmentTool
from app.orchestration.tools.terminal import TerminalExecTool
from app.orchestration.tools.todo import TodoWriteTool
from app.orchestration.tools.view_image import ViewImageTool
from app.orchestration.tools.web_fetch import WebFetchTool
from app.orchestration.tools.web_search import WebSearchTool


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        """根据工具名获取工具实例。

        v4.0: 工具名已统一使用下划线（fs_read / git_diff / ...），
        不再需要 dot→underscore 的 fallback 查找。
        但保留对旧格式名的兼容，防止数据库中已存储的旧配置失效。
        """
        tool = self._tools.get(name)
        if tool:
            return tool
        # 兼容旧格式名: fs.read → fs_read（数据库旧数据可能仍用点号）
        normalized = name.replace(".", "_").replace("/", "_")
        return self._tools.get(normalized)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def for_agent(self, whitelist: list[str] | None) -> list[Tool]:
        """返回 agent 白名单内的工具。

        v2.4: 白名单为空或 None 时返回全量(之前保守返回空导致 agent 无工具可用)。
        """
        if not whitelist:
            return self.all()
        return [t for t in self._tools.values() if t.name in whitelist]

    def all_schemas(self, whitelist: list[str] | None = None) -> list[dict]:
        """返回 OpenAI function-calling 的 tools 列表。"""
        tools = self.for_agent(whitelist)
        return [t.function_schema() for t in tools]


@lru_cache
def _build_default_registry() -> ToolRegistry:
    reg = ToolRegistry()
    for tool_cls in (
        FsReadTool, FsListTool, FsWriteTool,
        TerminalExecTool, EditorApplyDiffTool, WebFetchTool,
        CiRunTool, MemorySearchTool, GitDiffTool,
        GrepTool, WebSearchTool, ViewImageTool,
        ReadAttachmentTool, TodoWriteTool,
        # v2.2 (对齐 zcode 3.14): 补注册——多文件编辑/git 操作/代码库搜索/结构化提问
        MultiFileEditTool, GitTool, CodebaseSearchTool, AskUserQuestionTool,
    ):
        reg.register(tool_cls())
    return reg


def get_tool_registry() -> ToolRegistry:
    return _build_default_registry()


# 全局单例,便于直接 import
tool_registry = get_tool_registry()
