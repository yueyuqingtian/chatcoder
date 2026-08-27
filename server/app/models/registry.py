"""Provider 注册表:统一获取各来源的 Provider 实例。

v0.3:
- get_default_provider:服务端默认模型(持服务端密钥)。
- get_provider_for_model:按 model_id 查 Model 表构造 Provider。
  system_default → 用 settings 密钥构造;byok → 返回 None(服务端无密钥)。
- get_provider_for_agent:按 agent.model_id 路由,无绑定则回落默认。
v1.0:
- 支持 api_format 字段选择 provider(openai / anthropic)。
"""
from functools import lru_cache
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.base import ModelProvider
from app.models.providers.openai_compatible import OpenAICompatibleProvider

if TYPE_CHECKING:
    from app.persistence.models.agent import Agent
    from app.persistence.models.model_reg import Model


def _build_provider(
    api_key: str, base_url: str, model: str, api_format: str = "openai",
    meta: dict | None = None,
) -> ModelProvider:
    """根据 api_format 构造对应的 Provider 实例。"""
    api_format = (api_format or "openai").lower()
    if api_format == "ta3":
        from app.models.providers.ta3 import Ta3Provider

        return Ta3Provider(api_key=api_key, base_url=base_url, model=model, meta=meta or {})
    if api_format == "anthropic":
        from app.models.providers.anthropic import AnthropicProvider

        return AnthropicProvider(api_key=api_key, base_url=base_url, model=model)
    return OpenAICompatibleProvider(api_key=api_key, base_url=base_url, model=model)


async def _build_trae_provider(
    db: AsyncSession, model: "Model | None", provider=None,
) -> tuple["ModelProvider | None", str]:
    """构造 TraeProvider：token 实时取自 trae_auth 表（防过期），注入 401 刷新回调。

    对齐 workbuddy 模式：Model.api_key 为占位符（"__trae_session__"），
    真实 JWT 每次构造时从 trae_auth 动态加载；meta 携带设备指纹/账号信息
    供业务请求头使用（build_business_headers）。
    """
    from app.auth.trae import session as trae_session
    from app.core.config import settings as _settings
    from app.models.providers.trae import TraeProvider

    if model is None:
        return None, "trae_model_missing"
    if provider is None:
        if model.provider_id:
            from app.persistence.models.model_reg import Provider as _Provider

            provider = await db.get(_Provider, model.provider_id)
    provider_id = provider.id if provider is not None else model.provider_id
    auth = await trae_session.load_auth(db, provider_id)
    if auth is None or not auth.access_token:
        return None, "trae_login_required"

    agent_host = (
        (provider.base_url if provider is not None and provider.base_url else None)
        or _settings.trae_agent_endpoint
    ).rstrip("/")

    meta = dict(getattr(model, "trae_meta", None) or {})
    account = auth.account or {}
    meta.setdefault("region", account.get("region") or "cn")
    meta.setdefault("device_id", auth.device_id or "")
    meta.setdefault("machine_id", auth.machine_id or "")
    meta.setdefault("ide_version", _settings.trae_ide_version)
    meta.setdefault("app_id", _settings.trae_app_id)
    meta.setdefault("app_version_code", _settings.trae_app_version_code)
    user_info = {
        "user_id": account.get("user_id") or "",
        "name": account.get("name") or "",
        "token": auth.access_token,
        "region": account.get("region") or "cn",
        "scope": "marscode",
    }
    meta["user_info"] = user_info
    client_info = {
        "device_id": auth.device_id or "",
        "connect_session_id": f"trae-{provider_id}",
        "is_solo_mode": False,
    }
    meta["client_info"] = client_info
    common_params = {
        "device_id": auth.device_id or "",
        "machine_id": auth.machine_id or "",
        "region": (account.get("region") or "cn").upper(),
        "aiRegion": account.get("ai_region") or "CN",
        "quality": "stable",
        "app_version": _settings.trae_ide_version,
        "product_code": "SOLO_Lite",
    }
    meta["common_params"] = common_params

    async def _refresh() -> str | None:
        try:
            return await trae_session.refresh_session(
                db, provider_id, api_host=_settings.trae_account_endpoint,
                client_id=_settings.trae_client_id, ide_version=_settings.trae_ide_version)
        except trae_session.TraeAuthError:
            return None

    p = TraeProvider(
        api_key=auth.access_token,
        base_url=agent_host,
        model=model.name,
        meta=meta,
        refresh_token=_refresh,
    )
    return p, "trae_session"


