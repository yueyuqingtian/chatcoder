"""OpenAI 兼容 Provider — 基于 OpenAI 官方 SDK，stream 模式收集。

v5.2: 用 SDK 的 stream=True 模式收集完整响应，适配 SSE 网关。

核心发现：网关在 stream=False 时仍返回 SSE 格式(text/event-stream)，
导致 SDK 解析失败。但 SDK 的 stream=True 天然支持 SSE，所以始终用
stream=True，在内部收集所有 chunk 拼成完整响应。
"""
import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator

from openai import APIConnectionError, APIError, APITimeoutError, AsyncOpenAI

from app.core.config import settings
from app.models.base import ModelProvider
from app.models.schemas import ChatRequest, ChatResponse, Usage

logger = logging.getLogger(__name__)

# v28.1: httpx 网络层超时不再硬编码 120s——read 超时必须 >= chunk 空闲超时
# (provider_stream_idle_timeout)。旧固定 120s 小于默认 180s：长思考模型在思考
# 阶段静默（SSE 无 chunk）超过 120s 时 httpx ReadTimeout 先于 asyncio chunk
# 超时触发，配置形同虚设，表现为"长思考超过一定时间就报错"。取配置值并留
# 600s 下限，防止配置调小后网络层反而先于 chunk 超时收紧。
_DEFAULT_TIMEOUT = max(
    600.0,
    float(getattr(settings, "provider_stream_idle_timeout", 180) or 180),
)


def _new_call_id() -> str:
    """生成全局唯一 tool_call id。

    Gemini 等严格网关要求 function call id 在整段对话中唯一且非空。
    旧实现用 f"call_{idx:02d}" 兜底，idx 每轮从 0 开始，跨轮次会产生重复 id，
    网关返回 400 "Please ensure that function call ... has been called exactly once"。
    """
    return "call_" + uuid.uuid4().hex[:12]

# v21: thinking 模式思考预算（对齐 anthropic provider 的 effort→budget 映射；
# zcode 默认 budget 1024）。仅当 request.thinking=True 时使用。
_THINKING_BUDGET_BY_EFFORT = {
    "none": 0,
    "minimal": 2048,
    "low": 2048,
    "medium": 8192,
    "high": 16384,
    "xhigh": 32768,
    "max": 32768,
}
# 支持 thinking:{type:"enabled"} 参数的网关域名（对齐 zcode: deepseek.com/z.ai/bigmodel.cn/chatglm.site）
_THINKING_DOMAINS = ("deepseek.com", "z.ai", "bigmodel.cn", "chatglm.site", "moonshot.cn", "kimi.com")
# 支持 thinking 参数的模型名前缀（DeepSeek/GLM/Kimi 系）
_THINKING_MODEL_PREFIXES = ("deepseek", "glm", "kimi", "moonshot")


