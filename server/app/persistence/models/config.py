"""配置分层与命名 profile（D6）。"""
from sqlalchemy import BigInteger, Boolean, ForeignKey, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.database import Base


class ConfigProfile(Base):
    __tablename__ = "config_profiles"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)  # default / ci / paranoid ...
    scope: Mapped[str] = mapped_column(String(20), default="global")  # global / project
    project_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("projects.id"))
    data: Mapped[dict] = mapped_column(JSON, nullable=False)  # 完整配置对象
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
