"""命令执行策略规则（D4）。v2.2: 支持工具级规则（tool_name 匹配）。"""
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
    # v2.2 (对齐 zcode 3.12): 工具级规则（审批卡"始终允许"生成）；空 = 命令前缀规则
    tool_name: Mapped[str | None] = mapped_column(String(60))
