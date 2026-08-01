"""代理实例（v2：全局主代理 + 运行时子代理，取代团队概念）。"""
from sqlalchemy import BigInteger, ForeignKey, Integer, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.database import Base


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(10), default="main")  # main / sub
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    model_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("models.id"))
    session_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("sessions.id"))
    turn_id: Mapped[int | None] = mapped_column(BigInteger)  # 子代理归属 turn
    parent_agent_id: Mapped[int | None] = mapped_column(BigInteger)  # 主代理 id
    status: Mapped[str] = mapped_column(String(20), default="idle")  # idle/running/done/failed/terminated
    # 全局绑定能力
    skill_ids: Mapped[list | None] = mapped_column(JSON)
    mcp_server_ids: Mapped[list | None] = mapped_column(JSON)
    # 工作记忆（跨任务关键事实，v3 迁移至 memory_entries 前保留）
    learned_facts: Mapped[list | None] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(server_default=func.now())
