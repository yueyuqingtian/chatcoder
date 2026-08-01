"""轮次（turn）：一次用户消息的执行单元。"""
from sqlalchemy import BigInteger, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.database import Base


class Turn(Base):
    __tablename__ = "turns"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("sessions.id"))
    user_message_id: Mapped[int | None] = mapped_column(BigInteger)  # 触发本 turn 的用户消息
    status: Mapped[str] = mapped_column(String(20), default="running")  # running/completed/failed/cancelled/interrupted/rolled_back
    summary: Mapped[str | None]  # turn 结束时主代理产出摘要
    token_usage: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[str] = mapped_column(server_default=func.now())
    completed_at: Mapped[str | None]
