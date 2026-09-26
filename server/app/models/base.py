"""Provider 抽象基类（v3：统一多协议接口）。

所有 Provider 必须实现 chat / stream / stream_structured 三个方法，
保证 OpenAI / Anthropic / 其它协议在 agent_loop 侧能力对齐。
"""
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any

from app.models.schemas import ChatRequest, ChatResponse


class ModelProvider(ABC):
    """统一 LLM Provider 接口。"""

    name: str = "base"

    @abstractmethod
    async def chat(self, request: ChatRequest) -> ChatResponse:
        """非流式对话。"""

    @abstractmethod
    async def stream(self, request: ChatRequest) -> AsyncIterator[str]:
        """流式输出，逐块 yield 文本。"""
        raise NotImplementedError
        yield ""  # pragma: no cover  # 让类型识别为 AsyncIterator[str]

    @abstractmethod
    async def stream_structured(self, request: ChatRequest) -> AsyncIterator[dict[str, Any]]:
        """结构化流式：yield dict，含 type(thinking/content/tool_call/done) 等字段。

        v3 提升为基类抽象方法，确保各协议 Provider 能力对齐。
        各 Provider 可用最优原生流式实现；若协议不支持，可回退为
        先 chat 收集再分段 yield 的兼容实现。
        """
        raise NotImplementedError
        yield {}  # pragma: no cover  # 让类型识别为 AsyncIterator[dict]


def resolve_reasoning(provider: "ModelProvider",
                      effort: str | None = None) -> tuple[str | None, bool]:
    """解析「用户配置的思考深度」→ (reasoning_effort, thinking)。

    plan-53-258 R4: 压缩/摘要/记忆提取/提交信息等辅助 AI 请求此前不带思考深度，
    用户在设置中心配置的档位只在主对话生效。本函数把口径收敛到一处（与 agent_loop
    的 _thinking_enabled 判定同源），供各辅助请求复用：

    - effort 为空时回落全局设置 agent_reasoning_effort（用户配置的档位）；
      none/空 → 返回 None，表示不下发该字段（不干扰不支持思考的模型）；
    - thinking 仅在总开关开启且 provider 声明支持时为 True——未声明支持的
      provider（如 anthropic/commandcode 自有分支）保持不传。
    """
    from app.core.config import settings

    value = (effort or getattr(settings, "agent_reasoning_effort", "") or "").strip()
    if not value or value.lower() == "none":
        return None, False
    thinking = False
    try:
        if getattr(settings, "agent_thinking_enabled", False) and hasattr(provider, "supports_thinking"):
            thinking = bool(provider.supports_thinking())
    except Exception:
        thinking = False
    return value, thinking
