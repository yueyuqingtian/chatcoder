"""工作流 API（设计 §5.9）：会话列表、程序快照与事件回补，以及开始 / 暂停 / 续跑 / 取消 / 闸门决定。

所有状态变更走运行时的单一入口（同一套校验与对账），前端的按钮与主代理的工具共用这些能力。
"""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.orchestration.workflow import presets, service, store
from app.orchestration.workflow.graph import parse_plan, validate_plan
from app.orchestration.workflow.runtime import (
    WorkflowLocalChangesError,
    WorkflowModelUnavailableError,
    WorkflowPlanInvalidError,
)
from app.orchestration.workflow.tools import validate_plan_for_run

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/workflows", tags=["workflows"])
session_router = APIRouter(prefix="/sessions", tags=["workflows"])


class ResumeIn(BaseModel):
    note: str = Field(default="", max_length=500)
    # 暂停期间工作区有外部改动时，用户确认后才以 accept_drift=true 续跑（§6.6）
    accept_drift: bool = False


class StartIn(BaseModel):
    # 主工作区有未提交的改动时，用户确认后才以 accept_local_changes=true 开始（§5A）
    accept_local_changes: bool = False
    # 用户改选共享工作区：子工作流直接在主工作区执行，能看到这些未提交的改动
    shared_workspace: bool = False


class GateIn(BaseModel):
    decision: str = Field(pattern="^(approve|rework|abort|include)$")
    feedback: str = Field(default="", max_length=800)


class CancelIn(BaseModel):
    # adjusted：草稿已按新的要求调整，由新计划替代（界面显示“已调整”）；user：用户取消
    reason: str = Field(default="user", pattern="^(user|adjusted)$")


class FlowTemplateIn(BaseModel):
    template: str = Field(min_length=1, max_length=120)  # 预设键，或 user:<键>
    flow: str = Field(min_length=1, max_length=40)  # 模板中的子工作流标识


class TemplateCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    description: str = Field(default="", max_length=200)


class RetryIn(BaseModel):
    # checkpoint 从断点继续（保留该步骤已完成的进度）；fresh 从头重做该步骤
    mode: str = Field(pattern="^(checkpoint|fresh)$")


async def _snapshot_or_404(program_id: int) -> dict:
    snap = await store.load_program(program_id)
    if snap is None:
        raise HTTPException(404, "工作流不存在")
    return snap


async def _run_action(program_id: int, action):
    await _snapshot_or_404(program_id)
    try:
        await action()
    except WorkflowModelUnavailableError as exc:
        # 结构化的 409：界面据此提示“模型暂时不可用，已保留进度”，暂停状态保持不变
        raise HTTPException(409, detail={"code": "model_unavailable", "message": str(exc)}) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return await store.load_program(program_id)


class DraftFromPresetIn(BaseModel):
    preset: str = Field(min_length=1, max_length=80)


async def _session_workspace(session_id: int) -> str | None:
    """会话的工作目录：有独立工作树优先，否则为所属项目的目录（与代理循环的解析一致）。"""
    from app.persistence.database import async_session_factory
    from app.persistence.models.message import Session as ChatSession
    from app.persistence.models.project import Project

    async with async_session_factory() as db:
        session = await db.get(ChatSession, session_id)
        if session is None:
            return None
        if session.worktree_path:
            return session.worktree_path
        project = await db.get(Project, session.project_id) if session.project_id else None
        return (project.path or None) if project is not None else None


@session_router.post("/{session_id}/workflows/draft")
async def create_draft_from_preset(session_id: int, body: DraftFromPresetIn) -> dict:
    """“从预设开始”：以预设骨架生成一个待确认的工作流，编辑后再开始（设计 §4.5 第 9 点）。"""
    plan = presets.preset_plan(body.preset)
    if plan is None:
        raise HTTPException(404, "没有这个预设")
    workspace = await _session_workspace(session_id)
    if not workspace:
        raise HTTPException(409, "当前会话没有关联的项目目录，无法创建工作流")
    try:
        program_id = await service.get_runtime().create_program(
            session_id=session_id, plan=plan, workspace=workspace
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return await store.load_program(program_id)


@session_router.get("/{session_id}/workflows")
async def list_session_workflows(session_id: int) -> list[dict]:
    return await store.list_programs(session_id)


@router.get("/limits")
async def get_limits() -> dict:
    return service.describe_limits()


@router.get("/pool")
async def get_pool() -> dict:
    """并发池占用（“并发 2/4 · 排队 1”）与跨会话的工作流计数（设置页概览）。"""
    return {**service.pool_snapshot(), "programs": await store.program_counts()}


class TemplateIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=300)


class TemplatePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=300)
    enabled: bool | None = None
    # 模板计划：与 plan_workflow 同一格式，保存前按同一套规则校验
    plan: dict | None = None


def _diagnostics(items) -> list[dict]:
    return [
        {"code": d.code, "path": d.path, "message": d.message, "suggestion": d.suggestion}
        for d in items
    ]


@router.get("/templates")
async def list_templates() -> list[dict]:
    """模板清单：内置预设（只读）在前，用户模板在后。"""
    return [*presets.list_presets(), *await store.list_user_templates()]


@router.post("/templates/{key}/copy")
async def copy_preset(key: str) -> dict:
    """把内置预设复制为用户模板，之后可以编辑名称与说明。"""
    meta = next((p for p in presets.list_presets() if p["key"] == key), None)
    if meta is None:
        raise HTTPException(404, "预设不存在")
    return await store.save_template(
        meta["plan"], name=f"{meta['name']}（副本）", description=meta["description"]
    )


@router.patch("/templates/{key}")
async def patch_template(key: str, body: TemplatePatch) -> dict:
    plan: dict | None = None
    if body.plan is not None:
        spec, diags = parse_plan(body.plan)
        if spec is None:
            raise HTTPException(422, detail={"errors": _diagnostics(diags)})
        result = validate_plan(spec)
        if not result.ok:
            raise HTTPException(422, detail={"errors": _diagnostics(result.errors)})
        plan = body.plan
    row = await store.update_user_template(
        key,
        name=body.name,
        description=body.description,
        enabled=body.enabled,
        plan=plan,
    )
    if row is None:
        raise HTTPException(404, "模板不存在")
    return row


@router.delete("/templates/{key}")
async def delete_template(key: str) -> dict:
    if not await store.delete_user_template(key):
        raise HTTPException(404, "模板不存在")
    return {"ok": True}


@router.post("/templates")
async def create_template(body: TemplateCreateIn) -> dict:
    """“新建模板”：从一个探索步骤起步，之后在模板编辑器中补全（设计 §7.8）。"""
    return await store.save_template(
        presets.blank_plan(body.name), name=body.name, description=body.description
    )


async def _flow_template_source(template_ref: str, flow_key: str) -> tuple[list[dict], str] | None:
    """模板中某个子工作流的步骤与展示名：预设或用户模板；找不到返回 None。"""
    if presets.is_user_ref(template_ref):
        row = await store.get_user_template(template_ref[len(presets.USER_PREFIX) :])
        if row is None:
            return None
        plan, template_name = row["plan"], row["name"]
    else:
        plan = presets.preset_plan(template_ref)
        if plan is None:
            return None
        template_name = next(
            (item["name"] for item in presets.list_presets() if item["key"] == template_ref),
            template_ref,
        )
    flow = next((f for f in plan.get("flows", []) if f.get("key") == flow_key), None)
    if flow is None:
        return None
    return list(flow.get("nodes") or []), f"{template_name} · {flow.get('title') or flow_key}"


@router.post("/{program_id}/flows/{flow_key}/template")
async def swap_flow_template(program_id: int, flow_key: str, body: FlowTemplateIn) -> dict:
    """草稿计划中把某个子工作流的模板从 A 换成 B（设计 §7.7），改写后经同一套校验。"""
    await _snapshot_or_404(program_id)
    source = await _flow_template_source(body.template, body.flow)
    if source is None:
        raise HTTPException(404, "模板或其中的子工作流不存在")
    nodes, label = source
    runtime = service.get_runtime()
    try:
        await runtime.replace_draft_flow(
            program_id, flow_key, nodes, label, validate=validate_plan_for_run
        )
    except WorkflowPlanInvalidError as exc:
        raise HTTPException(
            409, detail={"code": "plan_invalid", "message": exc.message, "errors": exc.errors}
        ) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return await store.load_program(program_id)


@router.get("/{program_id}")
async def get_program(program_id: int) -> dict:
    return await _snapshot_or_404(program_id)


@router.get("/{program_id}/events")
async def get_events(program_id: int, after_seq: int = 0) -> list[dict]:
    await _snapshot_or_404(program_id)
    return await store.list_events(program_id, after_seq)


