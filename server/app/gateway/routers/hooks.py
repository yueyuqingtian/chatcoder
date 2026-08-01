"""钩子配置路由（D5）。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.gateway.schemas import HookConfigCreate, HookConfigOut
from app.persistence.database import get_db
from app.services import hook_service

router = APIRouter(prefix="/hooks", tags=["hooks"])


@router.get("", response_model=list[HookConfigOut])
async def list_hooks(db: AsyncSession = Depends(get_db)):
    return await hook_service.list_hooks(db)


@router.post("", response_model=HookConfigOut)
async def create_hook(body: HookConfigCreate, db: AsyncSession = Depends(get_db)):
    try:
        hook = await hook_service.create_hook(
            db, event=body.event, command=body.command,
            matcher=body.matcher, enabled=body.enabled,
        )
        await db.commit()
        return hook
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.patch("/{hook_id}", response_model=HookConfigOut)
async def update_hook(hook_id: int, body: dict, db: AsyncSession = Depends(get_db)):
    hook = await hook_service.update_hook(db, hook_id, **body)
    if hook is None:
        raise HTTPException(404, "钩子不存在")
    await db.commit()
    return hook


@router.delete("/{hook_id}", response_model=dict)
async def delete_hook(hook_id: int, db: AsyncSession = Depends(get_db)):
    ok = await hook_service.delete_hook(db, hook_id)
    if not ok:
        raise HTTPException(404, "钩子不存在")
    await db.commit()
    return {"ok": True}
