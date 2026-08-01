"""定时任务路由。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.gateway.schemas import (ScheduledTaskCreate, ScheduledTaskOut,
                                 ScheduledTaskUpdate)
from app.persistence.database import get_db
from app.services import scheduled_service

router = APIRouter(prefix="/scheduled-tasks", tags=["scheduled"])


@router.get("", response_model=list[ScheduledTaskOut])
async def list_tasks(db: AsyncSession = Depends(get_db)):
    return await scheduled_service.list_scheduled(db)


@router.post("", response_model=ScheduledTaskOut)
async def create_task(body: ScheduledTaskCreate, db: AsyncSession = Depends(get_db)):
    try:
        st = await scheduled_service.create_scheduled(
            db, session_id=body.session_id, name=body.name,
            cron=body.cron, prompt=body.prompt,
        )
        await db.commit()
        return st
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.patch("/{task_id}", response_model=ScheduledTaskOut)
async def update_task(task_id: int, body: ScheduledTaskUpdate, db: AsyncSession = Depends(get_db)):
    try:
        st = await scheduled_service.update_scheduled(
            db, task_id, name=body.name, cron=body.cron,
            prompt=body.prompt, enabled=body.enabled,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    if st is None:
        raise HTTPException(404, "定时任务不存在")
    await db.commit()
    return st


@router.delete("/{task_id}", response_model=dict)
async def delete_task(task_id: int, db: AsyncSession = Depends(get_db)):
    ok = await scheduled_service.delete_scheduled(db, task_id)
    if not ok:
        raise HTTPException(404, "定时任务不存在")
    await db.commit()
    return {"ok": True}
