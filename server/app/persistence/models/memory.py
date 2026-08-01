"""记忆记录（D8，替代旧 learned_facts 单字段）。"""
from sqlalchemy import BigInteger, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.database import Base


class MemoryEntry(Base):
    __tablename__ = "memory_entries"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("sessions.id"))
    turn_id: Mapped[int | None] = mapped_column(BigInteger)
    text: Mapped[str] = mapped_column(nullable=False)  # 提取的记忆内容
    kind: Mapped[str] = mapped_column(String(20), default="fact")  # fact / convention / pitfall / decision
    usage_count: Mapped[int] = mapped_column(Integer, default=0)
    last_usage_at: Mapped[str | None]
    generated_at: Mapped[str] = mapped_column(server_default=func.now())
