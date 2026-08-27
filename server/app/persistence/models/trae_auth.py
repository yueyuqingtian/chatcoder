"""TRAE SOLO CN 登录态存储 — 单行记录（tenant 级）。

与 ta3_auth / workbuddy_auth 同构，另存设备签名材料（EC P-256 密钥对、
机器指纹），供 ExchangeToken 刷新时生成 DeviceProof 签名使用。
方案: docs/plan-trae-solo-provider-integration.md §4.3。
"""
from sqlalchemy import JSON, BigInteger, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.database import Base


class TraeAuth(Base):
    __tablename__ = "trae_auth"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    provider_id: Mapped[int | None] = mapped_column(BigInteger)
    access_token: Mapped[str | None] = mapped_column(String(1000))  # JWT
    refresh_token: Mapped[str | None] = mapped_column(String(1000))
    # 设备签名材料（登录时生成，刷新 Token 必须复用同一对密钥/指纹）
    device_private_key: Mapped[str | None] = mapped_column(String(1000))  # EC P-256 私钥 PEM
    device_public_key: Mapped[str | None] = mapped_column(String(1000))   # 公钥 PEM
    device_id: Mapped[str | None] = mapped_column(String(120))
    machine_id: Mapped[str | None] = mapped_column(String(120))
    token_expires_at: Mapped[str | None] = mapped_column(String(40))
    refresh_expires_at: Mapped[str | None] = mapped_column(String(40))
    account: Mapped[dict | None] = mapped_column(JSON)
    catalog: Mapped[dict | None] = mapped_column(JSON)
    updated_at: Mapped[str | None] = mapped_column(String(40))
