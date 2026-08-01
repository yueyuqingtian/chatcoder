"""命令执行策略规则（D4）。"""
from sqlalchemy import BigInteger, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.database import Base


class ExecPolicyRule(Base):
    __tablename__ = "exec_policy_rules"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    session_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("sessions.id"))  # None=全局
    command_pattern: Mapped[str] = mapped_column(String(200), nullable=False)  # 前缀匹配，如 "git push"
    decision: Mapped[str] = mapped_column(String(10), nullable=False)  # allow / deny / ask
    justification: Mapped[str | None] = mapped_column(String(300))