class OpenAICompatibleProvider(ModelProvider):
    """通过 OpenAI 官方 SDK (stream 模式) 兼容任意 baseURL。"""

    name = "openai_compatible"

    def __init__(self, *, api_key: str, base_url: str, model: str):
        self._default_model = model
        # v28: stream chunk 空闲超时改读配置——长思考模型（grok-4.6 等）chunk 间隔
        # 可能超过旧硬编码 30s，导致"运行中突然停止且无报错"。
        self._chunk_timeout = float(getattr(settings, "provider_stream_idle_timeout", 180) or 180)
        # v6.3: 不声明接受 br 压缩——打包版 brotlicffi 缺 Decompressor C 扩展，
        # 网关若返回 br 压缩流会直接崩，gzip/deflate 由 httpx 原生支持
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url.rstrip("/"),
            timeout=_DEFAULT_TIMEOUT,
            max_retries=3,
            default_headers={"Accept-Encoding": "gzip, deflate"},
        )
        self._base_url = base_url.rstrip("/").lower()
        self._model_name = model

    # v21: 判断网关/模型是否支持 thinking:{type:"enabled"} 参数。
    # 对齐 zcode 的域名启发式 + 模型名前缀；OpenAI/Anthropic 官方网关不在此列
    # （OpenAI 用 reasoning_effort、Anthropic 走独立 provider 的 extended thinking）。
    def supports_thinking(self) -> bool:
        if any(d in self._base_url for d in _THINKING_DOMAINS):
            return True
        name = (self._model_name or "").lower()
        return any(name.startswith(p) for p in _THINKING_MODEL_PREFIXES)

    @staticmethod
    def _thinking_budget(reasoning_effort: str | None) -> int:
        """effort 档位 → 思考预算 token（默认 1024，对齐 zcode）。"""
        if not reasoning_effort:
            from app.core.config import settings
            return settings.agent_thinking_budget_tokens
        return _THINKING_BUDGET_BY_EFFORT.get(
            reasoning_effort.lower(),
            _THINKING_BUDGET_BY_EFFORT.get("medium"),
        )

    def _apply_thinking(self, kwargs: dict, request: "ChatRequest") -> None:
        """request.thinking=True 时写入 thinking 参数并移除 temperature。

        对齐 deepseek-harness serialize.ts（thinking:{type:"enabled"}）与 zcode
        （thinking 开启时 temperature 不支持，置空由网关内部固定采样）。
        v23.1: 轻量网关（智谱 bigmodel 中转、LiteLLM 等）只认 thinking.type、
        不认识 budget_tokens，会直接 400 UNKNOWN_FIELD——budget_tokens 仅发给
        官方 Anthropic 网关（Anthropic extended thinking 协议原生要求该字段）。
        """
        if not request.thinking:
            return
        thinking: dict = {"type": "enabled"}
        if "api.anthropic.com" in self._base_url:
            thinking["budget_tokens"] = self._thinking_budget(request.reasoning_effort)
        # 必须走 extra_body：openai SDK 1.x 的 create() 签名不含 thinking 参数，
        # 直接传 thinking= 会抛 "AsyncCompletions.create() got an unexpected keyword argument 'thinking'"；
        # extra_body 会把字段合并进请求体，由网关解释。
        kwargs["extra_body"] = {"thinking": thinking}
        kwargs.pop("temperature", None)

    async def _create_compat(self, kwargs: dict):
        """v23.1: 思考参数的兼容性重试（保留思考深度配置，自动降级字段）。

        部分中转网关（9router/cmc 等）会把 reasoning_effort=max 转译为
        thinking.budget_tokens 注入上游，或对 thinking 子字段做白名单校验，
        触发 400 UNKNOWN_FIELD。此时自动降级重试一次：
        剥离 thinking.budget_tokens（仅保留 type:"enabled"），并将 max 降为 xhigh。
        思考深度能力不丢：支持该字段的网关（官方 Anthropic）仍在首次请求中携带。
        """
        try:
            return await self._client.chat.completions.create(**kwargs)
        except APIError as e:
            msg = str(getattr(e, "message", "") or "")
            unknown_thinking_field = getattr(e, "status_code", None) == 400 and (
                "budget_tokens" in msg or ("UNKNOWN_FIELD" in msg and "thinking" in msg)
            )
            if not unknown_thinking_field:
                raise
            retry = dict(kwargs)
            eb = dict(retry.get("extra_body") or {})
            th = eb.get("thinking")
            if isinstance(th, dict):
                eb["thinking"] = {k: v for k, v in th.items() if k != "budget_tokens"}
                retry["extra_body"] = eb
            if retry.get("reasoning_effort") == "max":
                retry["reasoning_effort"] = "xhigh"
            logger.warning("[provider] 网关拒绝 thinking 字段(400 UNKNOWN_FIELD)，降级重试一次")
            return await self._client.chat.completions.create(**retry)

    async def chat(self, request: ChatRequest) -> ChatResponse:
        """非流式调用，内部用 stream=True 收集。"""
        messages = self._convert_messages(request.messages)
        kwargs: dict = {
            "model": request.model or self._default_model,
            "messages": messages,
            "stream": True,  # ← 始终 stream=True
            "stream_options": {"include_usage": True},  # v1.2: 要求网关在末尾 chunk 返回 usage
        }
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
        if request.max_tokens:
            kwargs["max_tokens"] = request.max_tokens
        if request.tools:
            kwargs["tools"] = request.tools
            kwargs["tool_choice"] = "auto"
        # v6.0: 透传 reasoning_effort（对齐 codex thinking），none/None 不传以兼容老网关
        if request.reasoning_effort and request.reasoning_effort != "none":
            kwargs["reasoning_effort"] = request.reasoning_effort
        # v21: thinking 模式（DeepSeek/GLM 系）——发 thinking 参数并移除 temperature
        self._apply_thinking(kwargs, request)

        try:
            stream = await self._create_compat(kwargs)
        except APIConnectionError as e:
            logger.error("[provider] 连接失败: %s", e)
            raise RuntimeError(f"model gateway error (connection): {e}") from e
        except APITimeoutError as e:
            logger.error("[provider] 超时: %s", e)
            raise RuntimeError(f"model gateway error (timeout): {e}") from e
        except APIError as e:
            logger.error("[provider] API错误 %s: %s", getattr(e, 'status_code', '?'), e.message)
            raise RuntimeError(f"model gateway error (HTTP {getattr(e, 'status_code', '?')}): {e.message}") from e

        # 收集 stream chunks
        content_parts: list[str] = []
        thinking_parts: list[str] = []  # v4.3: 收集推理/思考内容
        tool_calls_map: dict[int, dict] = {}  # index → {id, name, arguments}
        finish_reason = "stop"
        usage_data = {"prompt": 0, "completion": 0, "total": 0}

        try:
            # v4.8.3: chat() 回退路径也加 chunk 超时，与 stream_structured 一致
            # v28: 超时值改读配置 provider_stream_idle_timeout（默认 180s，兼容长思考模型）
            _stream_iter = stream.__aiter__()
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        _stream_iter.__anext__(), timeout=self._chunk_timeout,
                    )
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    raise RuntimeError(
                        f"model gateway error: stream chunk timeout ({int(self._chunk_timeout)}s)"
                    ) from None
                # v4.5: 无论是否有 choices 都检查 usage
                if chunk.usage:
                    _prompt_details = getattr(chunk.usage, 'prompt_tokens_details', None)
                    _completion_details = getattr(chunk.usage, 'completion_tokens_details', None)
                    usage_data = {
                        "prompt": chunk.usage.prompt_tokens or 0,
                        "completion": chunk.usage.completion_tokens or 0,
                        "total": chunk.usage.total_tokens or 0,
                        # v1.2: 提取缓存输入 token（OpenAI prompt_cache_hit_tokens / DeepSeek cached_tokens）
                        "cached": (getattr(_prompt_details, 'cached_tokens', 0) or
                                   getattr(chunk.usage, 'prompt_cache_hit_tokens', 0) or
                                   getattr(chunk.usage, 'cached_tokens', 0) or 0),
                        # v1.2: 提取推理 token（completion_tokens_details.reasoning_tokens）
                        "reasoning": getattr(_completion_details, 'reasoning_tokens', 0) or 0,
                    }
                if not chunk.choices:
                    continue

                delta = chunk.choices[0].delta
                choice_fr = chunk.choices[0].finish_reason

                if delta:
                    # v4.3: 收集推理/思考内容（DeepSeek reasoning_content / Claude thinking）
                    _reasoning = getattr(delta, 'reasoning_content', None) or getattr(delta, 'thinking', None)
                    if _reasoning:
                        thinking_parts.append(_reasoning)
                    if delta.content:
                        content_parts.append(delta.content)
                    if delta.tool_calls:
                        for tc in delta.tool_calls:
                            idx = tc.index
                            if idx not in tool_calls_map:
                                tool_calls_map[idx] = {"id": "", "name": "", "arguments": ""}
                            if tc.id:
                                tool_calls_map[idx]["id"] = tc.id
                            if tc.function:
                                if tc.function.name:
                                    tool_calls_map[idx]["name"] += tc.function.name
                                if tc.function.arguments:
                                    tool_calls_map[idx]["arguments"] += tc.function.arguments

                if choice_fr:
                    finish_reason = choice_fr
        except Exception as e:
            logger.error("[provider] stream 收集异常: %s", e)
            raise RuntimeError(f"model gateway error (stream): {e}") from e

        # 组装 tool_calls
        tool_calls = []
        _seen_ids: set[str] = set()
        for idx in sorted(tool_calls_map.keys()):
            tc = tool_calls_map[idx]
            args_raw = tc["arguments"] or "{}"
            try:
                args = json.loads(args_raw) if args_raw else {}
            except (json.JSONDecodeError, TypeError):
                args = {"_raw": args_raw}
            # 网关偶发返回空/重复 id：uuid 兜底并去重，保证全局唯一
            call_id = tc["id"] or _new_call_id()
            if call_id in _seen_ids:
                call_id = _new_call_id()
            _seen_ids.add(call_id)
            tool_calls.append({
                "id": call_id,
                "name": tc["name"],
                "arguments": args,
            })

        content = "".join(content_parts) if content_parts else None
        thinking = "".join(thinking_parts) if thinking_parts else None  # v4.3: 推理内容

        # --- DSML 文本格式兜底解析 ---
        # DeepSeek/GLM 模型偶尔在 content 中返回 DSML 格式的工具调用，
        # 而不是结构化 tool_calls。这里检测并解析。
        if not tool_calls and content and "DSML" in content:
            parsed_calls = _parse_dsml_tool_calls(content)
            if parsed_calls:
                logger.info(
                    "[provider] 从 DSML 文本解析出 %d 个工具调用: %s",
                    len(parsed_calls),
                    [c["name"] for c in parsed_calls],
                )
                tool_calls = parsed_calls
                content = _strip_dsml_blocks(content)
                if not content:
                    content = None
                finish_reason = "tool_calls"

        # --- v3.6: 退化文本格式兜底解析 ---
        # 部分模型在多轮对话后退化为纯文本，"想"调工具但输出了自然语言。
        # 例如："fs_read path='/src/main.py'" 或 "我需要读取 main.py，搜索 User 类"
        # 检测并尝试解析为结构化 tool_calls。
        if not tool_calls and content:
            _degraded_calls = _parse_degraded_tool_calls(content)
            if _degraded_calls:
                logger.info(
                    "[provider] 从退化文本解析出 %d 个工具调用: %s",
                    len(_degraded_calls),
                    [c["name"] for c in _degraded_calls],
                )
                tool_calls = _degraded_calls
                finish_reason = "tool_calls"

        return ChatResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=Usage(
                prompt_tokens=usage_data.get("prompt", 0),
                completion_tokens=usage_data.get("completion", 0),
                total_tokens=usage_data.get("total", 0),
                cached_input_tokens=usage_data.get("cached", 0),
                reasoning_tokens=usage_data.get("reasoning", 0),
            ),
            model=request.model or self._default_model,
            thinking=thinking,  # v4.3
        )

    async def stream(self, request: ChatRequest) -> AsyncIterator[str]:
        """流式调用，直接透传 stream chunks。"""
        messages = self._convert_messages(request.messages)
        kwargs: dict = {
            "model": request.model or self._default_model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},  # v1.2: 要求网关在末尾 chunk 返回 usage
        }
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
        if request.max_tokens:
            kwargs["max_tokens"] = request.max_tokens
        if request.tools:
            kwargs["tools"] = request.tools
            kwargs["tool_choice"] = "auto"
        # v6.0: 透传 reasoning_effort（对齐 codex thinking），none/None 不传以兼容老网关
        if request.reasoning_effort and request.reasoning_effort != "none":
            kwargs["reasoning_effort"] = request.reasoning_effort
        # v21: thinking 模式（DeepSeek/GLM 系）——发 thinking 参数并移除 temperature
        self._apply_thinking(kwargs, request)

        try:
            stream = await self._create_compat(kwargs)
            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content
        except APIError as e:
            logger.error("[provider] 流式错误: %s", e)
            raise RuntimeError(f"model gateway error: {e.message}") from e

    async def stream_structured(self, request: ChatRequest) -> AsyncIterator[dict]:
        """v4.4: 结构化流式——yield dict with type/thinking/content/tool_call fields。

        yield 格式:
        - {"type": "thinking", "delta": "..."}
        - {"type": "content", "delta": "..."}
        - {"type": "done", "usage": Usage, "finish_reason": str, "tool_calls": [...]}
        """
        messages = self._convert_messages(request.messages)
        kwargs: dict = {
            "model": request.model or self._default_model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},  # v1.2: 要求网关在末尾 chunk 返回 usage
        }
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
        if request.max_tokens:
            kwargs["max_tokens"] = request.max_tokens
        if request.tools:
            kwargs["tools"] = request.tools
            kwargs["tool_choice"] = "auto"
        # v6.0: 透传 reasoning_effort（对齐 codex thinking），none/None 不传以兼容老网关
        if request.reasoning_effort and request.reasoning_effort != "none":
            kwargs["reasoning_effort"] = request.reasoning_effort
        # v21: thinking 模式（DeepSeek/GLM 系）——发 thinking 参数并移除 temperature
        self._apply_thinking(kwargs, request)

        try:
            s = await self._create_compat(kwargs)
        except APIError as e:
            logger.error("[provider] 流式错误: %s", e)
            raise RuntimeError(f"model gateway error: {e.message}") from e

        content_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_calls_map: dict[int, dict] = {}
        finish_reason = "stop"
        usage_data = Usage()

        # v4.8: 流式读取加 chunk 超时，防止网关半开连接导致永久挂起
        # v28: 超时值改读配置 provider_stream_idle_timeout（默认 180s，兼容长思考模型）
        _stream_iter = s.__aiter__()
        while True:
            try:
                chunk = await asyncio.wait_for(
                    _stream_iter.__anext__(), timeout=self._chunk_timeout,
                )
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:
                raise RuntimeError(
                    f"model gateway error: stream chunk timeout ({int(self._chunk_timeout)}s)"
                ) from None
            # v4.5: 无论是否有 choices 都检查 usage（某些网关把 usage 放在最后有 choices 的 chunk）
            if chunk.usage:
                _prompt_details = getattr(chunk.usage, 'prompt_tokens_details', None)
                _completion_details = getattr(chunk.usage, 'completion_tokens_details', None)
                _cached = (getattr(_prompt_details, 'cached_tokens', None) or
                           getattr(chunk.usage, 'prompt_cache_hit_tokens', None) or
                           getattr(chunk.usage, 'cached_tokens', None) or 0)
                _reasoning = getattr(_completion_details, 'reasoning_tokens', None) or 0
                usage_data = Usage(
                    prompt_tokens=chunk.usage.prompt_tokens or 0,
                    completion_tokens=chunk.usage.completion_tokens or 0,
                    total_tokens=chunk.usage.total_tokens or 0,
                    cached_input_tokens=_cached,
                    reasoning_tokens=_reasoning,
                )
            if not chunk.choices:
                continue

            delta = chunk.choices[0].delta
            choice_fr = chunk.choices[0].finish_reason

            if delta:
                # v4.4: 思考内容实时广播
                _reasoning = getattr(delta, 'reasoning_content', None) or getattr(delta, 'thinking', None)
                if _reasoning:
                    thinking_parts.append(_reasoning)
                    yield {"type": "thinking", "delta": _reasoning}
                
                if delta.content:
                    content_parts.append(delta.content)
                    yield {"type": "content", "delta": delta.content}
                
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        if idx not in tool_calls_map:
                            tool_calls_map[idx] = {"id": "", "name": "", "arguments": ""}
                        if tc.id:
                            tool_calls_map[idx]["id"] = tc.id
                        if tc.function:
                            if tc.function.name:
                                tool_calls_map[idx]["name"] += tc.function.name
                            if tc.function.arguments:
                                tool_calls_map[idx]["arguments"] += tc.function.arguments

            if choice_fr:
                finish_reason = choice_fr

        # 组装 tool_calls
        tool_calls = []
        _seen_ids: set[str] = set()
        for idx in sorted(tool_calls_map.keys()):
            tc = tool_calls_map[idx]
            args_raw = tc["arguments"] or "{}"
            try:
                args = json.loads(args_raw) if args_raw else {}
            except (json.JSONDecodeError, TypeError):
                args = {"_raw": args_raw}
            # 网关偶发返回空/重复 id：uuid 兜底并去重，保证全局唯一
            call_id = tc["id"] or _new_call_id()
            if call_id in _seen_ids:
                call_id = _new_call_id()
            _seen_ids.add(call_id)
            tool_calls.append({
                "id": call_id,
                "name": tc["name"],
                "arguments": args,
            })

        content = "".join(content_parts) if content_parts else None
        thinking = "".join(thinking_parts) if thinking_parts else None

        # v6.2: 退化文本兜底解析（对齐 chat() 路径）——
        # agent loop 主路径走 stream_structured，模型以纯文本输出工具意图时
        # （DSML 格式 / "cd xxx && ..." 命令行），直接解析为结构化 tool_calls，
        # 避免被当成最终回答或反复引导重试。
        if not tool_calls and content:
            _degraded = _parse_dsml_tool_calls(content) if "DSML" in content else []
            if not _degraded:
                _degraded = _parse_degraded_tool_calls(content)
            if _degraded:
                logger.info(
                    "[provider] 流式退化文本解析出 %d 个工具调用: %s",
                    len(_degraded),
                    [c["name"] for c in _degraded],
                )
                tool_calls = _degraded
                content = None
                finish_reason = "tool_calls"

        yield {
            "type": "done",
            "content": content,
            "thinking": thinking,
            "tool_calls": tool_calls,
            "finish_reason": finish_reason,
            "usage": usage_data,
        }

    def _convert_messages(self, messages) -> list[dict]:
        """将 ChatMessage 列表转为 OpenAI SDK 格式。

        v6.1: developer 角色处理（对齐 codex ContextualUserFragment）：
        - settings.use_developer_role=True 时透传 role="developer"
        - False（默认）时转成 role="system"，兼容只认 system/user/assistant/tool 的网关

        SDK 接受原生 dict 格式，自动处理所有边界情况：
        - assistant 带 tool_calls 时 content 为 null
        - tool results 格式正确
        """
        from app.core.config import settings

        result = []
        for m in messages:
            if m.role == "developer":
                # developer -> system（保持独立消息，不合并，实现分层效果）
                result.append({"role": "system" if not settings.use_developer_role else "developer",
                               "content": m.content or ""})
            elif m.role == "assistant" and m.tool_calls:
                # assistant 带 tool_calls
                # 兜底 id 必须非空且同消息内唯一（旧实现 "call_default" 在多条
                # 无 id 调用时重复 → Gemini 400 "called exactly once"）
                tc_list = []
                _seen_ids: set[str] = set()
                for tc in m.tool_calls:
                    fn_args = tc.get("arguments", {})
                    if isinstance(fn_args, dict):
                        fn_args = json.dumps(fn_args, ensure_ascii=False)
                    call_id = tc.get("id") or _new_call_id()
                    if call_id in _seen_ids:
                        call_id = _new_call_id()
                    _seen_ids.add(call_id)
                    tc_list.append({
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": tc.get("name", ""),
                            "arguments": fn_args or "{}",
                        },
                    })
                result.append({
                    "role": "assistant",
                    # v21: 纯 tool_call 回合 content 发空串而非 null（对齐 deepseek-harness
                    # serializeAssistant：部分网关（DeepSeek 系）直接 reject null content）
                    "content": m.content or "",
                    "tool_calls": tc_list,
                    # v1.2: thinking 模式网关（DeepSeek/GLM 经 LiteLLM）要求把
                    # 历史 assistant 的 reasoning_content 原样回传，否则 400
                    **({"reasoning_content": m.reasoning_content} if m.reasoning_content else {}),
                })
            elif m.role == "tool":
                result.append({
                    "role": "tool",
                    "content": m.content or "",
                    "tool_call_id": m.tool_call_id or "",
                })
            elif m.content_blocks:
                parts = []
                if m.content:
                    parts.append({"type": "text", "text": m.content})
                parts.extend(m.content_blocks)
                result.append({"role": m.role, "content": parts})
            else:
                _d = {"role": m.role, "content": m.content or ""}
                # v1.2: 纯文本 assistant 也回传 reasoning_content（thinking 模式网关要求）
                if m.role == "assistant" and m.reasoning_content:
                    _d["reasoning_content"] = m.reasoning_content
                result.append(_d)

        return result


