"""主代理的工作流工具（设计 §5.8）：规划、启动、查询、续跑、取消。

工具由 agent_loop 特判执行（与子代理工具同一通道）。计划模式 / 只读模式下只允许规划与查询；
启动、续跑、取消在这些模式下直接拒绝（与续跑子代理的门禁口径一致）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.orchestration.workflow import service, store
from app.orchestration.workflow.graph import parse_plan, validate_plan

WORKFLOW_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "plan_workflow",
        "start_workflow",
        "workflow_status",
        "resume_workflow",
        "cancel_workflow",
        "amend_workflow",
    }
)
WORKFLOW_TOOL_RISK: dict[str, str] = {
    "plan_workflow": "low",
    "start_workflow": "medium",
    "workflow_status": "low",
    "resume_workflow": "medium",
    "cancel_workflow": "medium",
    "amend_workflow": "medium",
}
# 计划模式 / 只读模式下允许的工作流工具
READONLY_ALLOWED: frozenset[str] = frozenset({"plan_workflow", "workflow_status"})

BUILTIN_ROLES: frozenset[str] = frozenset({"explorer", "implementer", "reviewer", "general"})

_FLOW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "一个子工作流：一组有依赖关系的节点，整体可独立或与其它子工作流并行运行。",
    "properties": {
        "key": {"type": "string", "description": "子工作流标识（小写字母开头的 slug，如 backend）"},
        "title": {"type": "string", "description": "展示名称，使用当前对话语言"},
        "depends_on": {
            "type": "array",
            "items": {"type": "string"},
            "description": "需先成功完成的子工作流标识",
        },
        "isolation": {
            "type": "string",
            "enum": ["shared", "worktree"],
            "description": (
                "隔离方式：shared 共享工作区（默认，靠写入范围与串行保证安全）；"
                "worktree 独立 git 工作树（改动在分支中进行，结束时并入主工作区，需要 git 仓库）"
            ),
        },
        "nodes": {
            "type": "array",
            "description": "节点列表（按执行顺序声明；inputs 只能引用它之前的节点）",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "title": {"type": "string"},
                    "kind": {
                        "type": "string",
                        "enum": ["explore", "execute", "review", "check", "gate"],
                    },
                    "role": {
                        "type": "string",
                        "description": (
                            "子代理类型；缺省按 kind 取 explorer / implementer / reviewer"
                        ),
                    },
                    "task": {"type": "string", "description": "自包含的任务说明"},
                    "inputs": {"type": "array", "items": {"type": "string"}},
                    "acceptance": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "可验证的验收标准",
                    },
                    "write_scope": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "可写路径（execute 必填）",
                    },
                    "commands": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "确定性检查命令（白名单）",
                    },
                    "rework_to": {
                        "type": "string",
                        "description": "review 节点的返工目标（上游 execute）",
                    },
                    "max_rework": {"type": "integer", "minimum": 0, "maximum": 3},
                    "question": {"type": "string", "description": "gate 节点的待决定问题"},
                },
                "required": ["key", "kind"],
            },
        },
    },
    "required": ["key", "nodes"],
}


def workflow_tool_schemas() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "plan_workflow",
                "description": (
                    "为复杂、多阶段的任务现场规划一个工作流（探索 → 实现 → 评估，可并行与返工）。"
                    "只生成计划，不执行；返回计划编号与校验结果，用户确认后再调用 start_workflow。"
                    "简单任务直接完成，不要为了用工作流而用工作流。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "计划标题（不超过 40 字）"},
                        "goal": {"type": "string", "description": "总体目标"},
                        "rationale": {
                            "type": "string",
                            "description": "为什么这样拆分（不超过 200 字）",
                        },
                        "based_on": {
                            "type": "string",
                            "description": (
                                "以骨架为起点：预设 explore_build_review / parallel_research / "
                                "fix_and_verify，或用户模板 user:<key>。"
                                "本次 flows 按 key 覆盖骨架中的同名子工作流。"
                            ),
                        },
                        "flows": {
                            "type": "array",
                            "items": _FLOW_SCHEMA,
                            "description": "子工作流列表；使用 based_on 时只给出需要改动的子工作流",
                        },
                        "after": {
                            "type": "object",
                            "description": "所有子工作流完成后的集成检查（可选）",
                            "properties": {
                                "acceptance": {"type": "array", "items": {"type": "string"}},
                                "commands": {"type": "array", "items": {"type": "string"}},
                            },
                        },
                    },
                    "required": ["title", "goal"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "start_workflow",
                "description": "开始执行已生成的工作流计划（需用户确认后调用）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "program_id": {
                            "type": "integer",
                            "description": "plan_workflow 返回的计划编号",
                        }
                    },
                    "required": ["program_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "workflow_status",
                "description": (
                    "查询工作流进度：不传 program_id 时列出本会话的工作流；"
                    "传入则返回该工作流各节点状态与原因。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"program_id": {"type": "integer"}},
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "resume_workflow",
                "description": (
                    "从断点继续已暂停的工作流（网络中断、应用重启或用户暂停后）。已完成的节点不会重做。"
                    "用户表达“继续”意图时必须优先调用本工具，而不是重新规划。"
                    "若返回提示工作区在暂停期间有改动，先向用户说明并征得同意，"
                    "再以 accept_drift=true 调用。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "program_id": {"type": "integer"},
                        "note": {"type": "string", "description": "可选的补充说明"},
                        "accept_drift": {
                            "type": "boolean",
                            "description": "仅当用户已确认仍基于暂停期间的工作区改动继续时传 true",
                        },
                    },
                    "required": ["program_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "cancel_workflow",
                "description": (
                    "取消工作流（终态，已完成的改动保留在工作区）。仅在用户明确要求时调用。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"program_id": {"type": "integer"}},
                    "required": ["program_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "amend_workflow",
                "description": (
                    "修订运行中工作流尚未开始的部分（新增、替换或删除未开始的节点）。"
                    "已开始或已完成的节点必须与原规格一致；修订同样经过校验，最多 3 次。"
                    "reason 会记录在时间线中，请用一句话说明原因。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "program_id": {"type": "integer"},
                        "reason": {"type": "string", "description": "修订原因（一句话）"},
                        "title": {"type": "string"},
                        "goal": {"type": "string"},
                        "rationale": {"type": "string"},
                        "flows": {
                            "type": "array",
                            "items": _FLOW_SCHEMA,
                            "description": "修订后的完整子工作流列表；省略则保持当前计划的子工作流",
                        },
                        "after": {"type": "object"},
                    },
                    "required": ["program_id", "reason"],
                },
            },
        },
    ]


def append_workflow_tools(
    tool_schemas: list[dict[str, Any]], *, readonly_only: bool
) -> list[dict[str, Any]]:
    """把工作流工具追加到工具清单；只读 / 计划模式只暴露允许的工具；总开关关闭时不暴露。"""
    from app.core.config import settings

    if not settings.workflow_enabled:
        return tool_schemas
    names = {s["function"]["name"] for s in tool_schemas}
    extra = [
        s
        for s in workflow_tool_schemas()
        if s["function"]["name"] not in names
        and (not readonly_only or s["function"]["name"] in READONLY_ALLOWED)
    ]
    return [*tool_schemas, *extra]


async def _role_names() -> set[str]:
    from app.persistence.database import async_session_factory
    from app.persistence.models.subagent_profile import SubagentProfile

    async with async_session_factory() as s:
        rows = (await s.execute(select(SubagentProfile.name, SubagentProfile.is_active))).all()
    return set(BUILTIN_ROLES) | {name for name, active in rows if active}


def _hot_patterns() -> list[str]:
    """热点文件设置（逗号或换行分隔）：规划期用于串行化建议。"""
    from app.core.config import settings

    raw = str(settings.workflow_hot_files or "")
    return [part.strip() for part in raw.replace("\n", ",").split(",") if part.strip()]


async def validate_plan_for_run(
    plan_data: dict[str, Any],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """与 plan_workflow 相同的校验（角色、热点文件）：返回 (规范化计划, []) 或 (None, 诊断)。"""
    spec, diags = parse_plan(plan_data)
    if spec is None:
        return None, [_diag_dict(d) for d in diags]
    roles = await _role_names()
    result = validate_plan(
        spec, role_exists=lambda name: name in roles, hot_patterns=_hot_patterns()
    )
    if not result.ok:
        return None, [_diag_dict(d) for d in result.errors]
    return result.normalized.model_dump(), []


def _diag_dict(diag: Any) -> dict[str, Any]:
    return {
        "code": getattr(diag, "code", "schema"),
        "path": getattr(diag, "path", ""),
        "message": getattr(diag, "message", str(diag)),
        "suggestion": getattr(diag, "suggestion", None),
    }


def _format_diags(diags: list[Any]) -> str:
    lines = []
    for d in diags:
        suffix = f"（建议：{d.suggestion}）" if d.suggestion else ""
        lines.append(f"- [{d.code}] {d.path}：{d.message}{suffix}")
    return "\n".join(lines)


async def _plan(
    args: dict[str, Any],
    *,
    session_id: int,
    turn_id: int | None,
    workspace: str,
    readonly_only: bool,
) -> str:
    from app.core.config import settings as _settings
    from app.orchestration.workflow import presets
    from app.orchestration.workflow import store as _store

    based_on = str(args.get("based_on") or "").strip()
    if _settings.workflow_composition_mode == "presets_only" and not based_on:
        return (
            "当前设置为“仅允许预设骨架”：请用 based_on 指定预设（explore_build_review / "
            "parallel_research / fix_and_verify）或用户模板（user:<key>）后再规划。"
        )
    flows = args.get("flows") or []
    if based_on:
        if presets.is_user_ref(based_on):
            template = await _store.get_user_template(based_on[len(presets.USER_PREFIX) :])
            base = template["plan"] if template and template["enabled"] else None
        else:
            base = presets.preset_plan(based_on)
        if base is None:
            return (
                f"未找到骨架『{based_on}』。可用预设：explore_build_review、parallel_research、"
                "fix_and_verify；用户模板请使用 user:<key>。"
            )
        plan_data = presets.merge_into_base(
            base,
            title=str(args.get("title") or ""),
            goal=str(args.get("goal") or ""),
            rationale=str(args.get("rationale") or ""),
            flows=flows,
            after=args.get("after"),
        )
    else:
        plan_data = {
            "title": args.get("title", ""),
            "goal": args.get("goal", ""),
            "rationale": args.get("rationale", ""),
            "flows": flows,
        }
        if args.get("after"):
            plan_data["after"] = args["after"]
    spec, diags = parse_plan(plan_data)
    if spec is None:
        return "计划格式不正确，请修正后重新提交：\n" + _format_diags(diags)
    roles = await _role_names()
    result = validate_plan(
        spec,
        role_exists=lambda name: name in roles,
        readonly_only=readonly_only,
        hot_patterns=_hot_patterns(),
    )
    if not result.ok:
        return "计划未通过校验，请按提示修正后重新提交 plan_workflow：\n" + _format_diags(
            result.errors
        )
    program_id = await service.get_runtime().create_program(
        session_id=session_id,
        plan=result.normalized.model_dump(),
        workspace=workspace,
        origin_turn_id=turn_id,
    )
    stats = result.stats
    lines = [
        f"已生成工作流计划（program_id={program_id}，待用户确认）：{result.normalized.title}",
        f"子工作流 {stats['flows']} 个，节点 {stats['nodes']} 个，"
        f"预计代理调用约 {stats['estimated_agent_calls']} 次。",
    ]
    if result.warnings:
        lines.append("提示：\n" + _format_diags(result.warnings))
    from app.core.config import settings

    if not settings.workflow_confirm_before_start:
        # 设置关闭了开始前确认：规划后直接执行（与设置页的说明一致）
        await service.get_runtime().start(program_id)
        lines.append("已按设置直接开始执行（无需用户确认），进度可用 workflow_status 查询。")
        return "\n".join(lines)
    lines.append("用户确认后调用 start_workflow(program_id) 开始执行。")
    return "\n".join(lines)


async def _owned_program(program_id: int, session_id: int) -> dict[str, Any] | str:
    snap = await store.load_program(program_id)
    if snap is None or snap["run"]["session_id"] != session_id:
        return f"未找到工作流 {program_id}（或不属于本会话）"
    return snap


def _status_text(snap: dict[str, Any]) -> str:
    run = snap["run"]
    lines = [
        f"工作流 {run['id']}「{run['title']}」：{run['status']}"
        + (f"（{run['pause_reason']}）" if run.get("pause_reason") else "")
    ]
    for flow in snap["flows"]:
        lines.append(f"- 子工作流 {flow['run']['package_key']}：{flow['run']['status']}")
        for node in flow["nodes"]:
            reason = f" · {node['reason']}" if node.get("reason") else ""
            lines.append(f"  · {node['key']}（{node['kind']}）：{node['status']}{reason}")
    return "\n".join(lines)


async def run_workflow_tool(
    tool_name: str,
    args: dict[str, Any],
    *,
    session_id: int,
    turn_id: int | None,
    workspace: str,
    readonly_only: bool,
) -> str:
    """执行一个工作流工具，返回给模型的文本结果（失败也以文本返回，便于模型修正）。"""
    if (
        tool_name in ("start_workflow", "resume_workflow", "cancel_workflow", "amend_workflow")
        and readonly_only
    ):
        return (
            "当前为只读/计划模式，不能启动、续跑、修订或取消工作流。"
            "请先退出计划模式，或改用只读探索。"
        )
    try:
        if tool_name == "plan_workflow":
            return await _plan(
                args,
                session_id=session_id,
                turn_id=turn_id,
                workspace=workspace,
                readonly_only=readonly_only,
            )
        if tool_name == "workflow_status":
            if args.get("program_id") is None:
                programs = await store.list_programs(session_id)
                if not programs:
                    return "本会话还没有工作流。"
                return "\n".join(f"- {p['id']} {p['title']}：{p['status']}" for p in programs[:10])
            snap = await _owned_program(int(args["program_id"]), session_id)
            return snap if isinstance(snap, str) else _status_text(snap)
        program_id = int(args.get("program_id") or 0)
        snap = await _owned_program(program_id, session_id)
        if isinstance(snap, str):
            return snap
        runtime = service.get_runtime()
        if tool_name == "amend_workflow":
            current = (snap["run"].get("graph") or {}).get("flows") or []
            plan_data: dict[str, Any] = {
                "title": args.get("title") or snap["run"]["title"],
                "goal": args.get("goal") or snap["run"]["goal"],
                "rationale": args.get("rationale") or snap["run"]["rationale"],
                "flows": args.get("flows") or current,
            }
            if args.get("after"):
                plan_data["after"] = args["after"]
            rev = await runtime.amend(program_id, plan_data, reason=str(args.get("reason") or ""))
            return (
                f"工作流 {program_id} 计划已修订为第 {rev} 版；"
                "已开始的步骤保持不变，进度可用 workflow_status 查询。"
            )
        if tool_name == "start_workflow":
            await runtime.start(program_id)
            return f"工作流 {program_id} 已开始执行，进度可用 workflow_status 查询。"
        if tool_name == "resume_workflow":
            await runtime.resume(
                program_id,
                str(args.get("note") or ""),
                accept_drift=bool(args.get("accept_drift")),
            )
            return f"工作流 {program_id} 已从断点继续，已完成的节点不会重做。"
        if tool_name == "cancel_workflow":
            await runtime.cancel(program_id)
            return f"工作流 {program_id} 已取消。"
    except ValueError as exc:
        return f"无法执行：{exc}"
    return f"未知的工作流工具：{tool_name}"
