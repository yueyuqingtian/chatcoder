"""v2.2 (对齐 zcode 3.13): 子代理类型（SubagentProfile）管理 API。"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from app.persistence.database import get_db
from app.persistence.models.subagent_profile import SubagentProfile

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/subagents", tags=["subagents"])


class SubagentProfileIn(BaseModel):
    name: str
    description: str | None = None
    tools_whitelist: list[str] | None = None
    model_id: int | None = None
    system_prompt: str | None = None
    # plan-330-1648 M2: 思考深度档位（None = 跟随会话本轮档位）
    reasoning_effort: str | None = None
    is_active: bool = True


class SubagentProfilePatch(BaseModel):
    """S10（plan-41-197）：部分更新载荷——列表页的启用开关只传 is_active。

    此前 PATCH 复用全量 schema（name 必填），只传 is_active 会被 422 拒绝，
    表现为“列表里的启用滑块点击无法修改，只能在弹窗内改”。所有字段可选，
    未出现的字段保持原值（显式传 null 表示清除覆盖，如 reasoning_effort）。
    """
    name: str | None = None
    description: str | None = None
    tools_whitelist: list[str] | None = None
    model_id: int | None = None
    system_prompt: str | None = None
    reasoning_effort: str | None = None
    is_active: bool | None = None


def _to_out(p: SubagentProfile) -> dict:
    return {
        "id": p.id, "name": p.name, "description": p.description,
        "tools_whitelist": p.tools_whitelist, "model_id": p.model_id,
        "system_prompt": p.system_prompt,
        "reasoning_effort": getattr(p, "reasoning_effort", None),
        "is_active": p.is_active,
    }


@router.get("", response_model=list[dict])
async def list_profiles(db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(SubagentProfile).order_by(SubagentProfile.id.asc()))
    return [_to_out(p) for p in res.scalars().all()]


@router.post("", response_model=dict)
async def create_profile(body: SubagentProfileIn, db: AsyncSession = Depends(get_db)):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "名称不能为空")
    exists = (await db.execute(
        select(SubagentProfile).where(SubagentProfile.name == name)
    )).scalars().first()
    if exists:
        raise HTTPException(409, "同名子代理类型已存在")
    from app.persistence.database import run_write_locked

    def _p(s):
        obj = SubagentProfile(
            name=name, description=body.description,
            tools_whitelist=body.tools_whitelist, model_id=body.model_id,
            system_prompt=body.system_prompt, reasoning_effort=body.reasoning_effort,
            is_active=body.is_active,
        )
        s.add(obj)
        s.flush()
        oid = obj.id
        s.commit()
        return oid

    pid = await run_write_locked(_p, label="subagent.profile.create")
    created = (await db.execute(select(SubagentProfile).where(SubagentProfile.id == pid))).scalars().first()
    return _to_out(created)


@router.patch("/{profile_id}", response_model=dict)
async def update_profile(profile_id: int, body: SubagentProfilePatch,
                         db: AsyncSession = Depends(get_db)):
    from app.persistence.database import run_write_locked

    # S10（plan-41-197）：只更新请求里显式出现的字段（exclude_unset）——
    # 列表开关只传 is_active 时不再覆盖其它字段，也不再触发 name 必填校验。
    provided = body.model_dump(exclude_unset=True)

    def _p(s):
        p = s.get(SubagentProfile, profile_id)
        if p is None:
            return False
        for field, value in provided.items():
            if field == "name":
                if value and str(value).strip():
                    p.name = str(value).strip()
                continue
            setattr(p, field, value)
        s.commit()
        return True

    ok = await run_write_locked(_p, label=f"subagent.profile.update.{profile_id}")
    if not ok:
        raise HTTPException(404, "子代理类型不存在")
    p = (await db.execute(select(SubagentProfile).where(SubagentProfile.id == profile_id))).scalars().first()
    return _to_out(p)


@router.delete("/{profile_id}", response_model=dict)
async def delete_profile(profile_id: int, db: AsyncSession = Depends(get_db)):
    from app.persistence.database import run_write_locked

    def _p(s):
        p = s.get(SubagentProfile, profile_id)
        if p is None:
            return False
        s.delete(p)
        s.commit()
        return True

    ok = await run_write_locked(_p, label=f"subagent.profile.delete.{profile_id}")
    if not ok:
        raise HTTPException(404, "子代理类型不存在")
    return {"ok": True}