# ---------------------------------------------------------------------------
# DSML 文本格式解析（DeepSeek / 智谱等模型偶尔返回的格式）
# ---------------------------------------------------------------------------
# 格式示例:
#   <｜｜DSML｜｜tool_calls>
#   <｜｜DSML｜｜invoke name="fs_read">
#   <｜｜DSML｜｜parameter name="path" string="true">clinic/pom.xml</｜｜DSML｜｜parameter>
#   <｜｜DSML｜｜parameter name="offset" string="false">100</｜｜DSML｜｜parameter>
#   </｜｜DSML｜｜invoke>
#   </｜｜DSML｜｜/tool_calls>

import re as _re

# 注意：DSML 用的是全角竖线 ｜ (U+FF5C)，不是 ASCII 的 | (U+007C)
# 格式：<｜｜DSML｜｜invoke name="...">
#      ^  ^  ^^^^  ^  ^
#      <  2 pipe  DSML  2 pipe  invoke
_PIPE2 = r'[\uFF5C|]{2}'  # 2 个竖线（全角或半角）

# invoke 标签：name="tool_name"
_DSML_INVOKE_RE = _re.compile(
    r'<' + _PIPE2 + r'DSML' + _PIPE2 + r'invoke\s+name="([^"]+)"\s*>(.*?)'
    + r'</' + _PIPE2 + r'DSML' + _PIPE2 + r'invoke\s*>',
    _re.DOTALL,
)
# parameter 标签：name="param_name" (可选 string="true/false")
# 开标签: <｜｜DSML｜｜parameter name="path" string="true">
# 闭标签: </｜｜DSML｜｜parameter>
_DSML_PARAM_RE = _re.compile(
    r'<' + _PIPE2 + r'DSML' + _PIPE2 + r'parameter\s+name="([^"]+)"(?:[^>]*)>(.*?)'
    + r'</' + _PIPE2 + r'DSML' + _PIPE2 + r'parameter\s*>',
    _re.DOTALL,
)
# 整个 tool_calls 块
# 开: <｜｜DSML｜｜tool_calls>
# 闭: </｜｜DSML｜｜/tool_calls>
_DSML_BLOCK_RE = _re.compile(
    r'<' + _PIPE2 + r'DSML' + _PIPE2 + r'tool_calls>.*?</' + _PIPE2 + r'DSML' + _PIPE2 + r'/tool_calls\s*>',
    _re.DOTALL,
)


