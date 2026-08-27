"""v20: 子代理工具 schema（仅主代理可见）与只读探索工具白名单。

spawn_subagent / collect_results 由 agent_loop 的 _run_subagent_tool 特判执行，
不走全局 tool_registry（避免子代理递归看到这两个工具）。
engine 为主代理组装 tool_schemas 时用 append_subagent_tools 追加。

v22: 子代理类型开关（SubagentProfile.is_active）在工具暴露前检查——
设置里停用的类型不再把对应工具 schema 给模型，避免模型反复尝试被拒的调用。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

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
            "Do NOT spawn sub-agents unless the user explicitly asks for sub-agents, delegation, or parallel "
            "agent work, or the subtask is a genuinely independent research question whose parallel "
            "investigation would materially improve speed or quality. For simple or straightforward tasks, "
            "do the work yourself with direct tool calls instead. "
            "Subagents never share context with each other or with you; hand off everything they need. "
            "Set explore=true for a READ-ONLY research/investigation subtask: the tool call blocks until "
            "it finishes and returns the subagent's findings directly to you. "
            "Without explore, the subagent runs in the background; poll with collect_results. "
            "Do not delegate the final implementation to subagents — you execute serially "
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


def filter_tool_schemas(schemas: list[dict], whitelist: list[str] | None) -> list[dict]:
    """v3.0 (plan-88): 按工具名白名单过滤 schema 列表。

    whitelist 为空/None = 原样返回（全量语义）。用于子代理类型工具权限：
    勾选=允许（模型只见这些 schema），不勾=禁止。
    """
    if not whitelist:
        return schemas
    allowed = set(whitelist)
    return [s for s in schemas if s.get("function", {}).get("name") in allowed]


async def load_subagent_type_states(db) -> dict[str, bool]:
    """查询 SubagentProfile 启停状态，返回 {类型名: is_active}。

    engine 组装主代理工具 schema 前调用；查询失败时返回空 dict（默认放行）。
    """
    states: dict[str, bool] = {}
    try:
        from sqlalchemy import select
        from app.persistence.models.subagent_profile import SubagentProfile
        res = await db.execute(select(SubagentProfile))
        for p in res.scalars().all():
            states[p.name] = bool(p.is_active)
    except Exception:
        logger.warning("[subagent-tools] 子代理类型状态查询失败(按默认放行)", exc_info=True)
    return states


def append_subagent_tools(tool_schemas: list[dict],
                          subagent_types: dict[str, bool] | None = None) -> list[dict]:
    """为主代理工具列表追加子代理工具 schema（按名去重，不重复追加）。

    v22: 子代理类型开关（SubagentProfile.is_active）前移到工具暴露前——
    engine 在 async 上下文查询 profile 启停后传入 subagent_types（{类型名: is_active}），
    类型停用时不再把 spawn_subagent/collect_results 暴露给模型，
    避免模型反复尝试被拒的调用（此前仅 spawn 执行时返回错误）。

    参数：
        subagent_types: 类型名 → is_active 映射；None/缺省 = 全部放行（旧行为兼容）。
    """
    allow_spawn = True
    allow_collect = True

    if subagent_types is not None:
        explore_active = subagent_types.get("explore", True)
        general_active = subagent_types.get("general", True)
        # 如果 explore 停用且 general 停用（或无任何活跃子代理），完全禁用子代理工具
        if not explore_active and not general_active:
            allow_spawn = False
            allow_collect = False
        elif not explore_active:
            # explore 停用，但 general 仍开启：如果用户关闭了 explore，只允许普通子任务
            # 但 spawn_subagent 默认带 explore 参数，此处若需完全关闭则遵循开关
            allow_spawn = bool(general_active)
            allow_collect = bool(general_active)

    out = [
        s for s in tool_schemas
        if (allow_spawn or s.get("function", {}).get("name") != "spawn_subagent")
        and (allow_collect or s.get("function", {}).get("name") != "collect_results")
    ]
    names = {s.get("function", {}).get("name") for s in out}

    if allow_spawn and SPAWN_SUBAGENT_SCHEMA["function"]["name"] not in names:
        out.append(SPAWN_SUBAGENT_SCHEMA)
    if allow_collect and COLLECT_RESULTS_SCHEMA["function"]["name"] not in names:
        out.append(COLLECT_RESULTS_SCHEMA)

    return out
