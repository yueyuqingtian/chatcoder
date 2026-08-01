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
