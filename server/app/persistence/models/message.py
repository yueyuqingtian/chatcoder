"""会话与消息（v2：项目任务驱动）。"""
from sqlalchemy import BigInteger, Boolean, ForeignKey, Index, Integer, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.database import Base


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    project_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("projects.id"))
    title: Mapped[str | None] = mapped_column(String(160))  # 自动命名（首条消息 AI 生成）
    model_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("models.id"))  # 会话级模型
    status: Mapped[str] = mapped_column(String(20), default="active")  # active / archived
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    fork_parent_id: Mapped[int | None] = mapped_column(BigInteger)  # 分支来源会话 id
    worktree_path: Mapped[str | None] = mapped_column(String(512))  # git 工作树路径（有则优先作为工作目录）
    created_at: Mapped[str] = mapped_column(server_default=func.now())
    updated_at: Mapped[str] = mapped_column(server_default=func.now(), onupdate=func.now())


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("sessions.id"))
    turn_id: Mapped[int | None] = mapped_column(BigInteger)  # 归属 turn（合并展示的关键）
    thread_id: Mapped[int | None] = mapped_column(BigInteger)  # 子代理线程（= subagent.id）
    sender_type: Mapped[str] = mapped_column(String(10), nullable=False)  # user / agent / system
    sender_id: Mapped[int | None] = mapped_column(BigInteger)
    msg_type: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[dict] = mapped_column(JSON, nullable=False)
    token_usage: Mapped[int] = mapped_column(Integer, default=0)
    # 回滚软删标记（True=已回滚，列表查询过滤）
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[str] = mapped_column(server_default=func.now())

    __table_args__ = (
        Index("idx_messages_session_turn", "session_id", "turn_id"),
        Index("idx_messages_thread", "thread_id", postgresql_where="thread_id IS NOT NULL"),
    )
