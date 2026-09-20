"""工具调用（幂等键）。"""
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

# 说明（plan-282-1441）：此处原有第二份 `class AuditLog(Base)`（同一张 audit_logs 表，
# 但列集合与 audit.py 的版本不同）——属历史遗留重复定义。它会让任何同时导入
# tool_call 与 audit 的模块触发 SQLAlchemy
#   InvalidRequestError: Table 'audit_logs' is already defined for this MetaData instance
# （新写的会话级联删除测试即因此失败）。audit.py 才是权威定义：
# models/__init__.py 与 audit_service 都从那里导入，migrations 也按它补 session_id/turn_id。
# 故删除重复定义；需要 AuditLog 请从 app.persistence.models.audit 导入。
