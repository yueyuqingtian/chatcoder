"""数据库连接与权限策略（plan-282-1441 #7 内置 MCP「数据库连接」）。

设计要点：
- 连接**按项目**归属：同一套应用里不同项目的数据库互不可见（避免串到别的项目）。
- 密码**加密存储**（对称加密，见 db_connection_service），不落明文。
- 权限策略独立一表：读 / 写 / DDL 三个开关 + 是否强制审批 + 行数上限 + 超时。
  **服务端强制门控**——前端开关只作配置，不是安全边界。
"""
from sqlalchemy import BigInteger, Boolean, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.database import Base


class DbConnection(Base):
    """一个数据库连接（按项目归属）。"""

    __tablename__ = "db_connections"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(BigInteger, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    # mysql / postgresql / sqlserver
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int | None] = mapped_column(Integer)
    database: Mapped[str | None] = mapped_column(String(160))
    username: Mapped[str | None] = mapped_column(String(160))
    # 加密后的密码（密文），绝不存明文
    password_enc: Mapped[str | None] = mapped_column(Text)
    # 附加连接参数（如 sslmode / charset），JSON 对象
    params: Mapped[dict | None] = mapped_column(JSON)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[str] = mapped_column(server_default=func.now())
    updated_at: Mapped[str | None] = mapped_column(server_default=func.now(), onupdate=func.now())


class DbPolicy(Base):
    """某项目的数据库权限策略（一个项目一行）。"""

    __tablename__ = "db_policies"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(BigInteger, index=True, unique=True)
    allow_read: Mapped[bool] = mapped_column(Boolean, default=True)
    allow_write: Mapped[bool] = mapped_column(Boolean, default=False)
    allow_ddl: Mapped[bool] = mapped_column(Boolean, default=False)
    # 是否要求用户审批后才执行（即使权限已开）
    require_approval: Mapped[bool] = mapped_column(Boolean, default=True)
    # 单次查询返回行数上限（防一次拉爆上下文）
    row_limit: Mapped[int] = mapped_column(Integer, default=200)
    timeout_s: Mapped[int] = mapped_column(Integer, default=15)
    updated_at: Mapped[str | None] = mapped_column(server_default=func.now(), onupdate=func.now())
