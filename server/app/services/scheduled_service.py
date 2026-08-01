"""定时任务 CRUD 与 cron 解析。"""
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.scheduled import ScheduledTask


def parse_cron(cron: str) -> list[int] | None:
    """解析 5 段 cron 的分钟/小时字段（用于下次运行估算）。

    返回 [minute, hour]；非法返回 None。day/month/dow 仅校验非空。
    """
    parts = cron.strip().split()
    if len(parts) != 5:
        return None
    minute, hour = parts[0], parts[1]
    if not re.fullmatch(r"(\d{1,2}|\*|/\d{1,2}|[0-9,\-*/]+)", minute):
        return None
    if not re.fullmatch(r"(\d{1,2}|\*|/\d{1,2}|[0-9,\-*/]+)", hour):
        return None
    try:
        m = 0 if minute == "*" else int(minute.split("/")[0])
        h = 0 if hour == "*" else int(hour.split("/")[0])
    except ValueError:
        return None
    return [m, h]


async def create_scheduled(db: AsyncSession, *, session_id: int, name: str,
                           cron: str, prompt: str) -> ScheduledTask:
    if parse_cron(cron) is None:
        raise ValueError(f"非法 cron 表达式: {cron}")
    st = ScheduledTask(session_id=session_id, name=name, cron=cron, prompt=prompt)
    db.add(st)
    await db.flush()
    return st


async def list_scheduled(db: AsyncSession) -> list[ScheduledTask]:
    res = await db.execute(select(ScheduledTask).order_by(ScheduledTask.id))
    return list(res.scalars().all())


async def get_scheduled(db: AsyncSession, task_id: int) -> ScheduledTask | None:
    return await db.get(ScheduledTask, task_id)


async def update_scheduled(db: AsyncSession, task_id: int, **kwargs) -> ScheduledTask | None:
    st = await db.get(ScheduledTask, task_id)
    if st is None:
        return None
    if kwargs.get("cron") and parse_cron(kwargs["cron"]) is None:
        raise ValueError(f"非法 cron 表达式: {kwargs['cron']}")
    for k, v in kwargs.items():
        if v is not None:
            setattr(st, k, v)
    await db.flush()
    return st


async def delete_scheduled(db: AsyncSession, task_id: int) -> bool:
    st = await db.get(ScheduledTask, task_id)
    if st is None:
        return False
    await db.delete(st)
    await db.flush()
    return True
