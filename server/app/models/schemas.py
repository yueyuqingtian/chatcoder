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
    # v21: 是否显式开启 thinking 模式（对齐 deepseek-harness serialize.ts / zcode）。
    # 仅对支持 thinking 参数的网关生效（DeepSeek/GLM/Kimi 系），
    # agent_loop 根据 provider 能力 + effort 计算后传入；provider 据此发送
    # thinking:{type:"enabled", budget_tokens} 并移除 temperature（thinking 模式不支持）。
    # v29: 三态语义——True=显式开启；False=用户选择 none，要求**显式关闭**思考
    # （provider 按各自协议翻译成 disabled / enable_thinking=false 等关闭形态；
    # 协议本身无关闭字段的供应商保持不下发）；None=不触碰该字段（保持网关默认）。
    thinking: bool | None = None
    # TRAE Work 模式需要稳定的云端会话/消息标识；其它供应商忽略这些字段。
    session_id: str | None = None
    message_id: str | None = None
    # plan-270-1358: ta3（Ta+3 牛码）x-ws-id 伪装头需要工作目录指纹；
    # agent_loop 透传会话工作目录，其它供应商忽略。
    workspace_dir: str | None = None
    # plan-20261021-usage-update：本次请求的计时便笺（time.monotonic 秒）。agent_loop 写入 start，
    # Provider 在首个输出帧到达时写入 first（见 models.base.mark_first_token）。
    # 不参与序列化，也不影响任何请求参数。
    timing: dict[str, float] | None = Field(default=None, exclude=True)


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
    # v966: 本次流式响应收到的数据帧数（0 = 网关未下发任何帧=断流，用于空响应
    # 判定重试；None = provider 未提供该信号，保持旧判定逻辑）
    frames_received: int | None = None
    # plan-20261021-usage-update：流式响应的性能计时（毫秒）——请求发起 → 首个输出帧、
    # 首个输出帧 → 流结束。非流式 / 中断 / 超时的响应为 None（用量页按「无实测」处理）。
    first_token_ms: int | None = None
    gen_ms: int | None = None
