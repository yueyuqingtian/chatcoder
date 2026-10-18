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
    # 任务层级：request=用户请求，group=任务区块，step=可执行小点
    kind: Mapped[str] = mapped_column(String(16), default="request", nullable=False)
    # 依赖的 step id/index；仅供后端编排使用，前端不展示
    depends_on: Mapped[list | None] = mapped_column(JSON)
    estimate: Mapped[int | None] = mapped_column(Integer)
    is_hidden: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None]
    acceptance_criteria: Mapped[str | None]
    agent_id: Mapped[int | None] = mapped_column(BigInteger)  # 执行子代理
    # plan-156-619 S6：该步骤由哪个**子会话**执行（sessions.id）。
    # 与 agent_id 分开：agent_id 指探索子代理（agents 表），这里是执行子会话（sessions 表），
    # 两套机制并存，复用一个列会让“这一步到底交给了谁”无法区分。
    subsession_id: Mapped[int | None] = mapped_column(BigInteger)
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
