"""v20: 子代理工具 schema（仅主代理可见）与只读探索工具白名单。

spawn_subagent / collect_results 由 agent_loop 的 _run_subagent_tool 特判执行，
不走全局 tool_registry（避免子代理递归看到这两个工具）。
engine 为主代理组装 tool_schemas 时用 append_subagent_tools 追加。
"""
from __future__ import annotations

# v20: 探索子代理工具白名单（只读，无写盘/执行副作用）。
# 探索 = 调研/阅读/分析，主代理基于其结论串行整合实现。
EXPLORE_TOOLS = [
    "fs_read", "fs_list", "fs_grep", "git_diff",
    "memory_search", "web_fetch", "web_search", "view_image",
    "read_attachment", "codebase_search",
]

SPAWN_SUBAGENT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "spawn_subagent",
        "description": (
            "Launch an isolated subagent to work on a subtask in its own context. "
            "Subagents never share context with each other or with you; hand off everything they need. "
            "Set explore=true for a READ-ONLY research/investigation subtask: the tool call blocks until "
            "it finishes and returns the subagent's findings directly to you. "
            "For independent investigations, spawn several explore subagents in one round to run in parallel "
            "and save wall-clock time. Without explore, the subagent runs in the background; poll with "
            "collect_results. Do not delegate the final implementation to subagents — you execute serially "
            "and integrate their findings."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_title": {
                    "type": "string",
                    "description": "Short verb-object title of the subtask (under 40 characters).",
                },
                "task_description": {
                    "type": "string",
                    "description": "Detailed instructions: what to investigate or do, which files to read, what to look for, what to report back.",
                },
                "acceptance_criteria": {
                    "type": "string",
                    "description": "How to verify the subtask is done.",
                },
                "explore": {
                    "type": "boolean",
                    "description": "True = read-only research subtask whose findings are returned directly. Default false.",
                },
            },
            "required": ["task_title", "task_description"],
        },
    },
}

COLLECT_RESULTS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "collect_results",
        "description": (
            "Check the status and summaries of background subagents spawned this turn. "
            "Returns finished subagent results (done/failed) and how many are still running."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

SUBAGENT_TOOL_SCHEMAS = [SPAWN_SUBAGENT_SCHEMA, COLLECT_RESULTS_SCHEMA]


def append_subagent_tools(tool_schemas: list[dict]) -> list[dict]:
    """为主代理工具列表追加子代理工具 schema（按名去重，不重复追加）。"""
    names = {s.get("function", {}).get("name") for s in tool_schemas}
    out = list(tool_schemas)
    for schema in SUBAGENT_TOOL_SCHEMAS:
        if schema.get("function", {}).get("name") not in names:
            out.append(schema)
    return out
