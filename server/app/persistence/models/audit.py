"""审计日志（D19）。"""
from sqlalchemy import BigInteger, ForeignKey, Integer, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.database import Base


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    session_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("sessions.id"))
    turn_id: Mapped[int | None] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(String(40), nullable=False)  # tool_call / approval / rollback / config_change / hook
    detail: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(server_default=func.now())
