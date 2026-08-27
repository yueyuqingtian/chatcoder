"""ta3（Ta+3 牛码）登录态存储 — 单行记录（tenant 级）。

与参考项目 auth-session.json 同构（access_token + refresh_token + account + catalog 缓存）。
当前项目桌面版 API key 本就明文存 SQLite，此处保持同等安全级别（后续可加加密）。
"""
from sqlalchemy import BigInteger, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.database import Base


class Ta3Auth(Base):
    __tablename__ = "ta3_auth"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    provider_id: Mapped[int | None] = mapped_column(BigInteger)
    access_token: Mapped[str | None] = mapped_column(String(500))
    refresh_token: Mapped[str | None] = mapped_column(String(500))
    account: Mapped[dict | None] = mapped_column(JSON)
    catalog: Mapped[dict | None] = mapped_column(JSON)
    updated_at: Mapped[str | None] = mapped_column(String(40))