def _parse_dsml_tool_calls(content: str) -> list[dict]:
    """解析 DSML 文本格式的工具调用。

    返回结构化 tool_calls 列表，格式与 OpenAI tool_calls 一致：
    [{"id": "...", "name": "...", "arguments": {...}}]
    """
    calls = []
    for match in _DSML_INVOKE_RE.finditer(content):
        tool_name = match.group(1).strip()
        params_block = match.group(2)

        args = {}
        for pmatch in _DSML_PARAM_RE.finditer(params_block):
            p_name = pmatch.group(1).strip()
            p_value = pmatch.group(2).strip()

            # 尝试转为数字
            try:
                if "." in p_value:
                    args[p_name] = float(p_value)
                else:
                    args[p_name] = int(p_value)
            except ValueError:
                # 布尔
                if p_value.lower() == "true":
                    args[p_name] = True
                elif p_value.lower() == "false":
                    args[p_name] = False
                else:
                    args[p_name] = p_value

        calls.append({
            "id": _new_call_id(),
            "name": tool_name,
            "arguments": args,
        })

    return calls


def _strip_dsml_blocks(content: str) -> str:
    """从 content 中移除 DSML 块，保留纯文本部分。"""
    # 移除整个 tool_calls 块
    cleaned = _DSML_BLOCK_RE.sub("", content)
    # 移除残留的单个 DSML 标签
    cleaned = _re.sub(r'</?' + _PIPE2 + r'/?DSML' + _PIPE2 + r'[^>]*>', "", cleaned)
    # 清理多余空行
    cleaned = _re.sub(r'\n{3,}', '\n\n', cleaned)
    return cleaned.strip()


