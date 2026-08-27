"""轮次（turn）服务：创建、状态流转、查询。"""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.turn import Turn


async def create_turn(db: AsyncSession, *, session_id: int,
                      user_message_id: int | None = None) -> Turn:
    # 显式写入开始时间，保证 create_turn 的响应立即包含可计时的时间戳。
    turn = Turn(
        session_id=session_id,
        user_message_id=user_message_id,
        status="running",
        started_at=datetime.now(timezone.utc).isoformat(),
    )
    db.add(turn)
    await db.flush()
    return turn


async def get_turn(db: AsyncSession, turn_id: int) -> Turn | None:
    return await db.get(Turn, turn_id)


async def list_turns(db: AsyncSession, session_id: int, limit: int = 50) -> list[Turn]:
    res = await db.execute(
        select(Turn).where(Turn.session_id == session_id)
        .order_by(Turn.id.desc()).limit(limit)
    )
    return list(res.scalars().all())


async def update_turn_status(db: AsyncSession, turn_id: int, status: str,
                             summary: str | None = None, token_usage: int | None = None,
                             completed: bool = False) -> Turn | None:
    turn = await db.get(Turn, turn_id)
    if turn is None:
        return None
    turn.status = status
    if summary is not None:
        turn.summary = summary
    if token_usage is not None:
        turn.token_usage = token_usage
    if completed:
        # 必须写真实时间戳：str(func.now()) 会把 SQL 表达式字面化为 "now()"，
        # 前端解析失败导致"已工作 0 秒"。
        turn.completed_at = datetime.now(timezone.utc).isoformat()
    await db.flush()
    return turn
