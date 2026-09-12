"""记忆记录（D8，替代旧 learned_facts 单字段）。

plan-230-1144 M4.1: 三层化——scope 区分 global（跨项目规范/偏好）/
project（项目约定）/ session（会话事实）。旧数据 scope 为空按 session 处理。
"""
from sqlalchemy import BigInteger, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.database import Base


class MemoryEntry(Base):
    __tablename__ = "memory_entries"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    # 会话记忆挂 session；项目/全局记忆 session_id 存创建来源（保留可追溯）
    session_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("sessions.id"))
    turn_id: Mapped[int | None] = mapped_column(BigInteger)
    text: Mapped[str] = mapped_column(nullable=False)  # 提取的记忆内容
    kind: Mapped[str] = mapped_column(String(20), default="fact")  # fact / convention / pitfall / decision
    usage_count: Mapped[int] = mapped_column(Integer, default=0)
    last_usage_at: Mapped[str | None]
    # plan-230-1144 M4.1: 记忆作用域 session / project / global；空值兼容为 session
    scope: Mapped[str] = mapped_column(String(12), default="session")
    # project 记忆所属项目（scope=project 时有效）
    project_id: Mapped[int | None] = mapped_column(BigInteger)
    # 候选区标记：低置信记忆降级保存、不注入 prompt 但可被检索
    candidate: Mapped[bool] = mapped_column(default=False)
    # 过期时间（ISO；空=永久）。用于会话记忆的老化治理
    expires_at: Mapped[str | None] = mapped_column(String(40))
    # 被取代的旧记忆指向新记忆 id（整合去重时使用）
    superseded_by: Mapped[int | None] = mapped_column(BigInteger)
    generated_at: Mapped[str] = mapped_column(server_default=func.now())
