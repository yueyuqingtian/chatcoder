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
            "Its final message is returned to you as the tool result (the user does not see it), "
            "and its context starts fresh — the prompt must be self-contained.\n"
            "\nDECIDE AUTONOMOUSLY: judging by the criteria below, you may spawn one or more "
            "subagents on your own initiative — no user request is required.\n"
            "\n## When to use\n"
            "- Work that splits into independent subtasks you can run in parallel.\n"
            "- Broad exploration or research that would take more than ~3 search/read calls to answer — "
            "delegate it and keep the conclusion, not the raw file dumps.\n"
            "- Anything where you only need the conclusion instead of raw outputs in your own context.\n"
            "\n## When NOT to use\n"
            "- A single-fact lookup where you already know the file, symbol or value — search directly.\n"
            "- Simple or tightly sequential work: do it yourself with a few direct tool calls.\n"
            "\n## How to dispatch\n"
            "- background=true is the DEFAULT and recommended way: dispatch and return immediately, keep "
            "working on other things, and the completion report is pushed back to you automatically "
            "(do NOT poll for it). Combine it with explore=true when the subtask is read-only research.\n"
            "- explore=true only marks the subtask READ-ONLY — pair it with background=true for read-only "
            "research you do not need to block on.\n"
            "- explore=true WITHOUT background=true BLOCKS your turn until the subagent finishes and returns "
            "its findings inline. Use that combination only when your very next step depends on the "
            "conclusion and the subtask is small.\n"
            "- Independent subtasks: send SEVERAL spawn_subagent calls in ONE message so they run concurrently.\n"
            "- NEVER dispatch the same subtask twice, and give each subtask a distinct task_title; "
            "check collect_results first if unsure whether an equivalent one already exists.\n"
            "- Subagents never share context with each other or with you: hand off everything they need.\n"
            "\n## After the dispatch — waiting discipline\n"
            "- NEVER do the delegated task yourself — duplicated work costs tokens and time twice over; a "
            "subagent that looks slow is usually just still working. Take only NON-overlapping work; if none "
            "is left, call collect_results(wait=true) to block until they finish, or end your turn.\n"
            "- Subagents report back a structured summary (result, files, findings, verification, acceptance, "
            "risks); use subagent_inspect only to read context/trajectory. You integrate their work."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_title": {
                    "type": "string",
                    "description": (
                        "Short verb-object title of the subtask (under 40 characters), "
                        "written in the current reply language (the language the user speaks)."
                    ),
                },
                "task_description": {
                    "type": "string",
                    "description": (
                        "Detailed instructions: what to investigate or do, which files to read, what to look "
                        "for, what to report back. Write it in the current reply language — the subagent "
                        "mirrors the language of this handoff, so an English text makes it open in English."
                    ),
                },
                "acceptance_criteria": {
                    "type": "string",
                    "description": "How to verify the subtask is done. Written in the current reply language.",
                },
                "explore": {
                    "type": "boolean",
                    "description": (
                        "True = the subtask is READ-ONLY research (no writes, no commands). Default false. "
                        "Combine with background=true for non-blocking read-only research; setting explore "
                        "alone BLOCKS your turn until the subagent finishes."
                    ),
                },
                "background": {
                    "type": "boolean",
                    "description": (
                        "PREFER setting this to true: dispatch and return immediately (run in the background), "
                        "no blocking wait — you keep doing other work in parallel. The completion report is "
                        "pushed back to you automatically when the subagent finishes, so do NOT poll. Leave it "
                        "false only when you need the conclusion inline before your next step. Default false."
                    ),
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
            "Check the status and structured summaries of subagents spawned this turn. "
            "Returns each finished subagent's status, result summary, files touched, key findings, "
            "risks/blockers, plus how many are still running. "
            "Set wait=true to block until all subagents finish (preferred over repeated polling)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "wait": {
                    "type": "boolean",
                    "description": "True = block until all spawned subagents finish before returning. Default false.",
                },
                "agent_id": {
                    "type": "integer",
                    "description": (
                        "Only report this specific subagent (id returned by spawn_subagent). "
                        "Omit to report every subagent spawned this turn."
                    ),
                },
            },
            "required": [],
        },
    },
}

SUBAGENT_INSPECT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "subagent_inspect",
        "description": (
            "Inspect a specific subagent spawned this turn: its handoff input (task, inherited context), "
            "its execution trajectory (tool-call sequence with brief outputs) and current status/result. "
            "Use this when you need the subagent's detailed context or reasoning rather than just its summary."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "integer", "description": "Subagent agent id returned by spawn_subagent."},
                "section": {
                    "type": "string",
                    "description": "Which part to read: context (handoff + inherited context), transcript (trajectory), result (final summary). Default: all.",
                },
            },
            "required": ["agent_id"],
        },
    },
}

