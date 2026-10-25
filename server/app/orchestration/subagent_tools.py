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
    # plan-20261016-ux（问题 7）：符号检索 / 文件骨架 / 技能查阅同属纯读，探索子代理此前缺失——
    # 已开索引的项目里子代理只能逐个文件 grep，调研又慢又费 token
    "symbol_search", "outline", "skill_view",
    # plan-560-2311 D10：功能地图查询也是纯读，且主提示词的工具选择表把它列为
    # 「知道要改什么功能、不知道在哪个模块」时的首选入口。子代理拿不到它，只能靠
    # 关键词反复 grep，调研质量与主代理不一致。压缩块查看（compaction_index /
    # compaction_view）不进本表：子代理是新上下文、没有压缩块可查，暴露只会误导。
    "feature_lookup",
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
            "- Implementation work whose write scope is disjoint from yours — dispatch it with "
            "`mode=\"agent\"` instead of doing every edit yourself.\n"
            "- Anything where you only need the conclusion instead of raw outputs in your own context.\n"
            "\n## When NOT to use\n"
            "- A single-fact lookup where you already know the file, symbol or value — search directly.\n"
            "- Simple or tightly sequential work: do it yourself with a few direct tool calls.\n"
            "\n## How to dispatch\n"
            "- Dispatch returns immediately: you are NOT blocked while the subagent runs. Keep working on "
            "other things and the completion report is pushed back to you automatically (do NOT poll).\n"
            "- `mode` is the subagent's execution mode and the ONLY thing that decides what it may do: "
            "`readonly` = research/review only (no writes, no commands); `agent` = may write and run "
            "commands. Default `agent`. It never changes how the dispatch is scheduled.\n"
            "- `explore` is the legacy alias of the same read/write switch (true = readonly) — prefer `mode`.\n"
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
                "mode": {
                    "type": "string",
                    "enum": ["readonly", "agent"],
                    "description": (
                        "The subagent's execution mode — the only switch that decides what it may do. "
                        "readonly = research/review only (no writes, no commands); "
                        "agent = may write files and run commands. Default agent. "
                        "Plan mode and workflow tools are never available to a subagent, and the "
                        "approval mode is always inherited from this session."
                    ),
                },
                "explore": {
                    "type": "boolean",
                    "description": (
                        "Legacy alias of mode: true = readonly, false = agent. Prefer `mode`; "
                        "kept so older sessions and ta3-backed calls keep working."
                    ),
                },
                "purpose": {
                    "type": "string",
                    # 不能把 "" 放进 enum：Gemini 系网关直接拒绝空枚举值
                    # （400 GenerateContentRequest.tools[...].enum[0]: cannot be empty），
                    # 会让整轮请求失败。普通任务省略该参数即可，语义与旧版空串一致。
                    "enum": ["operator"],
                    "description": (
                        "operator = GUI-operating subagent: it runs at the lowest reasoning effort "
                        "configured for operator subagents (settings.operator_subagent_effort) and is "
                        "meant for multi-step browser / desktop operations where speed matters more "
                        "than deep reasoning. Omit this parameter for normal tasks."
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
            "Set wait=true to block until ALL of them finish (preferred over repeated polling); "
            "use it when your next step genuinely depends on their conclusions."
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
                "report_offset": {
                    "type": "integer",
                    "description": (
                        "Character offset for reading a long result in pages (use with agent_id). "
                        "When a result says it is not finished, call again with the offset it gives "
                        "and keep reading until the whole result is read."
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

RESUME_SUBAGENT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "resume_subagent",
        "description": (
            "Resume a subagent that was INTERRUPTED (network/gateway failure, or the previous turn was "
            "stopped) and kept its checkpoint. It continues from where it stopped with all completed "
            "steps and context intact — use this instead of spawn_subagent whenever the user asks to "
            "continue / resume / retry earlier work and a paused subagent is listed in your context "
            "(## Paused Subagents). Never re-dispatch the same task with spawn_subagent in that case."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "integer",
                    "description": "Id of the paused subagent to resume (listed under ## Paused Subagents).",
                },
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
            "its next model call, so you can steer, correct or narrow its work without cancelling it.\n"
            "You may call it MULTIPLE times on the same subagent — each message is queued and "
            "delivered in order; the newest instruction wins when they conflict. "
            "This is also how you ANSWER a subagent's question (see its `report_to_leader` message): "
            "the reply reaches the waiting subagent and unblocks it. "
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
            "kind=\"question\" with wait=true: block until the main agent replies (no time limit by default — "
            "the main agent may need to check with the user first, which can take a while). "
            "Every subagent runs in the background, so any of them may wait this way — use it when you "
            "genuinely cannot proceed without a decision."
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
                    # plan-549-2281（复盘）：与实现口径对齐——等待**默认不设上限**（主代理可能还要问用户，
                    # 用户可能很久才回），timeout_s 仅在调用方显式给出时生效。此前文案写死默认值，
                    # 模型会误以为到时无回复即失败而提前放弃。
                    "description": "Only if you explicitly need a cap: max seconds to wait when wait=true. Omit it to wait without a time limit (recommended).",
                },
            },
            "required": ["message"],
        },
    },
}

