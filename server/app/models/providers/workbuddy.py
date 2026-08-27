"""WorkBuddyProvider — WorkBuddy（腾讯 CodeBuddy）模型供应商实现。

复刻 CodeBuddy CLI 的网关请求契约（copilot.tencent.com/v2，标准 OpenAI 协议）：
- 端点：POST {base_url}/chat/completions（base_url = https://copilot.tencent.com/v2）
- 请求头：Authorization: Bearer accessToken + X-User-Id + X-Domain + 会话追踪头
  （X-Request-ID / X-Conversation-ID / X-Conversation-Request-ID /
   X-Conversation-Message-ID / X-Agent-Intent / X-IDE-* / X-Product）
- 请求体：OpenAI 标准字段 + thinking 翻译（thinkingLevelMap / thinkingFormat，
  见 workbuddy_model_caps）；工具 schema 直接透传（网关标准 function calling，
  无需 ta3 式工具名伪装）
- 流式解析：SSE data: 帧手动解析（OpenAI 协议，含 reasoning_content）

401 时通过 refresh_token 回调自动刷新一次后重试（由 registry 注入）。
上下文管理、压缩、审批、编排全部沿用当前项目（本类只负责"请求怎么发"）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from urllib.parse import quote, urlparse

import httpx

from app.core.config import settings
from app.models.base import ModelProvider
from app.models.providers.workbuddy_model_caps import get_model_caps, map_reasoning_effort
from app.models.schemas import ChatMessage, ChatRequest, ChatResponse, Usage

logger = logging.getLogger(__name__)

_FIRST_CHUNK_TIMEOUT_S = 30.0
_STREAM_IDLE_TIMEOUT_S = 30.0
_DEFAULT_TEMPERATURE = 1.0  # 对齐 CLI 模型元数据 temperature: 1

# Electron 同族 UA（参考应用为 Electron 打包应用；可经 WORKBUDDY_USER_AGENT 覆盖）
_DEFAULT_WB_UA = (
    "WorkBuddy/5.3.14 WorkBuddy/5.3.14 CLI/2.115.0"
)

DEFAULT_WORKBUDDY_API_BASE = "https://copilot.tencent.com"


class WorkBuddyProvider(ModelProvider):
    name = "workbuddy"

    def __init__(self, *, api_key: str, base_url: str, model: str, meta: dict | None = None,
                 refresh_token=None):
        self._access_token = api_key
        self._base_url = base_url.rstrip("/")
        self._model_name = model
        self._meta = meta or {}
        # 401 时调用（async 无参）→ 返回新 access_token；由 registry 注入（依赖 DB 会话）
        self._refresh_token = refresh_token
        self._ua = getattr(settings, "workbuddy_user_agent", "") or _DEFAULT_WB_UA
        # 禁用 httpx 自动注入的 UA/编码头之外，保持与 CLI 一致的压缩协商
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, write=10.0, read=_STREAM_IDLE_TIMEOUT_S, pool=10.0),
            headers={"Accept-Encoding": "gzip, deflate"},
            follow_redirects=False,
        )

    # ─────────────────────────── 请求头 ───────────────────────────

    def _base_headers(self) -> dict:
        domain = urlparse(self._base_url).netloc
        request_id = uuid.uuid4().hex
        headers: dict = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream, application/json",
            "Authorization": f"Bearer {self._access_token}",
            "X-Domain": domain,
            "X-Product": "SaaS",
            "X-Request-ID": request_id,
            "X-Conversation-ID": uuid.uuid4().hex,
            "X-Conversation-Request-ID": uuid.uuid4().hex,
            "X-Conversation-Message-ID": request_id,
            "X-Agent-Intent": "craft",
            "X-IDE-Type": "CLI",
            "X-IDE-Name": "workbuddy-desktop",
            "X-IDE-Version": "1.0.0",
            "X-Private-Data": "false",
            "User-Agent": self._ua,
        }
        uid = self._meta.get("account_uid") or ""
        if uid:
            headers["X-User-Id"] = quote(str(uid), safe="")
        enterprise_id = self._meta.get("enterprise_id") or ""
        if enterprise_id:
            headers["X-Enterprise-Id"] = str(enterprise_id).strip()
            headers["X-Tenant-Id"] = str(enterprise_id).strip()
        return headers

    # ─────────────────────────── 请求体 ───────────────────────────

    def _apply_thinking(self, body: dict, request: ChatRequest) -> None:
        caps = get_model_caps(self._model_name)
        effort = (request.reasoning_effort or "").strip() or None
        thinking_on = bool(request.thinking) or effort not in (None, "none")
        thinking_format = caps.get("thinking_format")

        if thinking_format == "deepseek":
            # deepseek 系：thinking.type 布尔 + effort 档位映射（null 档位删除）
            body["thinking"] = {"type": "enabled" if thinking_on else "disabled"}
            if thinking_on and effort:
                mapped = map_reasoning_effort(effort, caps)
                if mapped:
                    body["reasoning_effort"] = mapped
            else:
                body.pop("reasoning_effort", None)
        elif thinking_format == "zai":
            # GLM（zai）系：enable_thinking 布尔，不传 reasoning_effort
            if thinking_on:
                body["enable_thinking"] = True
            body.pop("reasoning_effort", None)
        elif caps.get("supports_reasoning_effort") is False:
            # kimi 系：不传 reasoning_effort（模型不支持），thinking 透传由网关决定
            if effort and effort != "none":
                body["thinking"] = {"type": "enabled"}
            body.pop("reasoning_effort", None)
        else:
            # 未知模型：标准 reasoning_effort 透传（xhigh 降级 high）
            if effort and effort != "none":
                body["reasoning_effort"] = map_reasoning_effort(effort, caps)

        if thinking_on and (body.get("reasoning_effort") or body.get("enable_thinking")
                            or body.get("thinking")):
            body["reasoning_summary"] = "auto"

    def _build_body(self, request: ChatRequest) -> dict:
        body: dict = {
            "model": request.model or self._model_name,
            "messages": [self._convert_message(m) for m in request.messages],
            "stream": True,
        }
        temperature = self._meta.get("temperature") or _DEFAULT_TEMPERATURE
        if request.temperature is not None:
            body["temperature"] = request.temperature
        else:
            body["temperature"] = temperature
        max_output = self._meta.get("maxOutputTokens")
        max_tokens = request.max_tokens
        if max_tokens:
            body["max_tokens"] = min(max_tokens, max_output) if max_output else max_tokens
        elif max_output:
            body["max_tokens"] = max_output
        if request.tools:
            body["tools"] = request.tools
            body["tool_choice"] = "auto"
        self._apply_thinking(body, request)
        # pruneRequestBody（对齐 CLI：undefined/null/'' 剔除）
        return {k: v for k, v in body.items() if v not in (None, "", {})}

    def _convert_message(self, m: ChatMessage) -> dict:
        """ChatMessage → OpenAI dict（无伪装，网关标准 function calling）。"""
        out: dict = {"role": m.role, "content": m.content or ""}
        if m.role in ("system", "developer"):
            out["role"] = "system"
        if m.role == "assistant":
            has_tool_calls = bool(m.tool_calls)
            if m.reasoning_content and has_tool_calls:
                out["reasoning_content"] = m.reasoning_content
            if has_tool_calls:
                tc_list = []
                for tc in m.tool_calls or []:
                    name = str(tc.get("name") or "")
                    args = tc.get("arguments") or {}
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except (json.JSONDecodeError, TypeError):
                            args = {}
                    tc_list.append({
                        "id": str(tc.get("id") or f"call_{len(tc_list):02d}"),
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": json.dumps(args if isinstance(args, dict) else {},
                                                   ensure_ascii=False),
                        },
                    })
                out["tool_calls"] = tc_list
                # 纯 tool_call 回合 content 发空串（部分网关拒绝 null）
                out["content"] = m.content or ""
        elif m.role == "tool":
            out["role"] = "tool"
            out["tool_call_id"] = m.tool_call_id or ""
        elif m.content_blocks:
            parts = []
            if m.content:
                parts.append({"type": "text", "text": m.content})
            parts.extend(m.content_blocks or [])
            out["content"] = parts
        return out

    # ─────────────────────────── SSE 解析 ───────────────────────────

    def _parse_frame(self, line: str, monitor) -> bool:
        """解析单条 SSE data 帧；返回是否终止。"""
        if not line.startswith("data:"):
            return False
        data = line[5:].strip()
        if not data:
            return False
        if data == "[DONE]":
            monitor["terminal"] = True
            return True
        try:
            payload = json.loads(data)
        except ValueError:
            return False
        usage = payload.get("usage")
        if isinstance(usage, dict):
            details = usage.get("completion_tokens_details") or {}
            prompt_details = usage.get("prompt_tokens_details") or {}
            monitor["usage"] = Usage(
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                total_tokens=int(usage.get("total_tokens") or 0),
                cached_input_tokens=int(prompt_details.get("cached_tokens") or 0),
                reasoning_tokens=int(details.get("reasoning_tokens") or 0),
            )
        choices = payload.get("choices") or []
        if not choices:
            return False
        choice = choices[0]
        delta = choice.get("delta") or {}
        reasoning = (delta.get("reasoning_content")
                     or delta.get("reasoning")
                     or delta.get("thinking") or "")
        if reasoning:
            monitor["thinking_parts"].append(reasoning)
        text = delta.get("content")
        if text:
            monitor["content_parts"].append(text)
        if delta.get("tool_calls"):
            for tc in delta["tool_calls"]:
                idx = int(tc.get("index") or 0)
                slot = monitor["tool_calls"].setdefault(
                    idx, {"id": "", "name": "", "arguments": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] += fn["name"]
                if fn.get("arguments"):
                    slot["arguments"] += fn["arguments"]
        finish_reason = choice.get("finish_reason") or choice.get("finishReason")
        if finish_reason:
            monitor["finish_reason"] = str(finish_reason)
            monitor["terminal"] = True
            # 不在此 break：网关常在 finish_reason 后补发 usage 帧与 [DONE]，
            # 提前终止会丢失 usage（由 [DONE] 或流结束兜底）
        return False

    # ─────────────────────────── 流式主流程 ───────────────────────────

    def _new_monitor(self) -> dict:
        return {
            "terminal": False,
            "finish_reason": "stop",
            "content_parts": [],
            "thinking_parts": [],
            "tool_calls": {},
            "usage": Usage(),
        }

    async def _try_refresh_token(self) -> bool:
        """401 时通过回调刷新 token；成功返回 True（调用方需重建请求头重试）。"""
        if self._refresh_token is None:
            return False
        try:
            new_token = await self._refresh_token()
        except Exception as e:  # noqa: BLE001
            logger.warning("[workbuddy] model=%s token 刷新失败: %s", self._model_name, e)
            return False
        if new_token:
            self._access_token = new_token
            logger.info("[workbuddy] model=%s 401 后 token 已刷新并重试", self._model_name)
            return True
        return False

    async def _stream_llm(self, request: ChatRequest) -> AsyncIterator[dict]:
        url = f"{self._base_url}/chat/completions"
        body = self._build_body(request)
        monitor = self._new_monitor()
        logger.info("[workbuddy] model=%s tools=%d → %s", request.model or self._model_name,
                    len(request.tools or []), url)

        attempt = 0
        while True:
            headers = self._base_headers()
            try:
                async with self._client.stream("POST", url, json=body, headers=headers) as resp:
                    if resp.status_code == 401 and attempt < 1 and await self._try_refresh_token():
                        attempt += 1
                        continue
                    if resp.status_code != 200:
                        text = (await resp.aread()).decode("utf-8", errors="replace")[:400]
                        raise RuntimeError(f"模型请求失败 {resp.status_code}：{text}")
                    sent_thinking = 0
                    sent_content = 0
                    async for line in resp.aiter_lines():
                        terminal = self._parse_frame(line, monitor)
                        while len(monitor["thinking_parts"]) > sent_thinking:
                            part = monitor["thinking_parts"][sent_thinking]
                            yield {"type": "thinking", "delta": part}
                            sent_thinking += 1
                        while len(monitor["content_parts"]) > sent_content:
                            part = monitor["content_parts"][sent_content]
                            yield {"type": "content", "delta": part}
                            sent_content += 1
                        if terminal:
                            break
                    break
            except asyncio.CancelledError:
                raise
            except httpx.TimeoutException as e:
                raise RuntimeError(f"模型请求超时：{e.__class__.__name__}") from e
            except httpx.HTTPError as e:
                raise RuntimeError(f"模型请求失败：{e.__class__.__name__}: {e}") from e

        # 组装结果
        content = "".join(monitor["content_parts"]) or None
        thinking = "".join(monitor["thinking_parts"]) or None
        tool_calls = []
        for idx in sorted(monitor["tool_calls"].keys()):
            slot = monitor["tool_calls"][idx]
            try:
                args = json.loads(slot["arguments"]) if slot["arguments"] else {}
            except (json.JSONDecodeError, TypeError):
                args = {"_raw": slot["arguments"]}
            if not isinstance(args, dict):
                args = {"_raw": str(args)}
            tool_calls.append({
                "id": slot["id"] or f"call_{idx:02d}",
                "name": slot["name"],
                "arguments": args,
            })

        # 退化文本兜底解析（复用 openai_compatible 的 DSML/文本意图解析）
        if not tool_calls and content:
            from app.models.providers.openai_compatible import (
                _parse_degraded_tool_calls,
                _parse_dsml_tool_calls,
            )
            degraded = _parse_dsml_tool_calls(content) if "DSML" in content else []
            if not degraded:
                degraded = _parse_degraded_tool_calls(content)
            if degraded:
                logger.info("[workbuddy] 退化文本解析出 %d 个工具调用", len(degraded))
                tool_calls = degraded
                content = None
                monitor["finish_reason"] = "tool_calls"

        yield {
            "type": "done",
            "content": content,
            "thinking": thinking,
            "tool_calls": tool_calls,
            "finish_reason": monitor["finish_reason"],
            "usage": monitor["usage"],
        }

    async def stream_structured(self, request: ChatRequest) -> AsyncIterator[dict]:
        async for event in self._stream_llm(request):
            yield event

    async def stream(self, request: ChatRequest) -> AsyncIterator[str]:
        async for event in self._stream_llm(request):
            if event["type"] == "content":
                yield event["delta"]

    async def chat(self, request: ChatRequest) -> ChatResponse:
        response = ChatResponse(content=None, tool_calls=[], finish_reason="stop", usage=Usage())
        async for event in self._stream_llm(request):
            if event["type"] == "done":
                response = ChatResponse(
                    content=event["content"],
                    thinking=event["thinking"],
                    tool_calls=event["tool_calls"],
                    finish_reason=event["finish_reason"],
                    usage=event["usage"],
                    model=request.model or self._model_name,
                )
        return response
