"""工具调用（幂等键）、审计日志。"""
from sqlalchemy import BigInteger, Integer, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.database import Base


class ToolCall(Base):
    """call_key 为客户端生成的幂等键，防重复执行。"""

    __tablename__ = "tool_calls"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    session_id: Mapped[int | None] = mapped_column(BigInteger)
    task_id: Mapped[int | None] = mapped_column(BigInteger)
    agent_id: Mapped[int | None] = mapped_column(BigInteger)
    call_key: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    tool_name: Mapped[str] = mapped_column(String(60), nullable=False)
    args: Mapped[dict | None] = mapped_column(JSON)
    result: Mapped[dict | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    client_id: Mapped[str | None] = mapped_column(String(60))
    created_at: Mapped[str] = mapped_column(server_default=func.now())


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    tenant_id: Mapped[int | None] = mapped_column(BigInteger)
    actor_type: Mapped[str | None] = mapped_column(String(10))
    actor_id: Mapped[int | None] = mapped_column(BigInteger)
    action: Mapped[str | None] = mapped_column(String(60))
    target: Mapped[str | None] = mapped_column(String(120))
    detail: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(server_default=func.now())
