"""后续任务建议（plan-156-619 S10）。

会话收尾时由 AI 提出一条「下一步可以做什么」的建议，展示在消息流底部。
用户可以「在新子会话执行」或「取消」——取消后该条永久不再出现。

【为什么单独一张表而不是塞进 messages】
  · 建议有**独立状态机**（pending → accepted / dismissed），消息没有；
  · 「取消后不再展示」需要跨刷新、跨重启的持久化判据，靠前端内存做不到；
  · 一个会话只保留一条 pending，用表 + 查询口径比在消息流里筛更直白。
"""
from sqlalchemy import JSON, BigInteger, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.database import Base


class TaskSuggestion(Base):
    __tablename__ = "task_suggestions"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True, autoincrement=True,
    )
    session_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # 归属 turn：只有「最新一轮」的建议才展示（历史轮次的建议不占消息流底部）
    turn_id: Mapped[int | None] = mapped_column(BigInteger)
    # 结构化载荷：{title, summary, steps[], mode, rationale}
    payload_json: Mapped[dict | None] = mapped_column(JSON)
    # pending / accepted / dismissed  —— dismissed 后永不展示
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    created_at: Mapped[str] = mapped_column(server_default=func.now())
    decided_at: Mapped[str | None] = mapped_column(String(40))
