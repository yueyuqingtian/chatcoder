"""antigravity（Google Antigravity）登录态存储（plan-antigravity-provider-integration）。

与 kiro_auth / ta3_auth / workbuddy_auth 同构（access_token + refresh_token + account
+ catalog 缓存），另存 Antigravity 专有字段：

- project：CloudCode 网关的 cloudaicompanionProject（免费档为 aicode-consumers）；
- tier：账号档位（free-tier / standard-tier / g1-pro-tier），用于界面展示与配额提示；
- scope：最近一次刷新返回的 scope（远端可能比本地文件多 experimentsandconfigs/cclog，
  只能以远端为准，不能拿本地 scope 做权限判断）；
- id_token：含 email/sub，用于账号标签与多账号去重；
- auth_kind：oauth 走 CloudCode 网关（全量模型），api_key 走 Gemini 兼容接口（仅 Gemini 系）。

当前项目桌面版凭据本就明文存 SQLite，此处保持同等安全级别。
"""
from sqlalchemy import BigInteger, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.database import Base


class AntigravityAuth(Base):
    __tablename__ = "antigravity_auth"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    provider_id: Mapped[int | None] = mapped_column(BigInteger)
    # 归属凭据（多账号）；旧行为空，由迁移挂到首条凭据
    credential_id: Mapped[int | None] = mapped_column(BigInteger)
    access_token: Mapped[str | None] = mapped_column(String(4000))
    refresh_token: Mapped[str | None] = mapped_column(String(4000))
    id_token: Mapped[str | None] = mapped_column(String(8000))
    project: Mapped[str | None] = mapped_column(String(120))
    tier: Mapped[str | None] = mapped_column(String(60))
    scope: Mapped[str | None] = mapped_column(String(600))
    auth_kind: Mapped[str | None] = mapped_column(String(20))
    account: Mapped[dict | None] = mapped_column(JSON)
    catalog: Mapped[dict | None] = mapped_column(JSON)
    expires_at: Mapped[str | None] = mapped_column(String(40))
    updated_at: Mapped[str | None] = mapped_column(String(40))