# ---------------------------------------------------------------------------
# v3.6: 退化文本格式解析（模型输出自然语言描述工具调用意图）
# ---------------------------------------------------------------------------
# 多轮对话后部分模型退化为纯文本，输出类似：
#   "fs_read path='/src/main.py'" 
#   或 "我需要读取 /src/main.py 文件"
#   或 "接下来执行 {"name":"fs_read","arguments":{"path":"main.py"}}"
# 检测并尝试解析为结构化 tool_calls。

# 已知工具名列表（从 tool registry 映射）
_KNOWN_TOOLS = {
    "fs_read", "fs_list", "fs_write", "fs_grep", "git_diff",
    "terminal_exec", "web_fetch", "web_search", "ci_run",
    "memory_search", "view_image", "editor_apply_diff",
}

# 模式1: tool_name(arg1=val1, arg2=val2) 或 tool_name path='/path' 格式
_TOOL_CALL_LIKE_RE = _re.compile(
    r'(?:^|[\s\n])(' + '|'.join(_KNOWN_TOOLS) + r')'
    r'\s*\(?(.*?)\)?(?:\s*$|\n|。|，)',
    _re.MULTILINE,
)

# 模式2: 内嵌的 JSON tool_call
_EMBEDDED_JSON_CALL_RE = _re.compile(
    r'\{"name"\s*:\s*"(' + '|'.join(_KNOWN_TOOLS) + r')"\s*,'
    r'\s*"arguments"\s*:\s*(\{[^}]+\})\s*\}',
    _re.DOTALL,
)

