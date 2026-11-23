"""智能体团队档案接口（plan-20261123-agent-team）。"""
import logging
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.orchestration.agent_team import BUILTINS, member_of
from app.persistence.database import get_db, run_write_locked
from app.persistence.models.model_reg import Model, Provider
from app.persistence.models.subagent_profile import SubagentProfile

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/subagents", tags=["subagents"])
ExecutionMode = Literal["readonly", "agent"]


class SubagentProfileIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    description: str | None = Field(default=None, max_length=300)
    model_id: int | None = Field(default=None, gt=0)
    system_prompt: str | None = Field(default=None, max_length=100_000)
    reasoning_effort: str | None = Field(default=None, max_length=20)
    execution_mode: ExecutionMode | None = None
    is_active: bool | None = None


class SubagentProfilePatch(SubagentProfileIn):
    revision: int | None = Field(default=None, ge=0)
    reset: Literal["prompt", "all"] | None = None


def _to_out(row: SubagentProfile) -> dict:
    builtin = BUILTINS.get(row.name)
    return {
        "id": row.id, "name": row.name,
        "display_name": row.display_name or (builtin[0] if builtin else row.name),
        "description": row.description,
        "model_id": row.model_id, "reasoning_effort": row.reasoning_effort,
        "execution_mode": builtin[2] if builtin else (row.execution_mode or "agent"),
        "is_active": row.is_active, "builtin": builtin is not None,
        "revision": row.revision,
    }


def _detail(row: SubagentProfile) -> dict:
    from app.orchestration.context_manager import subagent_prompt_preview

    return {**_to_out(row), **subagent_prompt_preview(member_of(row))}


def _get(db, profile_id: int) -> SubagentProfile:
    row = db.get(SubagentProfile, profile_id)
    if row is None:
        raise HTTPException(404, "智能体不存在")
    return row


def _apply(db, row: SubagentProfile, fields: dict) -> None:
    if "name" in fields:
        display_name = (fields.pop("name") or "").strip()
        if not display_name:
            raise HTTPException(422, "请输入智能体名称")
        row.display_name = display_name
    if "execution_mode" in fields:
        mode = fields["execution_mode"]
        if mode is None or (row.name in BUILTINS and mode != BUILTINS[row.name][2]):
            raise HTTPException(422, "此智能体不能使用所选执行模式")
    if "is_active" in fields and fields["is_active"] is None:
        raise HTTPException(422, "请选择成员启用状态")
    if "model_id" in fields or "reasoning_effort" in fields:
        model_id = fields.get("model_id", row.model_id)
        effort = fields.get("reasoning_effort", row.reasoning_effort)
        model = db.get(Model, model_id) if model_id is not None else None
        provider = db.get(Provider, model.provider_id) if model is not None and model.provider_id else None
        if model_id is not None and (model is None or not model.is_active
                                     or provider is not None and not provider.is_active):
            raise HTTPException(422, "所选模型不可用")
        if model is not None and effort is not None and effort not in (model.reasoning_efforts or []):
            raise HTTPException(422, "所选模型不支持此思考深度")
    for key, value in fields.items():
        setattr(row, key, value)


@router.get("")
async def list_profiles(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(SubagentProfile).options(defer(SubagentProfile.system_prompt))
                              .order_by(SubagentProfile.id))
    return [_to_out(row) for row in result.scalars()]


@router.get("/{profile_id}")
async def get_profile(profile_id: int, db: AsyncSession = Depends(get_db)):
    row = await db.get(SubagentProfile, profile_id)
    if row is None:
        raise HTTPException(404, "智能体不存在")
    return _detail(row)


@router.post("", status_code=201)
async def create_profile(body: SubagentProfileIn):
    def create(db):
        row = SubagentProfile(name=f"member-{uuid4().hex}", execution_mode="agent", is_active=True)
        fields = body.model_dump(exclude_unset=True)
        fields.setdefault("name", None)
        _apply(db, row, fields)
        db.add(row)
        db.flush()
        result = _detail(row)
        db.commit()
        return result

    return await run_write_locked(create, label="subagent.profile.create")


@router.patch("/{profile_id}")
async def update_profile(profile_id: int, body: SubagentProfilePatch):
    def save(db):
        row = _get(db, profile_id)
        changes = body.model_dump(exclude_unset=True, exclude={"revision", "reset"})
        expected = body.revision if body.revision is not None else row.revision
        changed = db.execute(update(SubagentProfile)
                             .where(SubagentProfile.id == profile_id, SubagentProfile.revision == expected)
                             .values(revision=SubagentProfile.revision + 1))
        if changed.rowcount != 1:
            raise HTTPException(409, "此智能体已在其他窗口更新")
        if body.reset == "all":
            if row.name not in BUILTINS:
                raise HTTPException(422, "仅内置智能体可以恢复整项默认配置")
            name, description, mode = BUILTINS[row.name]
            changes.update(name=name, description=description, execution_mode=mode,
                           model_id=None, reasoning_effort=None, is_active=True)
        if body.reset:
            changes["system_prompt"] = None
        _apply(db, row, changes)
        db.flush()
        result = _to_out(row)
        db.commit()
        return result

    return await run_write_locked(save, label="subagent.profile.update")


@router.delete("/{profile_id}")
async def delete_profile(profile_id: int):
    def remove(db):
        row = _get(db, profile_id)
        if row.name in BUILTINS:
            raise HTTPException(422, "内置智能体无法删除")
        db.delete(row)
        db.commit()
        return {"ok": True}

    return await run_write_locked(remove, label="subagent.profile.delete")


# ── plan-474-2105：运行时并发上限（只读快照）─────────────────────────
# 路径必须是 /api/runtime/limits，不能挂在 /subagents 前缀下，
# 否则会变成 /api/subagents/runtime/limits；因此单独建一个 router。
limits_router = APIRouter(prefix="/runtime", tags=["runtime"])


@limits_router.get("/limits")
async def runtime_limits():
    """单会话并发上限与当前占用（只读内存快照，诊断用）。

    plan-549-2281：子会话的创建与控制已下线，`max_subsessions_per_parent` 配置项已删除；
    `subsessions_running` 字段保留——存量历史子会话仍可能在跑，该计数是诊断它们占用的
    唯一入口（前端当前未消费，左栏「运行中」用的是 SessionOut.has_running，与此无关）。
    """
    from app.core.config import settings
    from app.orchestration.subagent import peek_subagent_manager
    from app.services import child_session_service

    sessions: list[dict] = []
    try:
        counts = await child_session_service.list_running_counts_by_parent()
        for root_id, subsessions_running in counts.items():
            manager = peek_subagent_manager(root_id)
            sessions.append({
                "session_id": root_id,
                "subagents_running": manager.pending_count() if manager is not None else 0,
                "subsessions_running": subsessions_running,
            })
    except Exception as exc:  # 展示型接口：读快照失败也返回上限，避免前端整块报错
        logger.warning("读取运行时并发占用失败: %s", exc)
        sessions = []
    return {
        "max_concurrent_subagents": settings.max_concurrent_subagents,
        "sessions": sessions,
    }
