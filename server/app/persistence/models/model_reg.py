"""模型注册（异构模型分工核心）。BYOK 的 api_key 仅存客户端本地加密。"""
from sqlalchemy import (
    Boolean,
    BigInteger,
    Integer,
    JSON,
    Numeric,
    SmallInteger,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.database import Base


class Model(Base):
    __tablename__ = "models"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    tenant_id: Mapped[int | None] = mapped_column(BigInteger)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(40))
    base_url: Mapped[str | None] = mapped_column(String(255))
    intelligence_level: Mapped[int] = mapped_column(SmallInteger, default=2)
    context_window: Mapped[int | None] = mapped_column(Integer)
    price_input_1k: Mapped[float | None] = mapped_column(Numeric(10, 4))
    price_output_1k: Mapped[float | None] = mapped_column(Numeric(10, 4))
    source_type: Mapped[str] = mapped_column(String(20), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # v1.0: 多模态能力标记 & API 格式(openai / anthropic)
    is_multimodal: Mapped[bool] = mapped_column(Boolean, default=False)
    api_format: Mapped[str] = mapped_column(String(20), default="openai")
    # v2.0: per-model API key(本地桌面版明文存储,云端版可加密)
    api_key: Mapped[str | None] = mapped_column(String(500))
    # v4: 模型支持的推理深度档位列表(如 ["minimal","low","medium","high"])
    reasoning_efforts: Mapped[list | None] = mapped_column(JSON, default=list)
    created_at: Mapped[str] = mapped_column(server_default=func.now())
