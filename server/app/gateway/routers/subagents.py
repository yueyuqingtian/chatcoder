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
    is_active: bool = True


def _to_out(p: SubagentProfile) -> dict:
    return {
        "id": p.id, "name": p.name, "description": p.description,
        "tools_whitelist": p.tools_whitelist, "model_id": p.model_id,
        "system_prompt": p.system_prompt, "is_active": p.is_active,
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
            system_prompt=body.system_prompt, is_active=body.is_active,
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
async def update_profile(profile_id: int, body: SubagentProfileIn,
                         db: AsyncSession = Depends(get_db)):
    from app.persistence.database import run_write_locked

    def _p(s):
        p = s.get(SubagentProfile, profile_id)
        if p is None:
            return False
        if body.name and body.name.strip():
            p.name = body.name.strip()
        p.description = body.description
        p.tools_whitelist = body.tools_whitelist
        p.model_id = body.model_id
        p.system_prompt = body.system_prompt
        p.is_active = body.is_active
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