async def _build_workbuddy_provider(
    db: AsyncSession, model: "Model | None", provider=None,
) -> tuple["ModelProvider | None", str]:
    """构造 WorkBuddyProvider：token 实时取自 auth 表（防过期），注入 401 刷新回调。

    workbuddy 模型的 Model.api_key 只是占位符（"__workbuddy_session__"），
    真实 accessToken 每次构造时从 workbuddy_auth 动态加载。
    """
    from app.auth.workbuddy import session as wb_session
    from app.models.providers.workbuddy import WorkBuddyProvider

    if model is None:
        return None, "workbuddy_model_missing"
    if provider is None:
        if model.provider_id:
            from app.persistence.models.model_reg import Provider as _Provider

            provider = await db.get(_Provider, model.provider_id)
    provider_id = provider.id if provider is not None else model.provider_id
    auth = await wb_session.load_auth(db, provider_id)
    if auth is None or not auth.access_token:
        return None, "workbuddy_login_required"

    endpoint = (
        (provider.base_url if provider is not None and provider.base_url else None)
        or getattr(settings, "workbuddy_endpoint", "https://copilot.tencent.com")
    ).rstrip("/")
    # v25: 网关 LLM 通道固定为 {endpoint}/v2（对齐 CLI resolveModelBaseURL 默认值）。
    # 请求 {endpoint}/chat/completions（无 /v2 前缀）会被 APISIX 302 重定向到别处。
    # 认证接口（auth/state、token/refresh 等）仍走 {endpoint}，不可复用带 /v2 的 base。
    api_base = f"{endpoint}/v2" if not endpoint.endswith("/v2") else endpoint
    meta = dict(getattr(model, "workbuddy_meta", None) or {})
    account = auth.account or {}
    meta.setdefault("account_uid", account.get("uid") or "")
    meta.setdefault("enterprise_id", account.get("enterpriseId") or "")

    async def _refresh() -> str | None:
        try:
            # 认证接口走 endpoint（不带 /v2，refresh_session 内部拼接 /v2/plugin/...）
            return await wb_session.refresh_session(db, provider_id, endpoint)
        except wb_session.WorkBuddyAuthError:
            return None

    p = WorkBuddyProvider(
        api_key=auth.access_token,
        base_url=api_base,
        model=model.name,
        meta=meta,
        refresh_token=_refresh,
    )
    return p, "workbuddy_session"


class ModelRegistry:
    """根据模型来源与配置构造 Provider。"""

    def get_default_provider(self) -> ModelProvider | None:
        """服务端默认模型 Provider(持有服务端密钥)。"""
        if not settings.default_model_ready:
            return None
        return _build_provider(
            api_key=settings.default_llm_api_key,
            base_url=settings.default_llm_base_url,
            model=settings.default_llm_model,
            api_format=getattr(settings, "default_llm_api_format", "openai"),
        )

    async def get_provider_for_model(
        self, db: AsyncSession, model: "Model | None"
    ) -> tuple[ModelProvider | None, str]:
        """按 Model 记录构造 Provider。

        优先级:
        1. model.api_key 自带密钥 → 用 model.base_url 直接构造
        2. system_default → 用 model.base_url + 服务端全局密钥
        3. byok → 服务端无密钥,返回 None
        4. model 为 None → 回落默认 provider
        """
        from app.core.enums import ModelSource

        if model is None:
            p = self.get_default_provider()
            return p, "default" if p else "no_default_configured"

        # v2.0: model 自带 api_key,直接构造 provider(最高优先)
        model_api_key = getattr(model, "api_key", None)
        if model_api_key:
            base_url = model.base_url or settings.default_llm_base_url
            model_name = model.name
            if base_url and model_name:
                api_format = getattr(model, "api_format", "openai") or "openai"
                # v24: workbuddy 模型 —— token 实时取自 auth 表（Model.api_key 为占位符）
                if api_format == "workbuddy":
                    return await _build_workbuddy_provider(db, model)
                # v25: trae 模型 —— token/设备指纹实时取自 trae_auth 表
                if api_format == "trae":
                    return await _build_trae_provider(db, model)
                # v23: ta3 模型携带远端元数据（系统提示词/协议/目录配置）
                ta3_meta = getattr(model, "ta3_meta", None) or {}
                return (
                    _build_provider(api_key=model_api_key, base_url=base_url, model=model_name,
                                    api_format=api_format, meta=ta3_meta),
                    "model_key",
                )

        # v16: 模型挂在供应商下 —— 用供应商的 base_url/api_key 构造
        provider_id = getattr(model, "provider_id", None)
        if provider_id:
            from app.persistence.models.model_reg import Provider

            provider = await db.get(Provider, provider_id)
            if provider and provider.is_active and provider.base_url and provider.api_key:
                api_format = (provider.api_format or getattr(model, "api_format", "openai") or "openai")
                return (
                    _build_provider(api_key=provider.api_key, base_url=provider.base_url, model=model.name, api_format=api_format),
                    "provider_key",
                )
            return None, "provider_incomplete"

        if model.source_type == ModelSource.BYOK:
            return None, "byok_requires_client"

        # system_default:用 model.base_url + 服务端密钥
        base_url = model.base_url or settings.default_llm_base_url
        api_key = settings.default_llm_api_key
        model_name = model.name
        if not (base_url and api_key and model_name):
            return None, "system_default_incomplete"
        api_format = getattr(model, "api_format", "openai") or "openai"
        return (
            _build_provider(api_key=api_key, base_url=base_url, model=model_name, api_format=api_format),
            "system_default",
        )

    async def get_provider_for_agent(
        self, db: AsyncSession, agent: "Agent"
    ) -> tuple[ModelProvider | None, str]:
        """按 agent.model_id 路由 Provider。

        - agent.model_id 为空 → 回落默认 provider。
        - 否则查 Model 表,按来源路由。
        """
        if not agent.model_id:
            p = self.get_default_provider()
            return p, "default" if p else "no_default_configured"

        from app.persistence.models.model_reg import Model

        model = await db.get(Model, agent.model_id)
        return await self.get_provider_for_model(db, model)


@lru_cache
def get_model_registry() -> ModelRegistry:
    return ModelRegistry()
