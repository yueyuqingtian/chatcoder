"""工作流持久化（设计 §5.2）：写入经 run_write_locked 单写线程，读取走 async_session_factory。

约定：事件统一挂在程序（根）运行下，按程序内单调的 seq 排序；子工作流与节点通过 parent_run_id /
run_id 关联。读取返回普通 dict，避免把 ORM 对象带出会话。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select

from app.core.config import settings
from app.orchestration.workflow.graph import INTEGRATE_NODE_KEY
from app.persistence.database import async_session_factory, run_write_locked
from app.persistence.models.workflow import (
    WorkflowAttempt,
    WorkflowEvent,
    WorkflowNode,
    WorkflowPlanRevision,
    WorkflowRun,
    WorkflowTemplate,
    WorkflowToolCall,
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_dict(row: WorkflowRun) -> dict[str, Any]:
    return {
        "id": row.id,
        "session_id": row.session_id,
        "parent_run_id": row.parent_run_id,
        "kind": row.kind,
        "package_key": row.package_key,
        "title": row.title or "",
        "goal": row.goal or "",
        "rationale": row.rationale or "",
        "based_on": row.based_on,
        "status": row.status,
        "pause_kind": row.pause_kind,
        "pause_reason": row.pause_reason,
        "closed_reason": row.closed_reason,
        "paused_at": row.paused_at,
        "resume_count": row.resume_count or 0,
        "rework_total": row.rework_total or 0,
        "token_used": row.token_used or 0,
        "token_budget": _budget_for(row),
        "workspace": row.workspace or "",
        "isolation": row.isolation or "shared",
        "depends_on": list(row.depends_on_json or []),
        "graph": row.graph_json,
        "plan_rev": row.plan_rev or 1,
        "origin_turn_id": row.origin_turn_id,
        "last_turn_id": row.last_turn_id,
        "last_seq": row.last_seq or 0,
        "started_at": row.started_at,
        "updated_at": row.updated_at,
        "finished_at": row.finished_at,
        "error": row.error_json,
    }


def _budget_for(row: WorkflowRun) -> int:
    """程序的 token 预算取当前设置（保存即生效）；子工作流不单独计预算。"""
    if row.parent_run_id is not None:
        return 0
    from app.core.config import settings

    return max(0, int(settings.workflow_token_budget_per_run or 0))


def _node_dict(row: WorkflowNode) -> dict[str, Any]:
    return {
        "id": row.id,
        "run_id": row.run_id,
        "key": row.node_key,
        "title": row.title or row.node_key,
        "kind": row.kind,
        "role": row.role,
        "task": row.task or "",
        "inputs": list(row.inputs_json or []),
        "acceptance": list(row.acceptance_json or []),
        "write_scope": list(row.write_scope_json or []),
        "commands": list(row.commands_json or []),
        "rework_to": row.rework_to,
        "max_rework": row.max_rework,
        "question": row.question,
        "status": row.status,
        "reason": row.reason,
        "attempt_no": row.attempt_no or 0,
        "rework_round": row.rework_round or 0,
        "output": row.output_json,
        "verdict": row.verdict_json,
        "checks": row.checks_json,
        "agent_id": row.agent_id,
        "next_retry_at": row.next_retry_at,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
        "error": row.error_json,
        "pending": row.pending_json,
        "conflict": row.conflict_json,
        "last_heartbeat_at": row.last_heartbeat_at,
        "token_used": row.token_used or 0,
    }


# ── 读取 ─────────────────────────────────────────────


async def load_program(program_id: int) -> dict[str, Any] | None:
    """程序快照：根运行 + 子工作流（含节点）。不存在或不是程序返回 None。"""
    async with async_session_factory() as s:
        root = await s.get(WorkflowRun, program_id)
        if root is None or root.kind != "program":
            return None
        flows = (
            (
                await s.execute(
                    select(WorkflowRun)
                    .where(WorkflowRun.parent_run_id == program_id)
                    .order_by(WorkflowRun.id)
                )
            )
            .scalars()
            .all()
        )
        flow_ids = [f.id for f in flows]
        nodes_by_flow: dict[int, list[dict[str, Any]]] = {fid: [] for fid in flow_ids}
        if flow_ids:
            node_rows = (
                (
                    await s.execute(
                        select(WorkflowNode)
                        .where(WorkflowNode.run_id.in_(flow_ids))
                        .order_by(WorkflowNode.id)
                    )
                )
                .scalars()
                .all()
            )
            for row in node_rows:
                nodes_by_flow[row.run_id].append(_node_dict(row))
        # 步骤按计划中的顺序排列（改模板、修订后行的创建顺序不代表计划顺序）；
        # 运行时追加的步骤（并入、合并）排在最后
        plan_flows = (root.graph_json or {}).get("flows") or []
        plan_order = {
            f.get("key"): {n.get("key"): i for i, n in enumerate(f.get("nodes") or [])}
            for f in plan_flows
        }
        for f in flows:
            order = plan_order.get(f.package_key or "", {})
            nodes_by_flow[f.id].sort(key=lambda n: (order.get(n["key"], len(order) + 1), n["id"]))
        return {
            "run": _run_dict(root),
            "flows": [{"run": _run_dict(f), "nodes": nodes_by_flow.get(f.id, [])} for f in flows],
        }


async def add_tokens(program_id: int, node_id: int, tokens: int) -> None:
    """累计节点与程序的 token 用量（预算判定依据）。"""
    if tokens <= 0:
        return

    def _op(s):
        root = s.get(WorkflowRun, program_id)
        node = s.get(WorkflowNode, node_id)
        if root is not None:
            root.token_used = (root.token_used or 0) + tokens
        if node is not None:
            node.token_used = (node.token_used or 0) + tokens
        s.commit()

    await run_write_locked(_op, label="workflow.add_tokens")


async def touch_heartbeat(node_id: int) -> None:
    """记录节点最近一次有进展的时间（停滞判定与界面“最近活动”共用）。"""

    def _op(s):
        row = s.get(WorkflowNode, node_id)
        if row is not None:
            row.last_heartbeat_at = now_iso()
            s.commit()

    await run_write_locked(_op, label="workflow.heartbeat")


def _flow_status_for(program_status: str) -> str:
    if program_status in ("running", "waiting_human"):
        return "running"
    if program_status == "paused":
        return "paused"
    return "draft"


def _write_flows(s, root: WorkflowRun, plan: dict[str, Any], ts: str) -> None:
    """按计划重写子工作流与节点：只改动尚未开始的部分（调用方保证已开始的节点未变）。"""
    flow_rows = s.query(WorkflowRun).filter(WorkflowRun.parent_run_id == root.id).all()
    by_key = {f.package_key: f for f in flow_rows}
    wanted = {f["key"] for f in plan["flows"]}
    for key, frow in by_key.items():
        if key not in wanted:
            for nrow in s.query(WorkflowNode).filter(WorkflowNode.run_id == frow.id).all():
                s.delete(nrow)
            s.delete(frow)
    for flow in plan["flows"]:
        frow = by_key.get(flow["key"])
        if frow is None:
            frow = WorkflowRun(
                session_id=root.session_id,
                parent_run_id=root.id,
                kind="flow",
                package_key=flow["key"],
                title=flow.get("title") or flow["key"],
                status=_flow_status_for(root.status),
                workspace="",
                isolation=flow.get("isolation") or settings.workflow_isolation_default,
                depends_on_json=list(flow.get("depends_on", [])),
                updated_at=ts,
            )
            s.add(frow)
            s.flush()
        else:
            frow.title = flow.get("title") or flow["key"]
            frow.depends_on_json = list(flow.get("depends_on", []))
            frow.updated_at = ts
        existing = {
            n.node_key: n
            for n in s.query(WorkflowNode).filter(WorkflowNode.run_id == frow.id).all()
        }
        wanted_nodes = {n["key"] for n in flow["nodes"]}
        for key, nrow in existing.items():
            if (
                key not in wanted_nodes
                and nrow.status == "pending"
                and nrow.kind not in RUNTIME_NODE_KINDS
            ):
                s.delete(nrow)
        for node in flow["nodes"]:
            fields = {
                "title": node.get("title") or node["key"],
                "kind": node["kind"],
                "role": node.get("role"),
                "task": node.get("task", ""),
                "inputs_json": list(node.get("inputs", [])),
                "acceptance_json": list(node.get("acceptance", [])),
                "write_scope_json": list(node.get("write_scope", [])),
                "commands_json": list(node.get("commands", [])),
                "rework_to": node.get("rework_to"),
                "max_rework": node.get("max_rework"),
                "question": node.get("question"),
            }
            nrow = existing.get(node["key"])
            if nrow is None:
                s.add(
                    WorkflowNode(
                        run_id=frow.id,
                        node_key=node["key"],
                        status="pending",
                        attempt_no=0,
                        rework_round=0,
                        **fields,
                    )
                )
            elif nrow.status == "pending":
                for name, value in fields.items():
                    setattr(nrow, name, value)
        if frow.isolation == "worktree":
            # 独立工作树：并入节点等待本子工作流的全部步骤，修订后步骤变了就同步依赖
            upstream = [n["key"] for n in flow["nodes"]]
            integrate = existing.get(INTEGRATE_NODE_KEY)
            if integrate is None:
                s.add(_integrate_node(frow.id, upstream))
            elif integrate.status == "pending":
                integrate.inputs_json = upstream


async def apply_plan_revision(program_id: int, plan: dict[str, Any], rev: int, reason: str) -> None:
    """运行中修订计划（设计 §4.5 第 5 点）：只改动尚未开始的子工作流与节点，已校验后一次落库。

    调用方负责校验：已开始的节点必须与原规格一致、已开始的子工作流不得删除或改依赖。
    """
    ts = now_iso()

    def _op(s):
        root = s.get(WorkflowRun, program_id)
        if root is None:
            return
        root.graph_json = plan
        root.plan_rev = rev
        root.title = plan["title"]
        root.goal = plan["goal"]
        root.rationale = plan.get("rationale", "")
        root.updated_at = ts
        s.add(
            WorkflowPlanRevision(run_id=program_id, rev=rev, graph_json=plan, reason=reason[:300])
        )
        _write_flows(s, root, plan, ts)
        s.commit()

    await run_write_locked(_op, label="workflow.amend")


async def apply_draft_plan(program_id: int, plan: dict[str, Any]) -> None:
    """草稿计划的局部改写（如替换某个子工作流的模板）：不产生修订记录，也不占用修订次数。"""
    ts = now_iso()

    def _op(s):
        root = s.get(WorkflowRun, program_id)
        if root is None:
            return
        root.graph_json = plan
        root.updated_at = ts
        _write_flows(s, root, plan, ts)
        s.commit()

    await run_write_locked(_op, label="workflow.draft_plan")


# 由运行时追加、不属于计划的节点类型（修订计划时不得删除）
RUNTIME_NODE_KINDS = frozenset({"integrate", "merge"})


def _integrate_node(flow_run_id: int, upstream_keys: list[str]) -> WorkflowNode:
    """独立工作树子工作流的并入节点：在全部步骤完成后把改动并入主工作区，不调用模型。"""
    return WorkflowNode(
        run_id=flow_run_id,
        node_key=INTEGRATE_NODE_KEY,
        title="并入主工作区",
        kind="integrate",
        role=None,
        task="",
        inputs_json=list(upstream_keys),
        acceptance_json=[],
        write_scope_json=[],
        commands_json=[],
        rework_to=None,
        max_rework=None,
        status="pending",
        attempt_no=0,
        rework_round=0,
    )


_MODE_LABEL = {
    "fresh": "新建",
    "checkpoint": "续跑",
    "rework": "返工",
    "repair": "格式修复",
    "manual": "手动重试",
}


def _text_list(items: Any) -> list[str]:
    """产出里的条目转为短文本（最多 20 条，每条 200 字）。"""
    if not isinstance(items, list):
        return []
    return [str(item)[:200] for item in items][:20]


def _node_detail(kind: str, output: dict[str, Any], verdict: dict[str, Any]) -> dict[str, Any]:
    """按节点类型整理产出：探索 → plan，实现 → verification，评估 → issues（最多 20 条）。"""
    if kind == "explore":
        return {"plan": _text_list(output.get("plan"))}
    if kind == "execute":
        return {"verification": _text_list(output.get("verification"))}
    if kind == "review":
        issues = verdict.get("issues") or []
        cleaned = [
            {
                "severity": str(item.get("severity") or "minor"),
                "file": str(item.get("file") or ""),
                "line": item.get("line") if isinstance(item.get("line"), int) else None,
                "problem": str(item.get("problem") or "")[:300],
                "fix_hint": str(item.get("fix_hint") or "")[:300],
            }
            for item in issues
            if isinstance(item, dict)
        ]
        return {"issues": cleaned[:20]}
    return {}


async def node_briefs(node_ids: list[int]) -> dict[int, dict[str, Any]]:
    """子代理面板的工作流节点头：节点、所属子工作流与程序、最近一次尝试的模式、评估结论与产出摘要。"""
    ids = [int(i) for i in node_ids if i]
    if not ids:
        return {}
    async with async_session_factory() as s:
        nodes = (
            (await s.execute(select(WorkflowNode).where(WorkflowNode.id.in_(ids)))).scalars().all()
        )
        flow_ids = list({n.run_id for n in nodes})
        flows = (
            {
                f.id: f
                for f in (await s.execute(select(WorkflowRun).where(WorkflowRun.id.in_(flow_ids))))
                .scalars()
                .all()
            }
            if flow_ids
            else {}
        )
        program_ids = list({f.parent_run_id for f in flows.values() if f.parent_run_id})
        programs = (
            {
                p.id: p
                for p in (
                    await s.execute(select(WorkflowRun).where(WorkflowRun.id.in_(program_ids)))
                )
                .scalars()
                .all()
            }
            if program_ids
            else {}
        )
        attempts = (
            (
                await s.execute(
                    select(WorkflowAttempt)
                    .where(WorkflowAttempt.node_id.in_(ids))
                    .order_by(WorkflowAttempt.id.asc())
                )
            )
            .scalars()
            .all()
        )
    last_mode: dict[int, str] = {}
    for attempt in attempts:
        last_mode[attempt.node_id] = attempt.mode
    out: dict[int, dict[str, Any]] = {}
    for node in nodes:
        flow = flows.get(node.run_id)
        program = programs.get(flow.parent_run_id) if flow and flow.parent_run_id else None
        verdict = node.verdict_json or {}
        output = node.output_json or {}
        out[node.id] = {
            "node_id": node.id,
            "node_key": node.node_key,
            "node_title": node.title or node.node_key,
            "kind": node.kind,
            "status": node.status,
            "attempt_no": int(node.attempt_no or 0),
            "rework_round": int(node.rework_round or 0),
            "mode": _MODE_LABEL.get(last_mode.get(node.id, ""), ""),
            "verdict": str(verdict.get("decision") or ""),
            "summary": str(output.get("summary") or output.get("text") or "")[:400],
            "flow_key": (flow.package_key if flow else "") or "",
            "flow_title": (flow.title if flow else "") or (flow.package_key if flow else "") or "",
            "program_id": program.id if program else None,
            "program_title": (program.title if program else "") or "",
            # 节点的产出（设计 §7.6）：探索给出计划，实现给出验证结果，评估给出问题清单
            "detail": _node_detail(node.kind, output, verdict),
        }
    return out


async def begin_tool_call(
    attempt_id: int,
    node_id: int,
    llm_call_id: str,
    tool: str,
    args_preview: str,
    effect_json: str | None = None,
) -> int:
    """记录一次工具调用开始（台账）；返回台账编号。"""
    ts = now_iso()

    def _op(s):
        row = WorkflowToolCall(
            attempt_id=attempt_id,
            node_id=node_id,
            llm_call_id=llm_call_id[:128],
            tool=tool[:64],
            args_preview=args_preview[:300],
            effect_json=effect_json,
            status="started",
            started_at=ts,
        )
        s.add(row)
        s.flush()
        call_id = int(row.id)
        s.commit()
        return call_id

    return await run_write_locked(_op, label="workflow.toolcall.begin")


async def end_tool_call(call_id: int, status: str) -> None:
    """补写工具调用的结果（done / failed）。"""

    def _op(s):
        row = s.get(WorkflowToolCall, call_id)
        if row is None:
            return
        row.status = status
        row.finished_at = now_iso()
        s.commit()

    await run_write_locked(_op, label="workflow.toolcall.end")


async def node_tool_calls(node_id: int) -> list[dict[str, Any]]:
    """节点在所有尝试中的工具调用台账（按发生顺序）。"""
    async with async_session_factory() as s:
        rows = (
            (
                await s.execute(
                    select(WorkflowToolCall)
                    .where(WorkflowToolCall.node_id == node_id)
                    .order_by(WorkflowToolCall.id.asc())
                )
            )
            .scalars()
            .all()
        )
    return [
        {
            "id": int(r.id),
            "llm_call_id": r.llm_call_id or "",
            "tool": r.tool,
            "args_preview": r.args_preview or "",
            "effect_json": r.effect_json,
            "status": r.status,
        }
        for r in rows
    ]


async def mark_tool_calls_reconciled(call_ids: list[int]) -> None:
    """续跑对账已经告知过模型的台账记录标记为已对账，避免之后的续跑重复提示。"""
    if not call_ids:
        return

    def _op(s):
        for row in s.query(WorkflowToolCall).filter(WorkflowToolCall.id.in_(call_ids)).all():
            row.status = "reconciled"
        s.commit()

    await run_write_locked(_op, label="workflow.toolcall.reconciled")


async def count_merge_steps(flow_run_id: int, prefix: str) -> int:
    """子工作流内以 prefix 开头的合并步骤数（用于限制同一节点的合并次数）。"""
    async with async_session_factory() as s:
        result = await s.execute(
            select(func.count())
            .select_from(WorkflowNode)
            .where(
                WorkflowNode.run_id == flow_run_id,
                WorkflowNode.kind == "merge",
                WorkflowNode.node_key.startswith(prefix),
            )
        )
        return int(result.scalar_one())


async def create_runtime_node(
    flow_run_id: int,
    *,
    node_key: str,
    title: str,
    kind: str,
    task: str,
    acceptance: list[str],
    write_scope: list[str],
) -> int:
    """运行时在子工作流内追加一个节点（L3 合并步骤），从待启动开始；返回节点编号。"""

    def _op(s):
        row = WorkflowNode(
            run_id=flow_run_id,
            node_key=node_key,
            title=title,
            kind=kind,
            role=None,
            task=task,
            inputs_json=[],
            acceptance_json=list(acceptance),
            write_scope_json=list(write_scope),
            commands_json=[],
            rework_to=None,
            max_rework=None,
            status="pending",
            attempt_no=0,
            rework_round=0,
        )
        s.add(row)
        s.flush()
        node_id = int(row.id)
        s.commit()
        return node_id

    return await run_write_locked(_op, label="workflow.node.insert")


async def add_node_scope(node_id: int, patterns: list[str]) -> list[str]:
    """把节点的写入范围扩展为并集（申请扩展写入范围被授予时使用），返回扩展后的范围。"""

    def _op(s):
        row = s.get(WorkflowNode, node_id)
        if row is None:
            return []
        merged = list(dict.fromkeys([*(row.write_scope_json or []), *patterns]))
        row.write_scope_json = merged
        s.commit()
        return merged

    return await run_write_locked(_op, label="workflow.node.scope")


def _template_dict(row: WorkflowTemplate) -> dict[str, Any]:
    return {
        "key": row.key,
        "name": row.name or row.key,
        "description": row.description or "",
        "source": "user",
        "enabled": bool(row.enabled),
        "plan": row.graph_json or {},
        "updated_at": row.updated_at,
    }


async def list_user_templates() -> list[dict[str, Any]]:
    async with async_session_factory() as s:
        rows = (
            (await s.execute(select(WorkflowTemplate).order_by(WorkflowTemplate.id.desc())))
            .scalars()
            .all()
        )
        return [_template_dict(r) for r in rows]


async def get_user_template(key: str) -> dict[str, Any] | None:
    async with async_session_factory() as s:
        row = (
            await s.execute(select(WorkflowTemplate).where(WorkflowTemplate.key == key))
        ).scalar_one_or_none()
        return _template_dict(row) if row is not None else None


def _slug(name: str) -> str:
    import re
    import secrets

    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:28] or "template"
    return f"{base}-{secrets.token_hex(2)}"


async def save_template(
    plan: dict[str, Any], *, name: str, description: str = ""
) -> dict[str, Any]:
    """把一个计划保存为用户模板（key 由名称生成，写入时校验唯一）。"""

    def _op(s):
        key = _slug(name)
        while s.query(WorkflowTemplate).filter(WorkflowTemplate.key == key).first() is not None:
            key = _slug(name)
        row = WorkflowTemplate(
            key=key,
            name=name[:80],
            description=description[:300],
            graph_json=plan,
            enabled=True,
            updated_at=now_iso(),
        )
        s.add(row)
        s.commit()
        return _template_dict(row)

    return await run_write_locked(_op, label="workflow.template.save")


async def update_user_template(
    key: str,
    *,
    name: str | None = None,
    description: str | None = None,
    enabled: bool | None = None,
    plan: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """更新用户模板；plan 为已通过校验的模板计划（调用方负责校验）。"""

    def _op(s):
        row = s.query(WorkflowTemplate).filter(WorkflowTemplate.key == key).first()
        if row is None:
            return None
        if name is not None:
            row.name = name[:80]
        if description is not None:
            row.description = description[:300]
        if enabled is not None:
            row.enabled = bool(enabled)
        if plan is not None:
            row.graph_json = plan
        row.updated_at = now_iso()
        s.commit()
        return _template_dict(row)

    return await run_write_locked(_op, label="workflow.template.update")


async def delete_user_template(key: str) -> bool:
    def _op(s):
        row = s.query(WorkflowTemplate).filter(WorkflowTemplate.key == key).first()
        if row is None:
            return False
        s.delete(row)
        s.commit()
        return True

    return await run_write_locked(_op, label="workflow.template.delete")


async def load_fingerprint(program_id: int) -> dict[str, str] | None:
    """程序暂停时记录的工作区指纹（无记录返回 None）。"""
    async with async_session_factory() as s:
        row = await s.get(WorkflowRun, program_id)
        if row is None or not row.fingerprint_json:
            return None
        return dict(row.fingerprint_json)


async def program_counts() -> dict[str, int]:
    """程序级计数（跨会话）：运行中（含等待决定）与已暂停，设置页概览使用。"""
    async with async_session_factory() as s:
        rows = (
            await s.execute(
                select(WorkflowRun.status, func.count())
                .where(
                    WorkflowRun.kind == "program",
                    WorkflowRun.status.in_(["running", "waiting_human", "paused"]),
                )
                .group_by(WorkflowRun.status)
            )
        ).all()
    counts = {str(status): int(n) for status, n in rows}
    return {
        "running": counts.get("running", 0) + counts.get("waiting_human", 0),
        "paused": counts.get("paused", 0),
    }


async def running_program_ids(
    session_id: int | None = None, statuses: tuple[str, ...] = ("running", "waiting_human")
) -> list[int]:
    """需要调度的程序：运行中，或等待决定但其它子工作流仍可推进；可按会话与状态筛选。"""
    async with async_session_factory() as s:
        stmt = select(WorkflowRun.id).where(
            WorkflowRun.kind == "program", WorkflowRun.status.in_(list(statuses))
        )
        if session_id is not None:
            stmt = stmt.where(WorkflowRun.session_id == session_id)
        rows = (await s.execute(stmt)).scalars().all()
        return list(rows)


async def find_node(flow_run_id: int, node_key: str) -> dict[str, Any] | None:
    async with async_session_factory() as s:
        row = (
            (
                await s.execute(
                    select(WorkflowNode).where(
                        WorkflowNode.run_id == flow_run_id, WorkflowNode.node_key == node_key
                    )
                )
            )
            .scalars()
            .first()
        )
        return _node_dict(row) if row is not None else None


async def list_events(
    program_id: int, after_seq: int = 0, limit: int = 200
) -> list[dict[str, Any]]:
    """程序事件回补（断线重连后按 seq 补齐）。"""
    async with async_session_factory() as s:
        rows = (
            (
                await s.execute(
                    select(WorkflowEvent)
                    .where(WorkflowEvent.run_id == program_id, WorkflowEvent.seq > after_seq)
                    .order_by(WorkflowEvent.seq)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [
            {
                "seq": r.seq,
                "type": r.type,
                "node_key": r.node_key,
                "payload": r.payload_json or {},
                "created_at": str(r.created_at),
            }
            for r in rows
        ]


_SESSION_STATE_PRIORITY = ("attention", "paused", "running", "draft")
_SESSION_STATE_OF = {
    "waiting_human": "attention",
    "paused": "paused",
    "running": "running",
    "draft": "draft",
}


async def session_workflow_states(session_ids: list[int]) -> dict[int, str]:
    """各会话在侧栏展示的工作流状态（同一会话取最需要关注的一个）。

    优先级：待你决定 > 已暂停 > 进行中 > 待确认。
    """
    if not session_ids:
        return {}
    async with async_session_factory() as s:
        rows = (
            await s.execute(
                select(WorkflowRun.session_id, WorkflowRun.status).where(
                    WorkflowRun.session_id.in_(session_ids),
                    WorkflowRun.parent_run_id.is_(None),
                    WorkflowRun.status.in_(tuple(_SESSION_STATE_OF)),
                )
            )
        ).all()
    out: dict[int, str] = {}
    for sid, status in rows:
        state = _SESSION_STATE_OF[status]
        current = out.get(int(sid))
        if current is None or _SESSION_STATE_PRIORITY.index(state) < _SESSION_STATE_PRIORITY.index(
            current
        ):
            out[int(sid)] = state
    return out


async def list_paused_workflows(session_id: int, limit: int = 5) -> list[dict[str, Any]]:
    """本会话中已暂停、可续跑的程序（供新一轮的上下文注入）。"""
    async with async_session_factory() as s:
        rows = (
            (
                await s.execute(
                    select(WorkflowRun)
                    .where(
                        WorkflowRun.session_id == session_id,
                        WorkflowRun.kind == "program",
                        WorkflowRun.status == "paused",
                    )
                    .order_by(WorkflowRun.id.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        items: list[dict[str, Any]] = []
        for row in rows:
            snap = await load_program(row.id)
            nodes = [n for f in (snap or {}).get("flows", []) for n in f["nodes"]]
            done = sum(1 for n in nodes if n["status"] in ("done", "skipped"))
            items.append(
                {
                    "program_id": row.id,
                    "title": row.title or "",
                    "progress": {"done": done, "total": len(nodes)},
                    "pause_kind": row.pause_kind or "",
                    "pause_reason": row.pause_reason or "",
                    "paused_at": row.paused_at or "",
                }
            )
        return items


async def list_pending_gates(session_id: int, limit: int = 3) -> list[dict[str, Any]]:
    """本会话中等待确认的步骤（plan-workflow-collab）：新一轮注入上下文，用户在输入框回复即可决定。"""
    from app.orchestration.workflow import report

    async with async_session_factory() as s:
        ids = (
            (
                await s.execute(
                    select(WorkflowRun.id)
                    .where(
                        WorkflowRun.session_id == session_id,
                        WorkflowRun.kind == "program",
                        WorkflowRun.status == "waiting_human",
                    )
                    .order_by(WorkflowRun.id.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
    items: list[dict[str, Any]] = []
    for program_id in ids:
        snap = await load_program(program_id)
        if snap is not None:
            items.extend(report.pending_gates(snap))
    return items


async def _progress_by_program(s, program_ids: list[int]) -> dict[int, dict[str, int]]:
    """各程序的步骤完成情况（done 含 skipped）；一次分组查询，避免逐个程序访问数据库。"""
    if not program_ids:
        return {}
    rows = (
        await s.execute(
            select(WorkflowRun.parent_run_id, WorkflowNode.status)
            .join(WorkflowNode, WorkflowNode.run_id == WorkflowRun.id)
            .where(WorkflowRun.parent_run_id.in_(program_ids))
        )
    ).all()
    out: dict[int, dict[str, int]] = {}
    for parent_id, status in rows:
        item = out.setdefault(parent_id, {"done": 0, "total": 0})
        item["total"] += 1
        if status in ("done", "skipped"):
            item["done"] += 1
    return out


async def list_programs(session_id: int) -> list[dict[str, Any]]:
    """会话内的程序列表（不含节点明细；每行附带 progress 摘要，供侧栏与面板列表使用）。"""
    async with async_session_factory() as s:
        rows = (
            (
                await s.execute(
                    select(WorkflowRun)
                    .where(WorkflowRun.session_id == session_id, WorkflowRun.kind == "program")
                    .order_by(WorkflowRun.id.desc())
                )
            )
            .scalars()
            .all()
        )
        progress = await _progress_by_program(s, [r.id for r in rows])
        return [
            {**_run_dict(r), "progress": progress.get(r.id, {"done": 0, "total": 0})} for r in rows
        ]


async def load_node_context(node_id: int) -> dict[str, Any] | None:
    """节点执行所需的上下文：节点本身、所属子工作流、程序、会话、工作区与上游产出。"""
    async with async_session_factory() as s:
        node = await s.get(WorkflowNode, node_id)
        if node is None:
            return None
        flow = await s.get(WorkflowRun, node.run_id)
        root = await s.get(WorkflowRun, flow.parent_run_id) if flow and flow.parent_run_id else None
        if flow is None or root is None:
            return None
        upstream_keys = list(node.inputs_json or [])
        upstream: dict[str, dict[str, Any]] = {}
        if upstream_keys:
            rows = (
                (
                    await s.execute(
                        select(WorkflowNode).where(
                            WorkflowNode.run_id == flow.id, WorkflowNode.node_key.in_(upstream_keys)
                        )
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                upstream[row.node_key] = {"kind": row.kind, "output": row.output_json or {}}
        return {
            "node": _node_dict(node),
            "flow_run_id": flow.id,
            "flow_key": flow.package_key or "",
            "program_id": root.id,
            "session_id": root.session_id,
            "workspace": (flow.workspace or root.workspace or "")
            if flow
            else (root.workspace or ""),
            "goal": root.goal or "",
            "turn_id": root.last_turn_id or root.origin_turn_id,
            "upstream": upstream,
        }


# ── 写入 ─────────────────────────────────────────────


async def create_program(
    *, session_id: int, plan: dict[str, Any], workspace: str, origin_turn_id: int | None
) -> int:
    """落库程序、子工作流与节点（初始为 draft），并写入第一版计划与创建事件。"""

    def _op(s):
        ts = now_iso()
        root = WorkflowRun(
            session_id=session_id,
            kind="program",
            title=plan["title"],
            goal=plan["goal"],
            rationale=plan.get("rationale", ""),
            based_on=plan.get("based_on"),
            status="draft",
            workspace=workspace,
            graph_json=plan,
            plan_rev=1,
            origin_turn_id=origin_turn_id,
            last_turn_id=origin_turn_id,
            updated_at=ts,
        )
        s.add(root)
        s.flush()
        for flow in plan["flows"]:
            flow_row = WorkflowRun(
                session_id=session_id,
                parent_run_id=root.id,
                kind="flow",
                package_key=flow["key"],
                title=flow.get("title") or flow["key"],
                status="draft",
                workspace="",
                isolation=flow.get("isolation") or settings.workflow_isolation_default,
                depends_on_json=list(flow.get("depends_on", [])),
                updated_at=ts,
            )
            s.add(flow_row)
            s.flush()
            for node in flow["nodes"]:
                s.add(
                    WorkflowNode(
                        run_id=flow_row.id,
                        node_key=node["key"],
                        title=node.get("title") or node["key"],
                        kind=node["kind"],
                        role=node.get("role"),
                        task=node.get("task", ""),
                        inputs_json=list(node.get("inputs", [])),
                        acceptance_json=list(node.get("acceptance", [])),
                        write_scope_json=list(node.get("write_scope", [])),
                        commands_json=list(node.get("commands", [])),
                        rework_to=node.get("rework_to"),
                        max_rework=node.get("max_rework"),
                        question=node.get("question"),
                        status="pending",
                        attempt_no=0,
                        rework_round=0,
                    )
                )
            if flow_row.isolation == "worktree":
                s.add(_integrate_node(flow_row.id, [n["key"] for n in flow["nodes"]]))
        s.add(WorkflowPlanRevision(run_id=root.id, rev=1, graph_json=plan, reason="初始计划"))
        s.add(
            WorkflowEvent(
                run_id=root.id,
                seq=1,
                type="workflow.log.appended",
                node_key=None,
                payload_json={"text": f"已创建工作流计划：{plan['title']}", "level": "info"},
            )
        )
        root.last_seq = 1
        s.commit()
        return root.id

    return await run_write_locked(_op, label="workflow.create_program")


async def update_run(run_id: int, **fields: Any) -> None:
    def _op(s):
        row = s.get(WorkflowRun, run_id)
        if row is None:
            return
        for key, value in fields.items():
            setattr(row, key, value)
        row.updated_at = now_iso()
        s.commit()

    await run_write_locked(_op, label="workflow.update_run")


async def update_node(node_id: int, **fields: Any) -> None:
    def _op(s):
        row = s.get(WorkflowNode, node_id)
        if row is None:
            return
        for key, value in fields.items():
            setattr(row, key, value)
        s.commit()

    await run_write_locked(_op, label="workflow.update_node")


async def bump_rework_total(program_id: int) -> None:
    def _op(s):
        row = s.get(WorkflowRun, program_id)
        if row is not None:
            row.rework_total = (row.rework_total or 0) + 1
            s.commit()

    await run_write_locked(_op, label="workflow.bump_rework")


async def list_node_attempts(
    program_id: int, flow_key: str, node_key: str
) -> list[dict[str, Any]] | None:
    """节点的尝试历史（按尝试序号）；节点不存在返回 None。"""
    async with async_session_factory() as s:
        flow = (
            await s.execute(
                select(WorkflowRun.id).where(
                    WorkflowRun.parent_run_id == program_id,
                    WorkflowRun.package_key == flow_key,
                )
            )
        ).scalar_one_or_none()
        if flow is None:
            return None
        node_id = (
            await s.execute(
                select(WorkflowNode.id).where(
                    WorkflowNode.run_id == flow, WorkflowNode.node_key == node_key
                )
            )
        ).scalar_one_or_none()
        if node_id is None:
            return None
        rows = (
            (
                await s.execute(
                    select(WorkflowAttempt)
                    .where(WorkflowAttempt.node_id == node_id)
                    .order_by(WorkflowAttempt.id.asc())
                )
            )
            .scalars()
            .all()
        )
        return [
            {
                "attempt_no": r.attempt_no,
                "mode": r.mode,
                "status": r.status,
                "reason": r.reason,
                "started_at": r.started_at,
                "ended_at": r.ended_at,
            }
            for r in rows
        ]


async def add_attempt(node_id: int, attempt_no: int, mode: str, agent_id: int | None = None) -> int:
    def _op(s):
        row = WorkflowAttempt(
            node_id=node_id,
            attempt_no=attempt_no,
            mode=mode,
            agent_id=agent_id,
            status="running",
            started_at=now_iso(),
        )
        s.add(row)
        s.flush()
        attempt_id = row.id
        s.commit()
        return attempt_id

    return await run_write_locked(_op, label="workflow.add_attempt")


async def finish_attempt(
    attempt_id: int,
    *,
    status: str,
    reason: str = "",
    tool_trace: list[dict[str, Any]] | None = None,
) -> None:
    def _op(s):
        row = s.get(WorkflowAttempt, attempt_id)
        if row is None:
            return
        row.status = status
        row.reason = (reason or "")[:300] or None
        row.ended_at = now_iso()
        row.tool_trace_json = tool_trace or None
        s.commit()

    await run_write_locked(_op, label="workflow.finish_attempt")


# 每个程序保留的最近事件条数（设计 §5.2）；更早的事件由回补接口不再提供
EVENT_RETENTION = 1000


async def emit_event(
    program_id: int, event_type: str, node_key: str | None, payload: dict[str, Any]
) -> dict[str, Any]:
    """追加一条程序级事件（分配 run 内单调 seq），返回可广播的信封。"""

    def _op(s):
        root = s.get(WorkflowRun, program_id)
        seq = (root.last_seq or 0) + 1
        root.last_seq = seq
        s.add(
            WorkflowEvent(
                run_id=program_id,
                seq=seq,
                type=event_type,
                node_key=node_key,
                payload_json=payload,
            )
        )
        if seq > EVENT_RETENTION:
            s.query(WorkflowEvent).filter(
                WorkflowEvent.run_id == program_id,
                WorkflowEvent.seq <= seq - EVENT_RETENTION,
            ).delete(synchronize_session=False)
        s.commit()
        return {"session_id": root.session_id, "seq": seq}

    meta = await run_write_locked(_op, label="workflow.emit")
    # 不带顶层 seq：顶层 seq 归会话级序号（ws 层分配并入补偿缓冲）。程序内序号放在 run_seq，
    # 否则会把会话的 lastSeq 错误地推高，断线补发时漏掉中间的会话事件。
    return {
        "session_id": meta["session_id"],
        "envelope": {
            "event": event_type,
            "payload": {
                "run_id": program_id,
                "node_key": node_key,
                "run_seq": meta["seq"],
                **payload,
            },
        },
    }
