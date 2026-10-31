"""ta3 供应商官方工具面白名单（plan-598-2482）。

为什么需要这份清单：ta3 伪装层此前把本项目全部 60 个工具都改名下发（见
`ta3_tool_aliases.TO_TA3` 的 75 条映射），而官方客户端在普通 agent 会话里只下发
27 个内置工具 + MCP 外部工具。上行工具集合与官方差异过大是体系特征之一，因此这里
显式声明「官方有对应」的内置工具白名单：只有名单内的工具以内置形态上行，其余全部
改走 MCP 外部工具通道（见 `ta3_mcp.build_extension_tools`）。

来源：官方客户端 Ta+3 牛码 0.7.5 解包产物
`dist/main/plugin/runtime/builtInCatalog.js:46-83` 及各工具插件定义，
只取普通 agent 会话可见的内置工具；创造模式（plugin_*）、测试组（test_*）、
Work 族（Work*）、专家族（*Expert*/DelegateExperts）、场景 context 工具不计入。
"""
from __future__ import annotations

# 官方客户端内置工具的 function.name（模型可见名，不是注册 id）。
# 注意官方存在 id ≠ function.name 的情况（如 id=GetProjectMemory → name=get_project_memory），
# 这里一律用 function.name，因为上行只带该字段。
TA3_OFFICIAL_BUILTIN_TOOLS: frozenset[str] = frozenset({
    # 文件与检索
    "Read",
    "List",
    "Search",
    "Diff",
    "ReadSkill",
    "Write",
    "Edit",
    "get_file_outline",
    "get_project_memory",
    "generate_project_memory",
    # 网络与终端
    "WebSearch",
    "Bash",
    # 任务与会话
    "TodoWrite",
    "ask_user",
    # 子代理
    "SubAgent",
})


def is_official_builtin(ta3_name: str) -> bool:
    """ta3 伪装名是否为官方内置工具名（决定它走内置面还是 MCP 通道）。"""
    return str(ta3_name or "") in TA3_OFFICIAL_BUILTIN_TOOLS
