"""项目（工作目录）：v2 项目任务驱动的顶层实体。"""
from sqlalchemy import BigInteger, Boolean, Integer, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.database import Base


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)  # 工作目录末段，可改
    path: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)  # 绝对路径，创建后不可改
    rules_docs: Mapped[list | None] = mapped_column(JSON)  # 手动配置的规则文档相对路径
    auto_scan_rules: Mapped[bool] = mapped_column(Boolean, default=True)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[str] = mapped_column(server_default=func.now())
    updated_at: Mapped[str] = mapped_column(server_default=func.now(), onupdate=func.now())