SUBAGENT_TOOL_SCHEMAS = [
    SPAWN_SUBAGENT_SCHEMA, COLLECT_RESULTS_SCHEMA, SUBAGENT_INSPECT_SCHEMA, CANCEL_SUBAGENT_SCHEMA,
    SEND_TO_SUBAGENT_SCHEMA, RESUME_SUBAGENT_SCHEMA,
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
    "resume_subagent": "medium",
    "report_to_leader": "low",
}

# ── plan-474-2105：子代理的能力边界 ──────────────────────────
# 子代理是主代理派生的一次性执行体：
#   · 不得直接向用户提问（用户只与主代理对话，提问会无人可见而无限阻塞），
#     需要决策用 report_to_leader(kind="question")；
#   · 不得再派生下级执行体，也不得跨会话操作（子会话工具一律不可见）。
# 子代理工具名按字面量列出（不 import subsession_tools，避免两模块循环导入）。
SUBAGENT_FORBIDDEN: frozenset[str] = frozenset({
    "ask_user_question",
    "create_subsession", "send_to_subsession", "get_subsession_progress",
    "interrupt_subsession", "message_main", "message_peer",
    "spawn_subagent", "collect_results", "subagent_inspect", "cancel_subagent",
    "send_to_subagent", "resume_subagent",
    # plan-549-2281：子代理不得进入计划模式、也不得创建/使用工作流。工作流工具不在
    # tool_registry 里（子代理原本天然拿不到），但「类型工具白名单」「自定义权限模式」
    # 都能把名字塞回来——这里显式列出，与上方子代理工具同一道防线。
    # 用字面量而非 import workflow.tools：避免与 workflow 包形成循环导入。
    "plan_workflow", "start_workflow", "workflow_status", "resume_workflow",
    "cancel_workflow", "amend_workflow", "decide_workflow", "request_write_scope",
})


def strip_for_subagent(tool_schemas: list[dict]) -> list[dict]:
    """剔除子代理不得拥有的工具（保留 report_to_leader 这一条自有通道）。"""
    return [
        s for s in tool_schemas
        if s.get("function", {}).get("name") not in SUBAGENT_FORBIDDEN
    ]


def _spawn_schema(explore_only: bool) -> dict:
    """spawn_subagent schema；只读/计划模式下替换为「只读探索」版本（v36）。

    explore_only=True 时：描述改为仅支持只读探索子任务，并移除 explore / mode / purpose
    参数（只读由运行时强制、无需模型传参）。plan-541-2264：只读/计划模式下同样只有后台派发，
    参数面不再存在任何调度开关。

    plan-560-2311 C3：受限变体此前只 pop 掉 `explore`，`mode`（enum readonly/agent）与
    `purpose`（enum operator）仍暴露在参数面里。运行时会兜住（mode 被强制只读、operator
    被显式拒绝），但「模型看得到的参数」与「真正生效的参数」不一致，模型会反复传一个
    必然被拒的值。这里把三者一并移除，并在描述里写明「只读由运行时强制」，与运行时同口径。
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
        "- The current permission mode is READ-ONLY / PLAN: the runtime forces read-only, so this "
        "tool takes NO mode / purpose argument. NEVER ask the subagent to modify, write or create "
        "files — it will be rejected.\n"
        "\n## How to dispatch\n"
        "- Dispatch returns immediately: you are NOT blocked. The conclusion comes back as a completion "
        "report pushed to you automatically; use collect_results(wait=true) only when you need it now.\n"
        "- Send SEVERAL spawn_subagent calls in ONE message to run them concurrently.\n"
        "- Give each subtask a distinct task_title; never dispatch the same subtask twice.\n"
        "\n## After the dispatch\n"
        "- Once you delegate a piece of work, do NOT also do it yourself — work on non-overlapping "
        "things, then integrate the conclusion.\n"
        "- Use collect_results / subagent_inspect when you need more detail."
    )
    props = fn["parameters"]["properties"]
    # plan-560-2311 C3：三个能力开关在受限变体里都不该出现——只读由运行时强制（explore），
    # 可写派发（mode）与操控子代理（purpose=operator）在本模式下必被拒。
    props.pop("explore", None)
    props.pop("mode", None)
    props.pop("purpose", None)
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
      只读探索版；隐藏 explore / mode / purpose 参数，只读由运行时强制），general 不参与本模式；
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
    for _schema in (COLLECT_RESULTS_SCHEMA, SUBAGENT_INSPECT_SCHEMA, CANCEL_SUBAGENT_SCHEMA,
                    RESUME_SUBAGENT_SCHEMA):
        _name = _schema["function"]["name"]
        if allow_collect and _permitted(_name) and _name not in names:
            out.append(_schema)
            names.add(_name)
    # plan-330-1648 M4: 主代理 → 子代理的运行中指令通道（与 collect 同门控：子代理能力可用即可用）
    if allow_collect and _permitted("send_to_subagent") and "send_to_subagent" not in names:
        out.append(SEND_TO_SUBAGENT_SCHEMA)
        names.add("send_to_subagent")
    return out
