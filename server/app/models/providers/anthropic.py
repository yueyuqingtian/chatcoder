"""Anthropic Message API 兼容 Provider。

支持 Anthropic 官方 /v1/messages 接口格式，同时也兼容以 Anthropic 格式暴露的代理网关。
核心区别:
- 系统提示放在 top-level system 字段，而非 messages 数组。
- 多模态内容使用 {type: "image", source: {type: "base64", media_type, data}} 格式。
- 响应体格式: {content: [{type: "text", text: "..."}], stop_reason, usage}
"""
import json
from collections.abc import AsyncIterator

import httpx

from app.models.base import ModelProvider
from app.models.schemas import ChatRequest, ChatResponse, Usage

_DEFAULT_TIMEOUT = 120.0


class AnthropicProvider(ModelProvider):
    """Anthropic Message API Provider。"""

    name = "anthropic"

    def __init__(self, *, api_key: str, base_url: str, model: str):
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
            "max_tokens": request.max_tokens or 4096,
            "stream": stream,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        # v6.0: extended thinking（对齐 claude code），reasoning_effort 非 none 时启用
        if request.reasoning_effort and request.reasoning_effort != "none":
            budget = {"low": 2048, "medium": 8192, "high": 16384, "xhigh": 32768, "max": 32768}.get(
                request.reasoning_effort, 8192
            )
            # v23.1: 中转网关（如 9router）对 Anthropic 请求体做严格白名单校验，
            # thinking.budget_tokens 不在其中会直接 400 UNKNOWN_FIELD；
            # 官方 api.anthropic.com 的 extended thinking 原生要求该字段，仅官方网关携带。
            _official = "api.anthropic.com" in self._base_url.lower()
            payload["thinking"] = (
                {"type": "enabled", "budget_tokens": budget} if _official else {"type": "enabled"}
            )
            # Anthropic thinking 要求 temperature=1，且 max_tokens > budget_tokens
            payload["temperature"] = 1
            if _official and payload.get("max_tokens", 0) <= budget:
                payload["max_tokens"] = budget + 4096
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
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT, headers={"Accept-Encoding": "gzip, deflate"}) as client:
            resp = await client.post(self._endpoint, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

        content_blocks = data.get("content") or []
        text_parts: list[str] = []
        tool_calls: list[dict] = []
        for block in content_blocks:
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
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

        return ChatResponse(
            content="\n".join(text_parts),
            tool_calls=tool_calls,
            finish_reason=data.get("stop_reason") or "stop",
            usage=Usage(
                prompt_tokens=prompt_t,
                completion_tokens=completion_t,
                total_tokens=prompt_t + completion_t + cache_creation_t + cache_read_t,
                cached_input_tokens=cache_read_t,
                reasoning_tokens=0,
            ),
            model=request.model or self._default_model,
        )

    async def stream(self, request: ChatRequest) -> AsyncIterator[str]:
        payload = self._build_payload(request, stream=True)
        headers = self._headers()
        timeout = httpx.Timeout(_DEFAULT_TIMEOUT, read=None)
        async with httpx.AsyncClient(timeout=timeout, headers={"Accept-Encoding": "gzip, deflate"}) as client:
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
        async with httpx.AsyncClient(timeout=timeout, headers={"Accept-Encoding": "gzip, deflate"}) as client:
            async with client.stream("POST", self._endpoint, json=payload, headers=headers) as resp:
                resp.raise_for_status()
                event_type = ""
                prompt_t = 0
                completion_t = 0
                cache_creation_t = 0
                cache_read_t = 0
                tool_calls: list[dict] = []
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
                        if event_type == "content_block_delta":
                            delta = chunk.get("delta") or {}
                            dt = delta.get("type")
                            if dt == "text_delta" and delta.get("text"):
                                yield {"type": "content", "delta": delta["text"]}
                            elif dt == "thinking_delta" and delta.get("thinking"):
                                yield {"type": "thinking", "delta": delta["thinking"]}
                        elif event_type == "message_delta":
                            d = chunk.get("delta") or {}
                            if d.get("stop_reason"):
                                finish_reason = d["stop_reason"]
                            u = chunk.get("usage") or {}
                            if u.get("output_tokens"):
                                completion_t = int(u["output_tokens"])
                        elif event_type == "message_start":
                            msg = chunk.get("message") or {}
                            u = msg.get("usage") or {}
                            if u.get("input_tokens"):
                                prompt_t = int(u["input_tokens"])
                            cache_creation_t = int(u.get("cache_creation_input_tokens", 0))
                            cache_read_t = int(u.get("cache_read_input_tokens", 0))
                        elif event_type == "message_stop":
                            break
                yield {
                    "type": "done",
                    "usage": Usage(
                        prompt_tokens=prompt_t,
                        completion_tokens=completion_t,
                        total_tokens=prompt_t + completion_t + cache_creation_t + cache_read_t,
                        cached_input_tokens=cache_read_t,
                        reasoning_tokens=0,
                    ),
                    "finish_reason": finish_reason,
                    "tool_calls": tool_calls,
                }