CANCEL_SUBAGENT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "cancel_subagent",
        "description": (
            "Cancel one still-running subagent spawned this turn (id returned by spawn_subagent). "
            "Use it only when the subtask is genuinely no longer needed — the plan changed or the "
            "scope was dropped. Finished subagents cannot be cancelled; never cancel after duplicating work."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "integer", "description": "Subagent agent id returned by spawn_subagent."},
            },
            "required": ["agent_id"],
        },
    },
}

SEND_TO_SUBAGENT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "send_to_subagent",
        "description": (
            "Send a follow-up instruction to a STILL-RUNNING subagent spawned this turn "
            "(id returned by spawn_subagent). The instruction is injected into its context before "
            "its next model call, so you can steer, correct or narrow its work without cancelling it. "
            "Use cancel_subagent to stop it entirely. Finished subagents cannot receive instructions."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "integer", "description": "Subagent id returned by spawn_subagent."},
                "message": {
                    "type": "string",
                    "description": "The instruction to deliver (be specific and self-contained).",
                },
            },
            "required": ["agent_id", "message"],
        },
    },
}

REPORT_TO_LEADER_SCHEMA = {
    "type": "function",
    "function": {
        "name": "report_to_leader",
        "description": (
            "Report progress to the main agent, or ask it a question (subagents only). "
            "The message reaches the main agent's context before its next model call, and is also "
            "visible in your own thread. "
            "kind=\"progress\" (default): fire-and-forget status update — keep working afterwards. "
            "kind=\"question\" with wait=true: block until the main agent replies. Only background "
            "subagents may wait; synchronous/read-only subagents cannot (include the open question "
            "in your final report instead)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "What to report or ask."},
                "kind": {
                    "type": "string",
                    "description": "progress (default) = status update; question = need a decision.",
                },
                "wait": {
                    "type": "boolean",
                    "description": "Only for kind=question: true = block until the main agent replies.",
                },
                "timeout_s": {
                    "type": "integer",
                    "description": "Max seconds to wait for a reply when wait=true (default 300).",
                },
            },
            "required": ["message"],
        },
    },
}

SUBAGENT_TOOL_SCHEMAS = [
    SPAWN_SUBAGENT_SCHEMA, COLLECT_RESULTS_SCHEMA, SUBAGENT_INSPECT_SCHEMA, CANCEL_SUBAGENT_SCHEMA,
    SEND_TO_SUBAGENT_SCHEMA,
]

# plan-330-1648 M4: 子代理自身可用的通信工具（只在**子代理**侧暴露，主代理不可见）。
SUBAGENT_OWN_TOOLS: list[dict] = [REPORT_TO_LEADER_SCHEMA]

# v36 (plan-321-1600 R1)：子代理工具名集合与展示用风险级。
# 这些工具由 agent_loop 特判执行（不经 executor/registry），但权限模式白名单、
# 权限面板工具清单都需要认识它们——否则「配置了不生效」。
SUBAGENT_TOOL_NAMES: set[str] = (
    {s["function"]["name"] for s in SUBAGENT_TOOL_SCHEMAS}
    | {s["function"]["name"] for s in SUBAGENT_OWN_TOOLS}
)
SUBAGENT_TOOL_RISK: dict[str, str] = {
    "spawn_subagent": "medium",
    "collect_results": "low",
    "subagent_inspect": "low",
    "cancel_subagent": "low",
    "send_to_subagent": "low",
    "report_to_leader": "low",
}


