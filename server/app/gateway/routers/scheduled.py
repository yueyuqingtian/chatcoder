"""定时任务路由（plan-230-1144 M1.1）。

新增：非法 cron 统一 422、`POST /{id}/run` 手动试跑、`GET /{id}/preview` 排程预览
（返回接下来 N 次触发时刻，让用户在保存前就能确认表达式写对了没有）。
"""
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.gateway.schemas import (ScheduledTaskCreate, ScheduledTaskOut,
                                 ScheduledTaskUpdate)
from app.persistence.database import get_db
from app.services import scheduled_service, scheduler_loop

router = APIRouter(prefix="/scheduled-tasks", tags=["scheduled"])


@router.get("", response_model=list[ScheduledTaskOut])
async def list_tasks(db: AsyncSession = Depends(get_db)):
    return await scheduled_service.list_scheduled(db)


@router.post("", response_model=ScheduledTaskOut)
async def create_task(body: ScheduledTaskCreate, db: AsyncSession = Depends(get_db)):
    try:
        sid = await scheduled_service.create_scheduled(
            db, session_id=body.session_id, name=body.name,
            cron=body.cron, prompt=body.prompt,
        )
        if body.missed_policy and body.missed_policy != "skip":
            await scheduled_service.update_scheduled(db, sid, missed_policy=body.missed_policy)
    except scheduled_service.CronParseError as e:
        raise HTTPException(422, f"非法 cron 表达式: {e}")
    return await scheduled_service.get_scheduled(db, sid)


@router.patch("/{task_id}", response_model=ScheduledTaskOut)
async def update_task(task_id: int, body: ScheduledTaskUpdate, db: AsyncSession = Depends(get_db)):
    try:
        ok = await scheduled_service.update_scheduled(
            db, task_id, name=body.name, cron=body.cron,
            prompt=body.prompt, enabled=body.enabled,
            missed_policy=body.missed_policy,
        )
    except scheduled_service.CronParseError as e:
        raise HTTPException(422, f"非法 cron 表达式: {e}")
    if not ok:
        raise HTTPException(404, "定时任务不存在")
    return await scheduled_service.get_scheduled(db, task_id)


@router.delete("/{task_id}", response_model=dict)
async def delete_task(task_id: int, db: AsyncSession = Depends(get_db)):
    ok = await scheduled_service.delete_scheduled(db, task_id)
    if not ok:
        raise HTTPException(404, "定时任务不存在")
    return {"ok": True}


@router.post("/{task_id}/run", response_model=dict)
async def run_task_now(task_id: int, db: AsyncSession = Depends(get_db)):
    """立即试跑一次，不影响既有排程。"""
    st = await scheduled_service.get_scheduled(db, task_id)
    if st is None:
        raise HTTPException(404, "定时任务不存在")
    try:
        return await scheduler_loop.run_task_now(task_id)
    except KeyError as e:
        raise HTTPException(404, str(e))


@router.get("/{task_id}/preview", response_model=dict)
async def preview_schedule(task_id: int, count: int = 5, db: AsyncSession = Depends(get_db)):
    """预览接下来 N 次触发时刻（校验表达式是否符合预期）。"""
    st = await scheduled_service.get_scheduled(db, task_id)
    if st is None:
        raise HTTPException(404, "定时任务不存在")
    runs: list[str] = []
    cursor = scheduled_service._local_now()
    for _ in range(max(1, min(count, 20))):
        nxt = scheduled_service.compute_next_run(st.cron, cursor)
        if nxt is None:
            break
        runs.append(nxt.astimezone().isoformat())
        cursor = nxt + timedelta(minutes=1)
    return {"cron": st.cron, "next_runs": runs}


@router.get("/meta/validate", response_model=dict)
async def validate_cron(cron: str):
    """表单实时校验：返回是否合法 + 下次触发时刻。"""
    try:
        spec = scheduled_service.parse_cron(cron)
    except scheduled_service.CronParseError as e:
        return {"valid": False, "error": str(e)}
    nxt = scheduled_service.compute_next_run(cron)
    return {
        "valid": True,
        "next_run_at": nxt.astimezone().isoformat() if nxt else None,
        "never_fires": nxt is None,
        "fields": {
            "minutes": sorted(spec.minutes)[:8],
            "hours": sorted(spec.hours)[:8],
            "days_of_month": sorted(spec.days_of_month)[:8],
            "months": sorted(spec.months)[:8],
            "days_of_week": sorted(spec.days_of_week),
        },
    }
