"""定时任务。"""
from sqlalchemy import BigInteger, Boolean, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.database import Base


class ScheduledTask(Base):
    __tablename__ = "scheduled_tasks"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("sessions.id"))
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    cron: Mapped[str] = mapped_column(String(40), nullable=False)  # 5 段 cron
    prompt: Mapped[str]  # 每次触发注入的指令
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run_at: Mapped[str | None]
    next_run_at: Mapped[str | None]
    created_at: Mapped[str] = mapped_column(server_default=func.now())
