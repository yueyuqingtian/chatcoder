"""Anthropic Message API 兼容 Provider。

支持 Anthropic 官方 /v1/messages 接口格式，同时也兼容以 Anthropic 格式暴露的代理网关。
核心区别:
- 系统提示放在 top-level system 字段，而非 messages 数组。
- 多模态内容使用 {type: "image", source: {type: "base64", media_type, data}} 格式。
- 响应体格式: {content: [{type: "text", text: "..."}], stop_reason, usage}
"""
import json
import logging
import re
import uuid
from collections.abc import AsyncIterator

import httpx

from app.models.base import ModelProvider, mark_first_token
from app.models.providers.temperature_policy import pick_temperature
from app.models.providers.tool_args import parse_tool_args
from app.models.schemas import ChatRequest, ChatResponse, Usage

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 120.0

# Claude 5.x 起 extended thinking 换成 adaptive 协议（plan-20261017-claude-thinking）：
# - thinking.type 只认 "adaptive"，旧的 "enabled" 会被 400 拒绝
#   （"requires adaptive thinking; omit thinking or use thinking.type=adaptive"）；
# - 必须显式带 display="summarized"，否则上游只回 signature、thinking 文本全为空串，
#   前端表现为"没有思考块"；
# - 思考深度改由 output_config.effort 控制，合法值 low/medium/high/xhigh/max（none 会 400）。
_ADAPTIVE_MODEL_RE = re.compile(r"claude-(?:opus|sonnet|haiku)?-?(\d+)", re.IGNORECASE)
_OUTPUT_EFFORTS = ("low", "medium", "high", "xhigh", "max")


def _uses_adaptive_thinking(model: str) -> bool:
    """模型是否使用 adaptive thinking 协议（Claude 5.x 起的主版本号判定）。"""
    match = _ADAPTIVE_MODEL_RE.search(model or "")
    if not match:
        return False
    try:
        return int(match.group(1)) >= 5
    except (TypeError, ValueError):
        return False


def _output_effort(effort: str) -> str:
    """chatcoder 档位 → output_config.effort 合法值（调用方已排除 none）。"""
    value = (effort or "").strip().lower()
    if value in _OUTPUT_EFFORTS:
        return value
    # minimal 是 OpenAI 侧档位，Anthropic 无对应值，按最保守的 low 处理
    return "low" if value == "minimal" else "high"


def _degrade_thinking_payload(payload: dict, message: str) -> dict | None:
    """按上游 400 报错降级思考参数；返回 None 表示没有可降级的字段。

    中转网关对 thinking 子字段的白名单各不相同（v23.1 已记录 budget_tokens 一类），
    不能因为某个字段不认识就让整轮对话失败。
    """
    lowered = (message or "").lower()
    degraded = dict(payload)

    if "requires adaptive thinking" in lowered:
        thinking = degraded.get("thinking")
        if isinstance(thinking, dict) and thinking.get("type") == "adaptive":
            return None
        degraded["thinking"] = {"type": "adaptive", "display": "summarized"}
        degraded.setdefault("output_config", {"effort": "high"})
        return degraded

    if "output_config" in lowered:
        if "output_config" not in degraded:
            return None
        degraded.pop("output_config", None)
        return degraded

    if "budget_tokens" in lowered or ("unknown_field" in lowered and "thinking" in lowered):
        thinking = degraded.get("thinking")
        if isinstance(thinking, dict) and "budget_tokens" in thinking:
            degraded["thinking"] = {k: v for k, v in thinking.items() if k != "budget_tokens"}
            return degraded
    return None


def _new_call_id() -> str:
    """生成唯一 tool_call id（网关偶尔不给 id，缺失时不能让多条调用撞车）。"""
    return "call_" + uuid.uuid4().hex[:12]


def _merge_stream_text(current: str, chunk: str) -> str:
    """合并流式工具参数增量，兼容标准增量 / 累计式 / 重复帧三种网关行为。

    Anthropic 官方是标准增量，但中转网关会重复投递「截至目前的完整内容」；
    直接累加会拼出重复 JSON，parse_tool_args 解析失败后整条工具调用作废——
    用户看到的表现就是「模型说了一句话就不动了」。
    """
    if not chunk:
        return current
    if not current:
        return chunk
    if chunk == current:
        return current
    if len(chunk) > len(current) and chunk.startswith(current):
        return chunk
    return current + chunk


