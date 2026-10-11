"""kiro（AWS Kiro Agentic IDE）登录态存储 — 单行记录（provider 级）。

与 ta3_auth / workbuddy_auth 同构（access_token + refresh_token + account + catalog 缓存），
另存 Kiro 专有字段：
- profile_arn：CodeWhisperer profile ARN（社交登录经 X-Kiro-Profile-Arn 路由企业流量，
  服务端据此判定账号与区域分区）；
- region：API 端点分区（默认 us-east-1，放开地区限制的核心开关）；
- auth_method：social / builder-id / idc；
- expires_at：access_token 过期时刻（刷新判定，ISO8601）。

当前项目桌面版凭据本就明文存 SQLite，此处保持同等安全级别。
"""
from sqlalchemy import BigInteger, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.database import Base


class KiroAuth(Base):
    __tablename__ = "kiro_auth"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    provider_id: Mapped[int | None] = mapped_column(BigInteger)
    # 归属凭据（多账号）；旧行为空，由迁移挂到首条凭据
    credential_id: Mapped[int | None] = mapped_column(BigInteger)
    # Kiro 令牌偏长（社交登录 access/refresh 均可达千字符），留足空间
    access_token: Mapped[str | None] = mapped_column(String(4000))
    refresh_token: Mapped[str | None] = mapped_column(String(4000))
    profile_arn: Mapped[str | None] = mapped_column(String(300))
    region: Mapped[str | None] = mapped_column(String(40))
    auth_method: Mapped[str | None] = mapped_column(String(40))
    account: Mapped[dict | None] = mapped_column(JSON)
    catalog: Mapped[dict | None] = mapped_column(JSON)
    expires_at: Mapped[str | None] = mapped_column(String(40))
    updated_at: Mapped[str | None] = mapped_column(String(40))