def _spawn_schema(explore_only: bool) -> dict:
    """spawn_subagent schema；只读/计划模式下替换为「只读探索」版本（v36）。

    explore_only=True 时：描述改为仅支持只读探索子任务，并移除 explore 参数（只读由
    运行时强制、无需模型传参）；background 参数**保留**——plan-64-289 起 background 只
    决定调度方式、不再影响读写范围，「只读 + 异步」合法且推荐（v36 早期注释称连带移除
    background，系解耦前口径，已作废）。
    """
    if not explore_only:
        return SPAWN_SUBAGENT_SCHEMA
    import copy

    schema = copy.deepcopy(SPAWN_SUBAGENT_SCHEMA)
    fn = schema["function"]
    fn["description"] = (
        "Launch a READ-ONLY exploration subagent to research a self-contained subtask in its "
        "own context. Its findings are reported back to you (the user does not see them), and "
        "its context starts fresh — the prompt must be self-contained.\n"
        "\nDECIDE AUTONOMOUSLY: you may dispatch exploration subagents on your own initiative "
        "when the criteria below are met — no user request is required.\n"
        "\n## When to use\n"
        "- Broad exploration or research that would take more than ~3 search/read calls to answer — "
        "delegate it and keep the conclusion, not the raw file dumps.\n"
        "- Several independent research areas you can investigate in parallel.\n"
        "\n## When NOT to use\n"
        "- A single-fact lookup where you already know the file, symbol or value — search directly.\n"
        "\n## Mode note\n"
        "- The current permission mode is READ-ONLY / PLAN: only read-only exploration is "
        "available. NEVER ask the subagent to modify, write or create files — it will be rejected.\n"
        "\n## How to dispatch\n"
        "- PREFER background=true: dispatch and return immediately, keep working on other things, "
        "and the completion report is pushed back to you automatically (do NOT poll).\n"
        "- Without background=true the call BLOCKS your turn until the subagent finishes and "
        "returns the findings inline — use it only when you need the conclusion right now.\n"
        "- Send SEVERAL spawn_subagent calls in ONE message to run them concurrently.\n"
        "- Give each subtask a distinct task_title; never dispatch the same subtask twice.\n"
        "\n## After the dispatch\n"
        "- Once you delegate a piece of work, do NOT also do it yourself — work on non-overlapping "
        "things, then integrate the conclusion.\n"
        "- Use collect_results / subagent_inspect when you need more detail."
    )
    props = fn["parameters"]["properties"]
    props.pop("explore", None)  # 仍由运行时强制只读，无需模型传参
    return schema


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
                          subagent_types: dict[str, bool] | None = None,
                          *,
                          explore_only: bool = False,
                          allowed: set[str] | None = None) -> list[dict]:
    """为主代理工具列表追加子代理工具 schema（按名去重，不重复追加）。

    v22: 子代理类型开关（SubagentProfile.is_active）前移到工具暴露前——
    engine 在 async 上下文查询 profile 启停后传入 subagent_types（{类型名: is_active}），
    类型停用时不再把 spawn_subagent/collect_results 暴露给模型，
    避免模型反复尝试被拒的调用（此前仅 spawn 执行时返回错误）。

    v36 (plan-321-1600 R1): 四种权限模式都支持子代理——
    - explore_only=True（只读/计划模式）：仅暴露只读探索子代理（spawn 描述换成
      只读探索版；隐藏 explore、保留 background，支持只读+异步），general 不参与本模式；
    - allowed：白名单模式下的子代理工具子集（None = 全量）——设置页未勾选的
      子代理工具不暴露，勾选真正生效。

    参数：
        subagent_types: 类型名 → is_active 映射；None/缺省 = 全部放行（旧行为兼容）。
        explore_only: 只读/计划模式（仅只读探索）。
        allowed: 允许暴露的子代理工具名集合；None = 不限制。
    """
    allow_spawn = True
    allow_collect = True

    if subagent_types is not None:
        explore_active = subagent_types.get("explore", True)
        general_active = subagent_types.get("general", True)
        if explore_only:
            # v36: 只读/计划模式只放行只读探索类型（general 不暴露）
            allow_spawn = bool(explore_active)
            allow_collect = bool(explore_active)
        elif not explore_active and not general_active:
            # 两个类型都停用 → 完全禁用子代理工具
            allow_spawn = False
            allow_collect = False
        elif not explore_active:
            # explore 停用、general 开启 → 仅普通子任务
            allow_spawn = bool(general_active)
            allow_collect = bool(general_active)

    _permitted = (lambda name: True) if allowed is None else (lambda name: name in allowed)

    # 先剔除传入列表中已有的子代理工具（幂等），再按本次口径重新追加
    out = [
        s for s in tool_schemas
        if s.get("function", {}).get("name") not in SUBAGENT_TOOL_NAMES
    ]
    names = {s.get("function", {}).get("name") for s in out}

    if allow_spawn and _permitted("spawn_subagent") and "spawn_subagent" not in names:
        out.append(_spawn_schema(explore_only))
        names.add("spawn_subagent")
    for _schema in (COLLECT_RESULTS_SCHEMA, SUBAGENT_INSPECT_SCHEMA, CANCEL_SUBAGENT_SCHEMA):
        _name = _schema["function"]["name"]
        if allow_collect and _permitted(_name) and _name not in names:
            out.append(_schema)
            names.add(_name)
    # plan-330-1648 M4: 主代理 → 子代理的运行中指令通道（与 collect 同门控：子代理能力可用即可用）
    if allow_collect and _permitted("send_to_subagent") and "send_to_subagent" not in names:
        out.append(SEND_TO_SUBAGENT_SCHEMA)
        names.add("send_to_subagent")
    return out

    return out