@router.post("/{program_id}/start")
async def start_program(program_id: int, body: StartIn | None = None) -> dict:
    runtime = service.get_runtime()
    accept = bool(body and body.accept_local_changes)
    shared = bool(body and body.shared_workspace)
    await _snapshot_or_404(program_id)
    try:
        await runtime.start(program_id, accept_local_changes=accept, shared_workspace=shared)
    except WorkflowLocalChangesError as exc:
        # 结构化的 409：界面据此列出改动文件，由用户确认或先处理
        raise HTTPException(409, detail={"code": "local_changes", "files": exc.files}) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return await store.load_program(program_id)


@router.post("/{program_id}/pause")
async def pause_program(program_id: int) -> dict:
    runtime = service.get_runtime()
    return await _run_action(program_id, lambda: runtime.pause(program_id))


@router.post("/{program_id}/template")
async def save_as_template(program_id: int, body: TemplateIn) -> dict:
    """把一次运行的计划存为用户模板（设计 §4.5 第 8 点：运行结束后由用户决定是否沉淀）。"""
    snap = await _snapshot_or_404(program_id)
    plan = snap["run"]["graph"]
    if not plan:
        raise HTTPException(409, "该工作流没有可保存的计划")
    return await store.save_template(
        presets.to_template_plan(plan), name=body.name, description=body.description
    )


@router.get("/{program_id}/drift")
async def get_drift(program_id: int) -> dict:
    """续跑前的工作区报告：写入范围内被外部改动的文件；HEAD 变化（用户提交过）单独给出。"""
    await _snapshot_or_404(program_id)
    report = await service.get_runtime().drift_report(program_id)
    return {"files": report["files"], "head": report["head"]}


@router.get("/{program_id}/drift/diff")
async def get_drift_diff(program_id: int, path: str) -> dict:
    """某个改动文件自暂停以来的差异；只接受改动清单中的文件。"""
    await _snapshot_or_404(program_id)
    try:
        return await service.get_runtime().drift_diff(program_id, path)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/{program_id}/resume")
async def resume_program(program_id: int, body: ResumeIn) -> dict:
    runtime = service.get_runtime()
    return await _run_action(
        program_id,
        lambda: runtime.resume(program_id, body.note, accept_drift=body.accept_drift),
    )


@router.post("/{program_id}/cancel")
async def cancel_program(program_id: int, body: CancelIn | None = None) -> dict:
    runtime = service.get_runtime()
    reason = body.reason if body is not None else "user"
    return await _run_action(program_id, lambda: runtime.cancel(program_id, reason=reason))


@router.post("/{program_id}/gates/{flow_key}/{node_key}")
async def decide_gate(program_id: int, flow_key: str, node_key: str, body: GateIn) -> dict:
    runtime = service.get_runtime()
    return await _run_action(
        program_id,
        lambda: runtime.decide_gate(program_id, flow_key, node_key, body.decision, body.feedback),
    )


@router.get("/{program_id}/nodes/{flow_key}/{node_key}/attempts")
async def list_node_attempts(program_id: int, flow_key: str, node_key: str) -> list[dict]:
    """节点的尝试历史（设计 §7.6）。"""
    await _snapshot_or_404(program_id)
    attempts = await store.list_node_attempts(program_id, flow_key, node_key)
    if attempts is None:
        raise HTTPException(404, "节点不存在")
    return attempts


class NoteIn(BaseModel):
    content: str = Field(min_length=1, max_length=800)


@router.post("/{program_id}/nodes/{flow_key}/{node_key}/note")
async def add_node_note(program_id: int, flow_key: str, node_key: str, body: NoteIn) -> dict:
    """运行中的步骤补充说明（设计 §7.6）：经子代理的指令通道，下一次模型调用前送达。"""
    runtime = service.get_runtime()
    await _snapshot_or_404(program_id)
    try:
        await runtime.add_node_note(program_id, flow_key, node_key, body.content)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True}


@router.post("/{program_id}/nodes/{flow_key}/{node_key}/retry")
async def retry_node(program_id: int, flow_key: str, node_key: str, body: RetryIn) -> dict:
    runtime = service.get_runtime()
    return await _run_action(
        program_id,
        lambda: runtime.retry_node(program_id, flow_key, node_key, body.mode),
    )
