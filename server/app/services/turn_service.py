"""轮次（turn）服务：创建、状态流转、查询。"""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.turn import Turn


async def create_turn(db: AsyncSession, *, session_id: int,
                      user_message_id: int | None = None) -> int:
    """创建 turn（写引擎单写线程，无锁单写者），返回 turn id。"""
    from app.persistence.database import run_write_locked

    def patch(s):
        # 显式写入开始时间，保证 create_turn 的响应立即包含可计时的时间戳。
        turn = Turn(
            session_id=session_id,
            user_message_id=user_message_id,
            status="running",
            started_at=datetime.now(timezone.utc).isoformat(),
        )
        s.add(turn)
        s.flush()
        tid = turn.id
        s.commit()
        return tid

    return await run_write_locked(patch, label="turn.create")


async def get_turn(db: AsyncSession, turn_id: int) -> Turn | None:
    return await db.get(Turn, turn_id)


async def list_turns(db: AsyncSession, session_id: int, limit: int = 50) -> list[Turn]:
    res = await db.execute(
        select(Turn).where(Turn.session_id == session_id)
        .order_by(Turn.id.desc()).limit(limit)
    )
    return list(res.scalars().all())


async def patch_turn(turn_id: int, **fields) -> None:
    """写引擎单写线程提交 Turn 字段补丁（无锁单写者；status 之外 None 跳过）。"""
    from app.persistence.database import run_write_locked

    def patch(s):
        t = s.get(Turn, turn_id)
        if t is None:
            return
        for k, v in fields.items():
            if v is not None or k == "status":
                setattr(t, k, v)
        s.commit()

    await run_write_locked(patch, label=f"turn.patch.{turn_id}")


async def update_turn_status(db: AsyncSession, turn_id: int, status: str,
                             summary: str | None = None, token_usage: int | None = None,
                             completed: bool = False) -> str | None:
    """更新 turn 状态（写经 WriteEngine 单写线程，无锁单写者）。

    权威读-改-写在写线程内基于最新 DB 状态进行（get 不到返回 None）；`db` 参数
    保留为兼容（调用方仍传 async 会话），本函数不再用 db 直接写入。
    返回写入后的 status（None=未找到）。
    """
    from datetime import datetime, timezone

    from app.persistence.database import run_write_locked

    def patch(s):
        from app.persistence.models.turn import Turn
        t = s.get(Turn, turn_id)
        if t is None:
            return None
        t.status = status
        if summary is not None:
            t.summary = summary
        if token_usage is not None:
            t.token_usage = token_usage
        if completed:
            # 必须写真实时间戳：str(func.now()) 会把 SQL 表达式字面化为 "now()"，
            # 前端解析失败导致"已工作 0 秒"。
            t.completed_at = datetime.now(timezone.utc).isoformat()
        s.commit()
        return t.status

    return await run_write_locked(patch, label=f"turn.status.{turn_id}")