# 工具意图词（匹配"我需要读取...文件"这种自然语言模式）
_INTENT_PATTERNS = [
    (_re.compile(r'((?:我(?:需要|准备|将|先|会|要)|接下来|继续|下一步|现在)\s*(?:读取|查看|搜索|检查|分析|审阅))\s+(.+?)[\n。，]'), "fs_read"),
    (_re.compile(r'(?:列出|显示|查看)\s*(?:目录|文件列表)\s*(.+?)[\n。，]'), "fs_list"),
    (_re.compile(r'(?:写入|创建|生成|保存)\s*(?:文件)?\s*(.+?)'), "fs_write"),
]

# v6.2: 命令行前缀（模型把 terminal_exec 调用退化为命令文本时识别）
_CMD_PREFIX_RE = _re.compile(
    r'^(?:cd|chdir|git|ls|cat|find|grep|echo|npm|pnpm|yarn|node|python|docker|'
    r'mvn|gradle|codegraph|npx|pip|curl|wget|powershell|cmd)\b',
    _re.IGNORECASE,
)


def _parse_degraded_tool_calls(content: str) -> list[dict]:
    """v6.2: 从退化文本中解析工具调用意图。

    返回结构化 tool_calls 列表，或空列表（无可解析内容）。
    新增策略4：命令行文本（cd xxx && ...）→ terminal_exec，
    一步到位转为结构化调用，避免多次引导重试仍失败。
    """
    if not content or len(content.strip()) < 10:
        return []

    calls = []

    # 策略1: 内嵌 JSON 格式（最高优先级，最可靠）
    for match in _EMBEDDED_JSON_CALL_RE.finditer(content):
        tool_name = match.group(1)
        args_raw = match.group(2)
        try:
            args = json.loads(args_raw)
        except (json.JSONDecodeError, TypeError):
            args = {}
        calls.append({
            "id": _new_call_id(),
            "name": tool_name,
            "arguments": args,
        })

    if calls:
        return calls

    # 策略2: tool_name('path/to/file') 格式
    for match in _TOOL_CALL_LIKE_RE.finditer(content):
        tool_name = match.group(1)
        params_str = match.group(2).strip() if match.group(2) else ""
        args = _coerce_simple_args(tool_name, params_str)
        if args is not None:
            calls.append({
                "id": _new_call_id(),
                "name": tool_name,
                "arguments": args,
            })

    if calls:
        return calls

    # 策略4: 命令行文本 -> terminal_exec（v6.2 新增）
    # 模型常见退化："cd /path && echo ... | codegraph serve --mcp" 这类命令链
    # 直接解析为 terminal_exec(command=整行)，工具内部会提取 cwd 并移除 cd 前缀。
    lines = [l.strip() for l in content.splitlines() if l.strip()]
    cmd_lines = [l for l in lines if _CMD_PREFIX_RE.match(l) and len(l) < 1000]
    if cmd_lines and len(content) < 800:
        # 多条命令行用 " && " 连接（terminal_exec 支持命令链）
        full_cmd = " && ".join(cmd_lines)
        calls.append({
            "id": _new_call_id(),
            "name": "terminal_exec",
            "arguments": {"command": full_cmd},
        })
        return calls

    # 策略3: 自然语言意图 -> 映射到工具
    # v6.0: 收紧触发条件，避免把正常报告误解析为工具调用
    # 仅在内容较短（<300）且意图出现在开头（前 120 字符）时才尝试
    if len(content) < 300:
        for pattern, tool_name in _INTENT_PATTERNS:
            match = pattern.search(content[:120])
            if match:
                target = match.group(2).strip().strip("'\"") if match.lastindex >= 2 else ""
                # 提取可能的文件路径
                path_match = _re.search(r'[\w./-]+\.[\w]+', target)
                if path_match:
                    args = {"path": path_match.group(0)}
                    if tool_name in ("fs_list",):
                        args = {"path": str(path_match.group(0)).rsplit("/", 1)[0] or "."}
                    calls.append({
                        "id": _new_call_id(),
                        "name": tool_name,
                        "arguments": args,
                    })
                    break

    return calls


def _coerce_simple_args(tool_name: str, params_str: str) -> dict | None:
    """将简单参数串转为 dict。"""
    if not params_str:
        return {"path": "."} if tool_name in ("fs_list",) else None

    args = {}

    # 尝试 key=value 格式
    kv_pairs = _re.findall(r'(\w+)\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|(\S+))', params_str)
    for key, v1, v2, v3 in kv_pairs:
        val = v1 or v2 or v3
        args[key] = val

    if args:
        return args

    # 尝试单独的字符串作为 path
    clean = params_str.strip().strip("'\"")
    if clean and not clean.startswith("{") and not clean.startswith("("):
        return {"path": clean}

    return None

