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
    api_key: str, base_url: str, model: str, api_format: str = "openai"
) -> ModelProvider:
    """根据 api_format 构造对应的 Provider 实例。"""
    api_format = (api_format or "openai").lower()
    if api_format == "anthropic":
        from app.models.providers.anthropic import AnthropicProvider

        return AnthropicProvider(api_key=api_key, base_url=base_url, model=model)
    return OpenAICompatibleProvider(api_key=api_key, base_url=base_url, model=model)


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
                return (
                    _build_provider(api_key=model_api_key, base_url=base_url, model=model_name, api_format=api_format),
                    "model_key",
                )

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
