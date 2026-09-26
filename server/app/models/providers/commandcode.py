"""CommandCode Provider — 直连 CommandCode 原生 API (https://api.commandcode.ai/alpha/generate)。

通过 CommandCode 特有的 Vercel AI SDK 风格 RPC 协议交互，避免第三方中转网关（如 9router）
转译 OpenAI SSE 时的丢帧/流过早截断问题。

支持：
- 结构化双向消息转换（OpenAI 角色 -> CommandCode user/assistant/tool 消息）
- Function calling (工具调用与结果回传)
- 原生思考流 (reasoning-delta -> thinking)
- 正文流 (text-delta -> content)
- 精确 Token 统计与缓存命中提取
"""
import asyncio
import json
import logging
import sys
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

import httpx

from app.core.config import settings
from app.models.base import ModelProvider
from app.models.schemas import ChatMessage, ChatRequest, ChatResponse, Usage

logger = logging.getLogger(__name__)

# CommandCode 官方默认端点与请求头
DEFAULT_BASE_URL = "https://api.commandcode.ai"
DEFAULT_VERSION = "0.25.7"

# v45: 输出上限兜底（请求未显式指定且 settings.agent_max_output_tokens=0 时使用）。
# 不带 max_tokens 时 CommandCode 网关套用自身较小的默认值，推理模型的 thinking 会先耗尽
# 该预算 —— 实测 finish_reason=length 且正文为空，用户看到"输出达到 token 上限，可能不完整"。
# 取 32768（主流模型单次输出上限的常见值），既不再被网关小默认值截断，也不会超模型上限被拒。
DEFAULT_MAX_OUTPUT_TOKENS = 32768

# plan-53-258 R2: CommandCode 只接受这五档思考深度（实测 400 提示
# "Invalid option: expected one of \"low\"|\"medium\"|\"high\"|\"xhigh\"|\"max\""）。
_REASONING_LEVELS = ("low", "medium", "high", "xhigh", "max")
# 内部档位别名：minimal 语义最接近 low；none/空 表示关闭思考，不下发该字段。
_REASONING_ALIASES = {"minimal": "low"}


def _normalize_reasoning_effort(effort: str | None) -> str | None:
    """把内部思考档位收敛到 CommandCode 接受的值域；返回 None 表示不下发。"""
    value = (effort or "").strip().lower()
    if not value or value == "none":
        return None
    value = _REASONING_ALIASES.get(value, value)
    return value if value in _REASONING_LEVELS else None

_DEFAULT_TIMEOUT = max(
    300.0,
    float(getattr(settings, "provider_stream_idle_timeout", 180) or 180),
)


