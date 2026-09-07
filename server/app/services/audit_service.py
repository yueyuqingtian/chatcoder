"""审计日志服务（D19）。"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.audit import AuditLog


async def log(db: AsyncSession, *, action: str, session_id: int | None = None,
              turn_id: int | None = None, detail: dict | None = None) -> int:
    """写审计日志（经 WriteEngine 单写线程，无锁单写者）。返回 entry id。"""
    from app.persistence.database import run_write_locked

    def patch(s):
        from app.persistence.models.audit import AuditLog
        entry = AuditLog(action=action, session_id=session_id, turn_id=turn_id, detail=detail)
        s.add(entry)
        s.flush()
        eid = entry.id
        s.commit()
        return eid

    return await run_write_locked(patch, label="audit.log")


async def list_logs(db: AsyncSession, session_id: int | None = None, limit: int = 200) -> list[AuditLog]:
    stmt = select(AuditLog).order_by(AuditLog.id.desc())
    if session_id is not None:
        stmt = stmt.where(AuditLog.session_id == session_id)
    res = await db.execute(stmt.limit(limit))
    return list(res.scalars().all())
