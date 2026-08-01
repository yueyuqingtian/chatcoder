"""租户与用户。MVP 单租户，所有业务表预留 tenant_id。"""
from sqlalchemy import BigInteger, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.database import Base


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    plan: Mapped[str] = mapped_column(String(40), default="free")
    created_at: Mapped[str] = mapped_column(server_default=func.now())


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    tenant_id: Mapped[int | None] = mapped_column(BigInteger)
    email: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(80))
    role: Mapped[str] = mapped_column(String(20), default="user")
    password_hash: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[str] = mapped_column(server_default=func.now())