class CommandCodeProvider(ModelProvider):
    """CommandCode 原生 Provider。"""

    name = "commandcode"

    def __init__(self, *, api_key: str, base_url: str = "", model: str = ""):
        self._api_key = api_key.strip()
        raw_base = (base_url or DEFAULT_BASE_URL).rstrip("/")
        if raw_base.endswith("/alpha/generate"):
            self._endpoint = raw_base
            self._base_url = raw_base[: -len("/alpha/generate")]
        else:
            self._base_url = raw_base
            self._endpoint = f"{raw_base}/alpha/generate"
        self._default_model = model

    def _headers(self, session_id: str | None = None) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
            "x-command-code-version": DEFAULT_VERSION,
            "x-cli-environment": "cli",
            "x-session-id": session_id or str(uuid.uuid4()),
            "Accept": "text/event-stream",
        }

    def _convert_messages(self, messages: list[ChatMessage]) -> tuple[str | None, list[dict]]:
        """将内部 ChatMessage 列表转换为 CommandCode 的 (system_prompt, messages)。"""
        system_parts: list[str] = []
        out_messages: list[dict] = []

        for m in messages:
            role = m.role

            if role in ("system", "developer"):
                if m.content:
                    system_parts.append(m.content)
                continue

            if role == "tool":
                # 工具执行结果消息
                content_val = m.content or ""
                out_messages.append({
                    "role": "tool",
                    "content": [
                        {
                            "type": "tool-result",
                            "toolCallId": m.tool_call_id or "",
                            "toolName": m.name or "",
                            "output": {"type": "text", "value": content_val},
                        }
                    ],
                })
                continue

            if role == "assistant":
                content_blocks: list[dict] = []
                if m.content:
                    content_blocks.append({"type": "text", "text": m.content})
                if m.tool_calls:
                    for tc in m.tool_calls:
                        args = tc.get("arguments") or {}
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except Exception:
                                args = {}
                        content_blocks.append({
                            "type": "tool-call",
                            "toolCallId": tc.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                            "toolName": tc.get("name") or "",
                            "input": args,
                        })
                out_messages.append({
                    "role": "assistant",
                    "content": content_blocks if content_blocks else [{"type": "text", "text": ""}],
                })
                continue

            # user 消息
            user_content: list[dict] = []
            if m.content:
                user_content.append({"type": "text", "text": m.content})
            if m.content_blocks:
                for block in m.content_blocks:
                    b_type = block.get("type")
                    if b_type == "text" and block.get("text"):
                        user_content.append({"type": "text", "text": block["text"]})
                    elif b_type == "image_url":
                        converted = self._convert_image_block(block)
                        if converted:
                            user_content.append(converted)
                    elif b_type == "image":
                        # 原生 image 块同样过一遍归一化：历史消息/系统注入的块可能仍是
                        # data URI + source.type=url 形态（上游会拒），统一收敛字段。
                        converted = self._convert_image_block(block)
                        if converted:
                            user_content.append(converted)
            if not user_content:
                user_content = [{"type": "text", "text": ""}]
            out_messages.append({
                "role": "user",
                "content": user_content,
            })

        system_prompt = "\n\n".join(system_parts) if system_parts else None
        return system_prompt, out_messages

    @staticmethod
    def _convert_image_block(block: dict) -> dict | None:
        """OpenAI image_url / 原生 image 块 → CommandCode 合法 image 块。

        plan-234-1171 R3: 此前这里把图片硬编码替换为占位文本 `[image]`，base64
        从未进入请求体，模型永远看不到图（本文件是唯一直接丢弃图片的 provider）。

        plan-53-258 R1（本次修复）：此前把 data URI 也塞进 `source.type="url"`，
        而上游的 url 分支只接受 http(s)——整轮请求直接失败：
            {'type': 'server_error', 'message': 'URL scheme must be http or https, got data:'}
        实测（api.commandcode.ai 真实请求，muse-spark / deepseek-v4.1-flash / glm-5.3-flash 交叉验证）：
        - data URI 必须走 `source.type="base64"`，字段名是 snake_case `media_type`
          （驼峰 mediaType 或省略该字段均返回 400 Validation error）；
        - `source.type="url"` 仅接受 http(s)，上游按地址自行下载（下载失败报 404）；
        - content 数组元素的 `type` 只接受 text|image|document|search_result|thinking。
        命中形态（模型正确读出图中颜色）：
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "..."}}
        """
        url = ""
        image_url = block.get("image_url")
        if isinstance(image_url, dict):
            url = str(image_url.get("url") or "")
        elif isinstance(image_url, str):
            url = image_url
        if not url:
            url = str(block.get("url") or "")
        # 已是原生 base64 形态（历史消息落库后回传）——字段齐全则归一化透传
        if not url:
            source = block.get("source")
            if isinstance(source, dict):
                if str(source.get("type") or "") == "base64":
                    data = str(source.get("data") or "")
                    media_type = str(source.get("media_type") or "")
                    if data and media_type:
                        return {
                            "type": "image",
                            "source": {"type": "base64", "media_type": media_type, "data": data},
                        }
                url = str(source.get("url") or "")
        if not url:
            return None
        # data URI → base64 分支（上游 url 分支只认 http(s)，传 data: 会被整轮拒绝）
        if url.startswith("data:"):
            header, _, data = url.partition(",")
            if not data:
                return None
            media_type = "image/png"
            if header.startswith("data:") and ";" in header:
                media_type = header[len("data:"):].split(";", 1)[0] or media_type
            return {
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": data},
            }
        # 远程 http(s) URL：上游按其地址下载
        return {"type": "image", "source": {"type": "url", "url": url}}

    def _convert_tools(self, tools: list[dict] | None) -> list[dict] | None:
        """转换工具定义为 CommandCode 要求的 input_schema 格式。"""
        if not tools:
            return None
        out = []
        for t in tools:
            fn = t.get("function") if t.get("type") == "function" else t
            if not fn:
                continue
            name = fn.get("name")
            if not name:
                continue
            desc = fn.get("description", "")
            schema = fn.get("parameters") or fn.get("input_schema") or {"type": "object", "properties": {}}
            out.append({
                "name": name,
                "description": desc,
                "input_schema": schema,
            })
        return out or None

    def _build_payload(self, request: ChatRequest) -> dict:
        system_prompt, messages = self._convert_messages(request.messages)
        tools = self._convert_tools(request.tools)
        thread_id = str(uuid.uuid4())

        params: dict[str, Any] = {
            "model": request.model or self._default_model,
            "messages": messages,
            "stream": True,
            "temperature": request.temperature if request.temperature is not None else 0.3,
        }
        # plan-53-258 R2: 下发用户配置的思考深度。
        # 根因：此前从不发 reasoning_effort，上游因此**完全不产出 reasoning 事件**
        # （实测同一 prompt：不带该字段事件流只有 text-*；带 high 时出现
        #   reasoning-start / reasoning-delta / reasoning-end，思考块随之为空）。
        _effort = _normalize_reasoning_effort(request.reasoning_effort)
        if _effort:
            params["reasoning_effort"] = _effort
        # v45: 显式下发充裕的输出上限。
        # 根因：不带 max_tokens 时网关套用自身较小的默认值（实测 finish_reason=length、
        # 正文为空）——推理模型的 thinking 会先耗尽该预算，用户看到"输出达到 token 上限，可能不完整"。
        # 优先用请求显式值；否则用配置 agent_max_output_tokens；都为空时用常量兜底。
        _max_out = request.max_tokens or settings.agent_max_output_tokens or DEFAULT_MAX_OUTPUT_TOKENS
        if _max_out and _max_out > 0:
            params["max_tokens"] = int(_max_out)
        if system_prompt:
            params["system"] = system_prompt
        if tools:
            params["tools"] = tools

        today = datetime.now().strftime("%Y-%m-%d")
        return {
            "threadId": thread_id,
            "memory": "",
            "config": {
                "workingDir": "workspace",
                "date": today,
                "environment": sys.platform,
                "structure": [],
                "isGitRepo": False,
                "currentBranch": "",
                "mainBranch": "",
                "gitStatus": "",
                "recentCommits": [],
            },
            "params": params,
        }

    async def chat(self, request: ChatRequest) -> ChatResponse:
        """非流式调用：基于 stream_structured 聚合完整结果。"""
        full_content = ""
        full_thinking = ""
        tool_calls: list[dict] = []
        finish_reason = "stop"
        usage = Usage()

        async for event in self.stream_structured(request):
            ev_type = event.get("type")
            if ev_type == "content":
                full_content += event.get("delta", "")
            elif ev_type == "thinking":
                full_thinking += event.get("delta", "")
            elif ev_type == "done":
                finish_reason = event.get("finish_reason", "stop")
                tool_calls = event.get("tool_calls", [])
                usage = event.get("usage", Usage())
                break

        return ChatResponse(
            content=full_content or None,
            thinking=full_thinking or None,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=usage,
            model=request.model or self._default_model,
        )

    async def stream(self, request: ChatRequest) -> AsyncIterator[str]:
        """纯文本流式输出。"""
        async for event in self.stream_structured(request):
            if event.get("type") == "content" and event.get("delta"):
                yield event["delta"]

    async def stream_structured(self, request: ChatRequest) -> AsyncIterator[dict[str, Any]]:
        """结构化流式输出：yield thinking / content / done 事件。"""
        payload = self._build_payload(request)
        headers = self._headers(request.session_id)
        timeout = httpx.Timeout(_DEFAULT_TIMEOUT, connect=30.0, read=_DEFAULT_TIMEOUT)

        full_content: list[str] = []
        full_thinking: list[str] = []
        tool_calls_map: dict[str, dict] = {}
        finish_reason = "stop"
        usage = Usage()

        async with httpx.AsyncClient(timeout=timeout, headers={"Accept-Encoding": "gzip, deflate"}) as client:
            try:
                async with client.stream("POST", self._endpoint, json=payload, headers=headers) as resp:
                    if resp.status_code != 200:
                        err_body = await resp.aread()
                        err_text = err_body.decode("utf-8", errors="replace")
                        logger.error("[commandcode] 请求失败 HTTP %s: %s", resp.status_code, err_text[:300])
                        raise RuntimeError(f"CommandCode upstream error (HTTP {resp.status_code}): {err_text[:200]}")

                    async for line in resp.aiter_lines():
                        s = line.strip()
                        if not s:
                            continue
                        if s.startswith("data:"):
                            s = s[5:].strip()
                        if not s or s == "[DONE]":
                            continue

                        try:
                            item = json.loads(s)
                        except Exception:
                            continue

                        if not isinstance(item, dict):
                            continue

                        ev_type = item.get("type")

                        if ev_type == "reasoning-delta":
                            delta = item.get("text") or ""
                            if delta:
                                full_thinking.append(delta)
                                yield {"type": "thinking", "delta": delta}

                        elif ev_type == "text-delta":
                            delta = item.get("text") or ""
                            if delta:
                                full_content.append(delta)
                                yield {"type": "content", "delta": delta}

                        elif ev_type == "tool-input-start":
                            t_id = item.get("id") or item.get("toolCallId") or f"call_{uuid.uuid4().hex[:8]}"
                            t_name = item.get("toolName") or ""
                            if t_id not in tool_calls_map:
                                tool_calls_map[t_id] = {"id": t_id, "name": t_name, "arguments": ""}

                        elif ev_type == "tool-input-delta":
                            t_id = item.get("id") or item.get("toolCallId")
                            if t_id and t_id in tool_calls_map:
                                tool_calls_map[t_id]["arguments"] += item.get("delta") or ""

                        elif ev_type == "tool-call":
                            t_id = item.get("toolCallId") or f"call_{uuid.uuid4().hex[:8]}"
                            t_name = item.get("toolName") or ""
                            raw_input = item.get("input") or {}
                            tool_calls_map[t_id] = {
                                "id": t_id,
                                "name": t_name,
                                "arguments": raw_input,
                            }

                        elif ev_type in ("finish-step", "finish"):
                            raw_fr = item.get("finishReason") or item.get("rawFinishReason") or "stop"
                            if raw_fr in ("tool-calls", "tool_calls"):
                                finish_reason = "tool_calls"
                            elif raw_fr in ("length", "max_tokens"):
                                finish_reason = "length"
                            else:
                                finish_reason = "stop"

                            u_dict = item.get("totalUsage") or item.get("usage")
                            if isinstance(u_dict, dict):
                                in_tok = u_dict.get("inputTokens") or 0
                                out_tok = u_dict.get("outputTokens") or 0
                                tot_tok = u_dict.get("totalTokens") or (in_tok + out_tok)
                                in_details = u_dict.get("inputTokenDetails") or {}
                                out_details = u_dict.get("outputTokenDetails") or {}
                                cached = in_details.get("cacheReadTokens") or u_dict.get("cachedInputTokens") or 0
                                reasoning = out_details.get("reasoningTokens") or u_dict.get("reasoningTokens") or 0
                                usage = Usage(
                                    prompt_tokens=in_tok,
                                    completion_tokens=out_tok,
                                    total_tokens=tot_tok,
                                    cached_input_tokens=cached,
                                    reasoning_tokens=reasoning,
                                )

                        elif ev_type == "error":
                            err_msg = item.get("error") or item.get("message") or "Unknown error"
                            logger.error("[commandcode] 流中接收到 error 事件: %s", err_msg)
                            raise RuntimeError(f"CommandCode error: {err_msg}")

            except httpx.TimeoutException as te:
                logger.error("[commandcode] 请求超时: %s", te)
                raise RuntimeError(f"CommandCode connection timeout ({_DEFAULT_TIMEOUT}s): {te}") from te
            except Exception as e:
                logger.error("[commandcode] 流式处理异常: %s", e)
                raise

        # 整理 tool_calls 格式
        final_tool_calls = []
        for tc in tool_calls_map.values():
            args = tc["arguments"]
            if isinstance(args, str):
                try:
                    args = json.loads(args) if args else {}
                except Exception:
                    args = {"_raw": args}
            final_tool_calls.append({
                "id": tc["id"],
                "name": tc["name"],
                "arguments": args,
            })

        yield {
            "type": "done",
            "content": "".join(full_content) or None,
            "thinking": "".join(full_thinking) or None,
            "tool_calls": final_tool_calls,
            "finish_reason": "tool_calls" if final_tool_calls else finish_reason,
            "usage": usage,
        }
