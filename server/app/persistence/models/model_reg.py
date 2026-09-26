"""模型注册（异构模型分工核心）。BYOK 的 api_key 仅存客户端本地加密。"""
from sqlalchemy import (
    Boolean,
    BigInteger,
    Index,
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
    """v16: 模型供应商 —— 一个供应商(URL+Key)下挂多个模型。

    plan-248-1258 M2.1: 新增凭据化与代理字段：
    - api_key 保留为「兼容字段 + 轮询兜底」——新逻辑优先读 provider_credentials；
      迁移时旧 api_key 自动拆入首条凭据，两者并存不破坏既有行为。
    - proxy_mode / proxy_url：每供应商独立代理（inherit=跟随全局，custom=独立地址，
      direct=直连绕过代理，global=强制走全局代理）。
    plan-271-1364: credential_strategy 取用策略 ——
    - sticky（默认）：上次成功过的凭据优先（粘性），失败进冷却后切下一条；
    - round_robin：忽略粘性，严格按 priority 顺序轮转起点，分散用量。
    """
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
    # plan-248-1258 M2.3: 供应商级代理
    proxy_mode: Mapped[str] = mapped_column(String(12), default="inherit")
    proxy_url: Mapped[str | None] = mapped_column(String(255))
    # plan-271-1364: 凭据取用策略（sticky=粘性优先 | round_robin=按优先级轮转）
    credential_strategy: Mapped[str] = mapped_column(String(16), default="sticky")
    # plan-41-225: 供应商手动排序位（用户在模型页左列拖拽调整顺序）。
    # 0 = 未排序，列表由 id 兜底——保证老库升级后顺序与升级前完全一致（零感知）。
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[str] = mapped_column(server_default=func.now())


class ProviderCredential(Base):
    """plan-248-1258 M2.1: 供应商凭据（多 API Key / 多登录账号）。

    一个供应商可挂多条凭据；请求失败（401/403/429/5xx/连接错误）时按 priority
    轮询到下一条，实现「一个 key 挂了自动切另一个」。
    OAuth 类供应商（workbuddy/ta3/trae）的凭据以 token_ref 关联各自 auth 表行，
    api_key 存占位值；模型调用时按凭据取对应账号的 token。
    """
    __tablename__ = "provider_credentials"
    __table_args__ = (
        Index("idx_provider_credentials_provider", "provider_id", "priority"),
    )

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    provider_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # 显示标签：账号昵称 / key 备注（如 "主号"、"备用 key"）
    label: Mapped[str | None] = mapped_column(String(120))
    api_key: Mapped[str | None] = mapped_column(String(500))
    # OAuth 类账号：指向 workbuddy_auth / ta3_auth / trae_auth 的行 id（见各自表 credential_id）
    token_ref: Mapped[str | None] = mapped_column(String(60))
    # 轮询优先级（小的先用）
    priority: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # 运行态：ok | cooldown | error | disabled
    status: Mapped[str] = mapped_column(String(12), default="ok")
    last_error: Mapped[str | None] = mapped_column(String(300))
    # 冷却截止时间（ISO 字符串；status=cooldown 期间跳过该凭据）
    cooldown_until: Mapped[str | None] = mapped_column(String(40))
    # 最近一次成功时间（粘性优先依据）
    last_ok_at: Mapped[str | None] = mapped_column(String(40))
    # plan-290: 连续失败次数（成功即清零）。达到 provider_credential_fail_threshold
    # 才置 cooldown；未达标时只记录 last_error 并留在可用池里，避免瞬时抖动误冷却。
    fail_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # workbuddy 等账号的积分余额缓存（数字，可为小数）
    credits: Mapped[float | None] = mapped_column(Numeric(12, 2))
    # 供应商自定义扩展（如 workbuddy 账号 uid / enterpriseId 冗余快照）
    extra: Mapped[dict | None] = mapped_column(JSON)
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
