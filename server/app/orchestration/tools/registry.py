"""v0.3: 工具注册表 — name → Tool 实例(单例)。"""
from functools import lru_cache

from app.orchestration.tools.base import Tool
from app.orchestration.tools.ask_user import AskUserQuestionTool
from app.orchestration.tools.bg_process import TerminalBgStatusTool, TerminalBgKillTool
from app.orchestration.tools.browser import ALL_BROWSER_TOOLS
from app.orchestration.tools.ci import CiRunTool
from app.orchestration.tools.codebase_search import CodebaseSearchTool
from app.orchestration.tools.compaction_view import CompactionIndexTool, CompactionViewTool
from app.orchestration.tools.desktop import (
    DesktopAppsTool,
    DesktopClickTool,
    DesktopFindTextTool,
    DesktopFocusTool,
    DesktopHitTool,
    DesktopKeysTool,
    DesktopRecipeTool,
    DesktopScreenshotTool,
    DesktopScrollTool,
    DesktopSnapshotTool,
    DesktopTypeTool,
    DesktopWindowsTool,
)
from app.orchestration.tools.editor import EditorApplyDiffTool
from app.orchestration.tools.fs_list import FsListTool
from app.orchestration.tools.fs_read import FsReadTool
from app.orchestration.tools.fs_write import FsWriteTool
from app.orchestration.tools.git import GitTool
from app.orchestration.tools.git_diff import GitDiffTool
from app.orchestration.tools.goal import GoalCompleteTool
from app.orchestration.tools.grep import GrepTool
from app.orchestration.tools.memory_search import MemorySearchTool
from app.orchestration.tools.memory_write import MemoryWriteTool
from app.orchestration.tools.multi_edit import MultiFileEditTool
from app.orchestration.tools.read_attachment import ReadAttachmentTool
from app.orchestration.tools.skill_view import SkillViewTool
from app.orchestration.tools.symbol_search import OutlineTool, SymbolSearchTool
from app.orchestration.tools.terminal import TerminalExecTool
from app.orchestration.tools.todo import TodoWriteTool
from app.orchestration.tools.view_image import ViewImageTool
from app.orchestration.tools.web_fetch import WebFetchTool
from app.orchestration.tools.web_search import WebSearchTool


def _gated_off(tool: Tool) -> bool:
    """工具声明了 feature_enabled（功能开关）且当前关闭。"""
    gate = getattr(tool, "feature_enabled", None)
    if gate is None:
        return False
    try:
        return not gate()
    except Exception:  # noqa: BLE001
        return False


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

    def for_agent(self, whitelist: list[str] | None, include_scoped: bool = False) -> list[Tool]:
        """返回 agent 白名单内的工具。

        v2.4: 白名单为空或 None 时返回全量(之前保守返回空导致 agent 无工具可用)。

        plan-128-518：「会话态工具」（Tool.session_scoped=True，如 goal_complete）默认不下发——
        它们只在会话处于对应模式（目标模式激活）时才由引擎显式 include_scoped=True 放行。
        此前它们随全量工具无条件下发，导致用户没用目标模式模型也会调用目标工具。
        """
        # plan-20261015-browser-agent：功能开关关闭的工具（如浏览器套件）不下发 schema——
        # 关着的能力模型调了也只会报错，白占每轮几千 token；执行与历史回放仍可经 get() 取到。
        pool = [
            t for t in self._tools.values()
            if (include_scoped or not getattr(t, "session_scoped", False)) and not _gated_off(t)
        ]
        if not whitelist:
            return pool
        return [t for t in pool if t.name in whitelist]

    def all_schemas(self, whitelist: list[str] | None = None, include_scoped: bool = False) -> list[dict]:
        """返回 OpenAI function-calling 的 tools 列表。"""
        tools = self.for_agent(whitelist, include_scoped=include_scoped)
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
        # v30.1: 压缩索引查看（AI 按需查看压缩前会话）
        CompactionIndexTool, CompactionViewTool,
        # plan-230-1144 M1.2: 技能正文按需加载（此前 content 从未送达模型，AI 无法主动使用技能）
        SkillViewTool,
        # plan-230-1144 M3: 符号索引检索与文件骨架（函数级定位，替代大范围扫描探索）
        SymbolSearchTool, OutlineTool,
        # plan-230-1144 M4.1: 记忆三层化——AI 主动写记忆（此前只有 turn 后被动抽取）
        MemoryWriteTool,
        # plan-20261015-browser-agent：浏览器工具套件（驱动右侧面板内置浏览器，无桌面端时回退后台浏览器）
        *ALL_BROWSER_TOOLS,
        # 目标模式（plan-671）：模型标记会话目标完成，停止自动续跑
        GoalCompleteTool,
        # v1.0 (plan-153-705): 后台进程查询/终止（配合 terminal_exec waitForCompletion=false）
        TerminalBgStatusTool, TerminalBgKillTool,
        # plan-334-1661: 电脑操控（内置原生工具，非 MCP——内核常驻，详见 tools/desktop.py）
        DesktopWindowsTool, DesktopSnapshotTool, DesktopHitTool,
        DesktopScreenshotTool, DesktopFindTextTool,
        DesktopClickTool, DesktopTypeTool, DesktopKeysTool, DesktopScrollTool,
        DesktopFocusTool, DesktopAppsTool, DesktopRecipeTool,
    ):
        reg.register(tool_cls())
    return reg


def get_tool_registry() -> ToolRegistry:
    return _build_default_registry()


# 全局单例,便于直接 import
tool_registry = get_tool_registry()
