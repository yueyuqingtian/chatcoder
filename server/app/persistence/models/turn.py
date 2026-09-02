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
    # plan-644: 计划模式字段持久化--本 turn 产出的方案文档路径（相对工作区）
    # 与其生命周期状态。plan_status 为 None 表示本 turn 与计划流程无关。
    plan_doc_path: Mapped[str | None] = mapped_column(String(512))
    # proposed(待确认) / confirmed(已确认执行) / done(执行完成) / cancelled(已取消) / superseded(被新方案取代)
    plan_status: Mapped[str | None] = mapped_column(String(20))
