"""v2.2 (对齐 zcode 3.13): 子代理类型配置。

内置 Explore（只读搜索代理）与 general（全量工具），
用户可在设置页增删改，spawn_subagent 通过 profile 指定类型。
"""
from sqlalchemy import JSON, BigInteger, Boolean, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.database import Base


class SubagentProfile(Base):
    __tablename__ = "subagent_profiles"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(60), nullable=False)  # 唯一名，如 explore / general
    description: Mapped[str | None] = mapped_column(String(300))
    # 工具白名单（JSON 数组）；空/None = 全量工具
    tools_whitelist: Mapped[list | None] = mapped_column(JSON)
    # 模型覆盖（None = 跟随会话/主代理模型）
    model_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("models.id"))
    system_prompt: Mapped[str | None] = mapped_column(String(4000))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
