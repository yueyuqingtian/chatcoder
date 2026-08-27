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


class Provider(Base):
    """v16: 模型供应商 —— 一个供应商(URL+Key)下挂多个模型。"""
    __tablename__ = "providers"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    tenant_id: Mapped[int | None] = mapped_column(BigInteger)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    base_url: Mapped[str | None] = mapped_column(String(255))
    api_key: Mapped[str | None] = mapped_column(String(500))
    api_format: Mapped[str] = mapped_column(String(20), default="openai")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # v23: ta3 供应商登录态（auth_status: pending | logged_in；account_label: 账号显示名）
    auth_status: Mapped[str | None] = mapped_column(String(20))
    account_label: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[str] = mapped_column(server_default=func.now())


class Model(Base):
    __tablename__ = "models"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    tenant_id: Mapped[int | None] = mapped_column(BigInteger)
    # v16: 所属供应商；为空表示独立模型（使用自身 base_url/api_key）
    provider_id: Mapped[int | None] = mapped_column(BigInteger)
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
    # v23: ta3 模型远端元数据（list-assistants 下发）：
    # {systemMessage, anthropic, provider, completionOptions, requestHeaders, title, orgId, profileId}
    ta3_meta: Mapped[dict | None] = mapped_column(JSON)
    # v24: workbuddy 模型元数据（/v3/config 下发）：
    # {title, credits, vendor, tags, maxOutputTokens, supportsReasoning, onlyReasoning, reasoning, temperature}
    workbuddy_meta: Mapped[dict | None] = mapped_column(JSON)
    # v25: trae 模型元数据（batch_get_detail_param 下发）：
    # {config_name, title, functions, prompt_max_tokens, max_tokens, multimodal, model_extra_config}
    trae_meta: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(server_default=func.now())
