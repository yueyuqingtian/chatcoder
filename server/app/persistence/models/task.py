"""任务（子代理承载的工作项）与产物。"""
from sqlalchemy import BigInteger, Boolean, ForeignKey, Integer, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.database import Base


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("sessions.id"))
    turn_id: Mapped[int | None] = mapped_column(BigInteger)
    parent_task_id: Mapped[int | None] = mapped_column(BigInteger)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None]
    acceptance_criteria: Mapped[str | None]
    agent_id: Mapped[int | None] = mapped_column(BigInteger)  # 执行子代理
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending/running/done/failed/cancelled
    priority: Mapped[int] = mapped_column(Integer, default=0)
    artifact_ids: Mapped[list | None] = mapped_column(JSON)
    token_usage: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str | None] = mapped_column(String(500))  # 失败原因/完成说明
    created_at: Mapped[str] = mapped_column(server_default=func.now())
    updated_at: Mapped[str] = mapped_column(server_default=func.now(), onupdate=func.now())


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    task_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("tasks.id"))
    type: Mapped[str | None] = mapped_column(String(20))
    title: Mapped[str | None] = mapped_column(String(200))
    storage_ref: Mapped[str | None] = mapped_column(String(255))
    summary: Mapped[str | None]
    git_baseline: Mapped[str | None] = mapped_column(String(64))
    files: Mapped[list | None] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(server_default=func.now())