class AnthropicProvider(ModelProvider):
    """Anthropic Message API Provider。"""

    name = "anthropic"

    def __init__(self, *, api_key: str, base_url: str, model: str,
                 client_kwargs: dict | None = None):
        # plan-110-438 M3: 供应商代理配置注入每次请求的 httpx 客户端
        self._client_kwargs = dict(client_kwargs or {})
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._default_model = model
        self._endpoint = f"{self._base_url}/messages"

    def _headers(self) -> dict:
        return {
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

    def _build_payload(self, request: ChatRequest, stream: bool) -> dict:
        """构造 Anthropic /v1/messages 请求体。"""
        system_parts: list[str] = []
        messages: list[dict] = []

        for m in request.messages:
            # v6.1: developer 角色（对齐 codex）——Anthropic 无 developer 概念，合并到顶层 system
            if m.role in ("system", "developer"):
                if m.content:
                    system_parts.append(m.content)
                continue

            if m.role == "tool":
                messages.append({
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": m.tool_call_id or "",
                            "content": m.content,
                        }
                    ],
                })
                continue

            if m.role == "assistant" and m.tool_calls:
                assistant_content: list[dict] = []
                if m.content:
                    assistant_content.append({"type": "text", "text": m.content})
                for tc in m.tool_calls:
                    assistant_content.append({
                        "type": "tool_use",
                        "id": tc.get("id", "call_default"),
                        "name": tc.get("name", ""),
                        "input": tc.get("arguments", {}),
                    })
                messages.append({"role": "assistant", "content": assistant_content})
                continue

            # v1.0: 多模态内容块支持
            if m.content_blocks:
                parts: list[dict] = []
                if m.content:
                    parts.append({"type": "text", "text": m.content})
                for block in m.content_blocks:
                    if block.get("type") == "text":
                        parts.append({"type": "text", "text": block.get("text", "")})
                    elif block.get("type") in ("image_url", "image"):
                        # OpenAI 格式 → Anthropic 格式转换
                        img_data = block.get("image_url", {}).get("url", "") if block.get("type") == "image_url" else block.get("source", {}).get("data", "")
                        media_type = "image/png"
                        raw_data = ""
                        if img_data.startswith("data:"):
                            header, _, raw_data = img_data.partition(",")
                            if ";" in header:
                                media_type = header.split(":")[1].split(";")[0]
                        else:
                            raw_data = img_data
                        parts.append({
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": raw_data,
                            },
                        })
                    elif block.get("type") == "image":
                        parts.append(block)
                messages.append({"role": m.role, "content": parts})
            else:
                messages.append({"role": m.role, "content": m.content})

        payload: dict = {
            "model": request.model or self._default_model,
            "messages": messages,
            "stream": stream,
        }
        # 不限制输出：未显式配置时不传 max_tokens，由网关/模型自身决定上限。
        # 例外：官方 api.anthropic.com 该字段必填（缺失会 400），保留兜底值。
        if request.max_tokens:
            payload["max_tokens"] = request.max_tokens
        elif "api.anthropic.com" in self._base_url.lower():
            payload["max_tokens"] = 4096
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        # Claude/Anthropic 系只接受默认温度，非默认值（agent 循环的 0.3/0.7）
        # 会被网关 400 "does not support non-default temperature"。
        _temp = pick_temperature(
            request.temperature,
            model_name=request.model,
            extra_names=(self._default_model,),
            base_url=self._base_url,
        )
        if _temp is not None:
            payload["temperature"] = _temp
        # v6.0: extended thinking（对齐 claude code），reasoning_effort 非 none 时启用
        # v29: 用户显式选择档位 none（thinking=False）时下发 disabled —— Anthropic
        # extended thinking 协议原生支持该关闭形态；此前只做"不传"，网关/模型默认
        # 仍可能开思考导致"关不掉"。
        _effort = (request.reasoning_effort or "").strip().lower()
        if _effort and _effort != "none":
            _model = request.model or self._default_model
            if _uses_adaptive_thinking(_model):
                # plan-20261017-claude-thinking: Claude 5.x 用 adaptive 协议，
                # display=summarized 必带——缺失时上游只回 signature，思考文本全为空。
                payload["thinking"] = {"type": "adaptive", "display": "summarized"}
                payload["output_config"] = {"effort": _output_effort(_effort)}
            else:
                budget = {
                    "low": 2048, "medium": 8192, "high": 16384,
                    "xhigh": 32768, "max": 32768,
                }.get(_effort, 8192)
                # v23.1: 中转网关（如 9router）对 Anthropic 请求体做严格白名单校验，
                # thinking.budget_tokens 不在其中会直接 400 UNKNOWN_FIELD；
                # 官方 api.anthropic.com 的 extended thinking 原生要求该字段，仅官方网关携带。
                _official = "api.anthropic.com" in self._base_url.lower()
                payload["thinking"] = (
                    {"type": "enabled", "budget_tokens": budget}
                    if _official else {"type": "enabled"}
                )
                if _official and payload.get("max_tokens", 0) <= budget:
                    payload["max_tokens"] = budget + 4096
            # Anthropic thinking 要求 temperature=1（adaptive 同样只接受默认温度）
            payload["temperature"] = 1
        elif request.thinking is False or _effort == "none":
            payload["thinking"] = {"type": "disabled"}
        if request.tools:
            payload["tools"] = [
                {
                    "name": t.get("function", {}).get("name", ""),
                    "description": t.get("function", {}).get("description", ""),
                    "input_schema": t.get("function", {}).get("parameters", {"type": "object", "properties": {}}),
                }
                for t in request.tools
                if t.get("type") == "function"
            ]
        return payload

    async def chat(self, request: ChatRequest) -> ChatResponse:
        payload = self._build_payload(request, stream=False)
        headers = self._headers()
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT,
                                     headers={"Accept-Encoding": "gzip, deflate"},
                                     **self._client_kwargs) as client:
            resp = await client.post(self._endpoint, json=payload, headers=headers)
            # plan-20261017-claude-thinking: 网关对 thinking 字段的校验差异较大，
            # 400 时按报错提示降级重试（最多两次），避免整轮对话直接失败。
            for _ in range(2):
                if resp.status_code != 400:
                    break
                degraded = _degrade_thinking_payload(payload, resp.text)
                if degraded is None:
                    break
                logger.warning(
                    "[anthropic] 网关 400，降级思考参数重试 thinking=%s output_config=%s",
                    degraded.get("thinking"), degraded.get("output_config"),
                )
                payload = degraded
                resp = await client.post(self._endpoint, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

        content_blocks = data.get("content") or []
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_calls: list[dict] = []
        for block in content_blocks:
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "thinking":
                # 非流式回退路径此前只认 text/tool_use，思考块被整段丢弃
                _thought = block.get("thinking") or ""
                if _thought:
                    thinking_parts.append(_thought)
            elif block.get("type") == "tool_use":
                tool_calls.append({
                    "id": block.get("id", "call_default"),
                    "name": block.get("name", ""),
                    "arguments": block.get("input", {}),
                })

        usage_raw = data.get("usage") or {}
        prompt_t = int(usage_raw.get("input_tokens", 0))
        completion_t = int(usage_raw.get("output_tokens", 0))
        # v1.2: Anthropic 缓存 token 提取
        cache_creation_t = int(usage_raw.get("cache_creation_input_tokens", 0))
        cache_read_t = int(usage_raw.get("cache_read_input_tokens", 0))
        # adaptive thinking 的思考量在 output_tokens_details.thinking_tokens
        _details = usage_raw.get("output_tokens_details") or {}
        reasoning_t = int(_details.get("thinking_tokens", 0) or 0)

        # Anthropic 用 stop_reason=tool_use 表示本轮有工具调用，对齐 OpenAI 的
        # finish_reason=tool_calls（agent_loop 的畸形流检测按该值判断）
        _stop_reason = data.get("stop_reason") or "stop"
        if _stop_reason == "tool_use":
            _stop_reason = "tool_calls"

        return ChatResponse(
            content="\n".join(text_parts),
            tool_calls=tool_calls,
            finish_reason=_stop_reason,
            usage=Usage(
                prompt_tokens=prompt_t,
                completion_tokens=completion_t,
                total_tokens=prompt_t + completion_t + cache_creation_t + cache_read_t,
                cached_input_tokens=cache_read_t,
                reasoning_tokens=reasoning_t,
            ),
            model=request.model or self._default_model,
            thinking="\n".join(thinking_parts) or None,
        )

    async def stream(self, request: ChatRequest) -> AsyncIterator[str]:
        payload = self._build_payload(request, stream=True)
        headers = self._headers()
        timeout = httpx.Timeout(_DEFAULT_TIMEOUT, read=None)
        async with httpx.AsyncClient(timeout=timeout,
                                     headers={"Accept-Encoding": "gzip, deflate"},
                                     **self._client_kwargs) as client:
            async with client.stream("POST", self._endpoint, json=payload, headers=headers) as resp:
                resp.raise_for_status()
                event_type = ""
                async for line in resp.aiter_lines():
                    s = line.strip()
                    if not s:
                        continue
                    if s.startswith("event:"):
                        event_type = s[6:].strip()
                        continue
                    if s.startswith("data:"):
                        body = s[5:].strip()
                        try:
                            chunk = json.loads(body)
                        except (json.JSONDecodeError, TypeError):
                            continue
                        if event_type == "content_block_delta":
                            delta = chunk.get("delta") or {}
                            if delta.get("type") == "text_delta" and delta.get("text"):
                                yield delta["text"]
                        elif event_type == "message_stop":
                            break

    async def stream_structured(self, request: ChatRequest) -> AsyncIterator[dict]:
        """v3: 结构化流式，对齐 OpenAI provider 的 yield 格式。

        yield:
        - {"type": "thinking", "delta": "..."}  (thinking_delta)
        - {"type": "content", "delta": "..."}   (text_delta)
        - {"type": "done", "usage": Usage, "finish_reason": str, "tool_calls": [...]}
        """
        payload = self._build_payload(request, stream=True)
        headers = self._headers()
        timeout = httpx.Timeout(_DEFAULT_TIMEOUT, read=None)
        # plan-20261017-claude-thinking: 400 时按报错降级思考参数重试（最多两次）。
        # 400 发生在首个事件之前，不会造成重复下发。
        for attempt in range(3):
            async with (
                httpx.AsyncClient(
                    timeout=timeout,
                    headers={"Accept-Encoding": "gzip, deflate"},
                    **self._client_kwargs,
                ) as client,
                client.stream("POST", self._endpoint, json=payload, headers=headers) as resp,
            ):
                if resp.status_code == 400:
                    body_text = (await resp.aread()).decode("utf-8", "replace")
                    degraded = (
                        _degrade_thinking_payload(payload, body_text)
                        if attempt < 2 else None
                    )
                    if degraded is not None:
                        logger.warning(
                            "[anthropic] 网关 400，降级思考参数重试 "
                            "thinking=%s output_config=%s",
                            degraded.get("thinking"), degraded.get("output_config"),
                        )
                        payload = degraded
                        continue
                    raise RuntimeError(f"model gateway error (HTTP 400): {body_text[:500]}")
                resp.raise_for_status()
                event_type = ""
                prompt_t = 0
                completion_t = 0
                cache_creation_t = 0
                cache_read_t = 0
                reasoning_t = 0
                # plan-20261017-claude-tools: Anthropic 流式工具调用由
                # content_block_start(tool_use) → input_json_delta(partial_json) → stop
                # 三段组成，此前只解析 text/thinking 增量，tool_calls 恒为空列表。
                # 结果：模型输出一句话并准备调用工具时，agent_loop 看不到任何工具调用，
                # 直接判定"本轮结束"——用户看到的就是「回复一句话就中断、无报错」。
                tool_blocks: dict[int, dict] = {}
                finish_reason = "stop"
                async for line in resp.aiter_lines():
                    s = line.strip()
                    if not s:
                        continue
                    if s.startswith("event:"):
                        event_type = s[6:].strip()
                        continue
                    if s.startswith("data:"):
                        body = s[5:].strip()
                        try:
                            chunk = json.loads(body)
                        except (json.JSONDecodeError, TypeError):
                            continue
                        if event_type == "content_block_start":
                            block = chunk.get("content_block") or {}
                            if block.get("type") == "tool_use":
                                mark_first_token(request)  # plan-20261021-usage-update：工具调用块起始即首字
                                idx = int(chunk.get("index") or 0)
                                new_id = str(block.get("id") or "")
                                slot = tool_blocks.get(idx)
                                # 网关可能把所有工具都塞在 index=0，按 id 另开槽位
                                if (slot is not None and new_id and slot.get("id")
                                        and new_id != slot["id"]):
                                    idx = max(tool_blocks.keys(), default=-1) + 1
                                    slot = None
                                if slot is None:
                                    slot = {"id": "", "name": "", "arguments": ""}
                                    tool_blocks[idx] = slot
                                if new_id:
                                    slot["id"] = new_id
                                _name = block.get("name")
                                if isinstance(_name, str) and _name:
                                    slot["name"] = _name
                                # 部分网关在 start 帧直接给完整 input，没有后续增量帧
                                _input = block.get("input")
                                if isinstance(_input, dict) and _input:
                                    slot["arguments"] = json.dumps(_input, ensure_ascii=False)
                                elif (isinstance(_input, str) and _input.strip()
                                      and _input.strip() != "{}"):
                                    slot["arguments"] = _input
                        elif event_type == "content_block_delta":
                            delta = chunk.get("delta") or {}
                            dt = delta.get("type")
                            if dt == "text_delta" and delta.get("text"):
                                mark_first_token(request)
                                yield {"type": "content", "delta": delta["text"]}
                            elif dt == "thinking_delta" and delta.get("thinking"):
                                mark_first_token(request)
                                yield {"type": "thinking", "delta": delta["thinking"]}
                            elif dt == "input_json_delta":
                                slot = tool_blocks.get(int(chunk.get("index") or 0))
                                _partial = delta.get("partial_json")
                                if slot is not None and isinstance(_partial, str) and _partial:
                                    slot["arguments"] = _merge_stream_text(
                                        slot["arguments"], _partial)
                        elif event_type == "message_delta":
                            d = chunk.get("delta") or {}
                            if d.get("stop_reason"):
                                _sr = str(d["stop_reason"])
                                # Anthropic 用 tool_use 表示本轮有工具调用；
                                # 对齐 OpenAI 的 tool_calls 语义（agent_loop 与前端按此判断）
                                finish_reason = "tool_calls" if _sr == "tool_use" else _sr
                            u = chunk.get("usage") or {}
                            if u.get("output_tokens"):
                                completion_t = int(u["output_tokens"])
                            _details = u.get("output_tokens_details") or {}
                            if _details.get("thinking_tokens"):
                                reasoning_t = int(_details["thinking_tokens"])
                        elif event_type == "message_start":
                            msg = chunk.get("message") or {}
                            u = msg.get("usage") or {}
                            if u.get("input_tokens"):
                                prompt_t = int(u["input_tokens"])
                            cache_creation_t = int(u.get("cache_creation_input_tokens", 0))
                            cache_read_t = int(u.get("cache_read_input_tokens", 0))
                        elif event_type == "message_stop":
                            break
                tool_calls: list[dict] = []
                _seen_ids: set[str] = set()
                for idx in sorted(tool_blocks.keys()):
                    slot = tool_blocks[idx]
                    if not slot.get("name"):
                        continue
                    call_id = slot["id"] or _new_call_id()
                    if call_id in _seen_ids:
                        call_id = _new_call_id()
                    _seen_ids.add(call_id)
                    tool_calls.append({
                        "id": call_id,
                        "name": slot["name"],
                        # plan-20261016-ux（问题 3）：宽松解析，失败带 _parse_error
                        "arguments": parse_tool_args(slot["arguments"]),
                    })
                yield {
                    "type": "done",
                    "usage": Usage(
                        prompt_tokens=prompt_t,
                        completion_tokens=completion_t,
                        total_tokens=prompt_t + completion_t + cache_creation_t + cache_read_t,
                        cached_input_tokens=cache_read_t,
                        reasoning_tokens=reasoning_t,
                    ),
                    "finish_reason": finish_reason,
                    "tool_calls": tool_calls,
                }
            return
