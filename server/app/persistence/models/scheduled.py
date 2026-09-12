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
    # plan-230-1144 M1.1：触发结果与错过策略
    # last_status: triggered / ok / failed / skipped
    last_status: Mapped[str | None] = mapped_column(String(16), default=None)
    last_error: Mapped[str | None] = mapped_column(String(300), default=None)
    # missed_policy: skip（错过即跳过，只跑下一次）/ run_once（重启后补跑一次）
    missed_policy: Mapped[str] = mapped_column(String(12), default="skip")
    created_at: Mapped[str] = mapped_column(server_default=func.now())
