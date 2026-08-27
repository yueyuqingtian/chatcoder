"""workbuddy（腾讯 CodeBuddy/WorkBuddy）登录态存储 — 单行记录（tenant 级）。

与 ta3_auth 同构：access_token + refresh_token + account + catalog 缓存。
当前项目桌面版 API key 本就明文存 SQLite，此处保持同等安全级别（后续可加加密）。
"""
from sqlalchemy import JSON, BigInteger, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.database import Base


class WorkBuddyAuth(Base):
    __tablename__ = "workbuddy_auth"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    provider_id: Mapped[int | None] = mapped_column(BigInteger)
    access_token: Mapped[str | None] = mapped_column(String(500))
    refresh_token: Mapped[str | None] = mapped_column(String(500))
    account: Mapped[dict | None] = mapped_column(JSON)
    catalog: Mapped[dict | None] = mapped_column(JSON)
    updated_at: Mapped[str | None] = mapped_column(String(40))
