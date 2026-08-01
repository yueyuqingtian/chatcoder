"""钩子配置（D5，Claude Code 风格）。"""
from sqlalchemy import BigInteger, Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.database import Base


class HookConfig(Base):
    __tablename__ = "hook_configs"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    event: Mapped[str] = mapped_column(String(40), nullable=False)  # pre_tool_use / post_tool_use / ...
    command: Mapped[str] = mapped_column(String(500), nullable=False)  # 要执行的 shell 命令（JSON stdin）
    matcher: Mapped[str | None] = mapped_column(String(120))  # 可选匹配器（如 tool 名过滤）
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
