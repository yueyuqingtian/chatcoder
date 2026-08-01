"""配置 profile 路由（D6）。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.gateway.schemas import (ConfigProfileCreate, ConfigProfileOut,
                                 ConfigProfileUpdate)
from app.persistence.database import get_db
from app.services import config_service

router = APIRouter(prefix="/profiles", tags=["profiles"])


@router.get("", response_model=list[ConfigProfileOut])
async def list_profiles(project_id: int | None = None, db: AsyncSession = Depends(get_db)):
    return await config_service.list_profiles(db, project_id)


@router.post("", response_model=ConfigProfileOut)
async def create_profile(body: ConfigProfileCreate, db: AsyncSession = Depends(get_db)):
    profile = await config_service.create_profile(
        db, name=body.name, scope=body.scope, project_id=body.project_id, data=body.data,
    )
    await db.commit()
    return profile


@router.patch("/{profile_id}", response_model=ConfigProfileOut)
async def update_profile(profile_id: int, body: ConfigProfileUpdate, db: AsyncSession = Depends(get_db)):
    profile = await config_service.update_profile(
        db, profile_id, data=body.data, is_active=body.is_active,
    )
    if profile is None:
        raise HTTPException(404, "profile 不存在")
    await db.commit()
    return profile


@router.delete("/{profile_id}", response_model=dict)
async def delete_profile(profile_id: int, db: AsyncSession = Depends(get_db)):
    ok = await config_service.delete_profile(db, profile_id)
    if not ok:
        raise HTTPException(404, "profile 不存在")
    await db.commit()
    return {"ok": True}


@router.post("/sessions/{session_id}/profile", response_model=dict)
async def switch_session_profile(session_id: int, profile_id: int, db: AsyncSession = Depends(get_db)):
    """将会话激活指定 profile。"""
    from app.gateway.routers.sessions import _to_out
    from app.orchestration.agent_events import broadcast
    profile = await config_service.update_profile(db, profile_id, is_active=True)
    if profile is None:
        raise HTTPException(404, "profile 不存在")
    await db.commit()
    await broadcast(session_id, {
        "event": "config.changed",
        "payload": {"profile_id": profile_id, "changed_keys": list((profile.data or {}).keys())},
    })
    return {"ok": True, "profile_id": profile_id}
