"""LLM 调用的通用数据结构。"""
from typing import Any, Literal
from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    # v6.1: 支持 developer 角色（对齐 codex ContextualUserFragment role="developer"）
    # system = 行为准则（核心，不随任务变化）；developer = 分层注入的上下文片段
    role: Literal["system", "developer", "user", "assistant", "tool"]
    content: str | None = None
    name: str | None = None
    tool_call_id: str | None = None
    # v0.3: assistant 携带的工具调用列表(用于 function-calling 往返)
    tool_calls: list[dict[str, Any]] | None = None
    # v1.0: 多模态内容块 — [{type: "text", text: "..."}, {type: "image_url", image_url: {url: "data:..."}}, ...]
    # 为 None 时 content 为纯文本;有值时 content 作为首文本块,content_blocks 追加图片等
    content_blocks: list[dict[str, Any]] | None = None
    # v1.2: 思考模式的推理内容（DeepSeek reasoning_content / GLM thinking）。
    # thinking 模式下网关要求多轮对话（尤其工具调用）中把历史 assistant 的
    # reasoning_content 原样回传，否则 400: "The reasoning_content in the
    # thinking mode must be passed back to the API."
    reasoning_content: str | None = None


class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    model: str
    # v6.1: 对齐 codex -- temperature 默认 None（不传），让模型供应商选择最优温度
    # codex build_responses_request 完全不传 temperature，只用 reasoning_effort
    temperature: float | None = None
    max_tokens: int | None = None
    tools: list[dict[str, Any]] | None = None
    stream: bool = False
    # v6.0: reasoning_effort 控制推理深度（对齐 codex thinking / claude extended thinking）
    # 可选 "high"/"medium"/"low"/None；None 表示不传，兼容不支持 reasoning 的模型
    reasoning_effort: str | None = None


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    # v1.2: 精确 token 统计（痛点：缓存输入/推理 token 全为 0）
    # OpenAI 兼容 API: prompt_tokens_details.cached_tokens / completion_tokens_details.reasoning_tokens
    # Anthropic: cache_creation_input_tokens / cache_read_input_tokens
    cached_input_tokens: int = 0
    reasoning_tokens: int = 0


class ChatResponse(BaseModel):
    content: str | None = None
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    finish_reason: str = "stop"
    usage: Usage = Field(default_factory=Usage)
    model: str = ""
    # v4.3: 模型的推理/思考内容（DeepSeek reasoning_content / Claude thinking）
    thinking: str | None = None
