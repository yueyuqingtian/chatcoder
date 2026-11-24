"""AntigravityProvider — Google Antigravity（CloudCode 网关）模型供应商实现。

方案 docs/plan-antigravity-provider-integration.md §2、§5.4。

与 KiroProvider / Ta3Provider 同思路：不走 SDK，用 httpx 裸请求完全控制头与体。

- 端点：POST {gateway}/v1internal:streamGenerateContent?alt=sse
- 请求体：{project, model, requestId, userAgent, request:{contents, systemInstruction,
  tools, generationConfig}}（方案 §2.4）
- 响应：SSE，每帧 data: {"response":{candidates, usageMetadata, modelVersion}}
- 三点必须遵守（实测 400 复现）：
  1. 工具往返必须回放 thoughtSignature——签名挂在 functionCall part 上（§3 坑点 1）；
  2. generationConfig.systemInstruction 必须是 {"parts":[...]}，不能是字符串；
  3. role 只能是 user / model。
- 401/403：调用注入的 refresh 回调（async 无参 → 返回新 token）后重试一次。

上下文管理、压缩、审批、编排全部沿用当前项目（本类只负责「请求怎么发」）。
"""
from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.auth.antigravity.catalog import split_family
from app.auth.antigravity.token import build_headers, resolve_endpoints
from app.models.base import ModelProvider, mark_first_token
from app.models.providers.temperature_policy import request_temperature
from app.models.providers.tool_args import parse_tool_args
from app.models.providers.usage_normalization import openai_usage
from app.models.schemas import ChatMessage, ChatRequest, ChatResponse, Usage

logger = logging.getLogger(__name__)

_CONNECT_TIMEOUT_S = 15.0
_READ_TIMEOUT_S = 600.0

# 思考预算档位（thinkingConfig.thinkingBudget）。档位为 none/空时不下发该字段。
_THINKING_BUDGET = {
    "minimal": 1024,
    "low": 2048,
    "medium": 8192,
    "high": 16384,
    "xhigh": 24576,
    "max": 32768,
}

# 签名在 tool_call 字典里的键名（随 tool_calls 透传到下一轮，见方案 §4.3）
SIGNATURE_KEY = "thought_signature"


def _new_call_id() -> str:
    return "call_" + uuid.uuid4().hex[:12]


def _new_request_id() -> str:
    return "req_" + uuid.uuid4().hex


class AntigravityProvider(ModelProvider):
    """antigravity Provider（两条认证通道）。

    auth_kind 决定走哪条通道（方案 §5.3）：
    - oauth（默认）：CloudCode 网关，Gemini/Claude/GPT-OSS 全量模型可用；
    - api_key：Gemini 原生接口（generativelanguage 兼容），仅 Gemini 系。
    两条通道的消息/工具结构相同，差别在端点、鉴权头、请求体包装与 SSE 帧外层。
    """

    name = "antigravity"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        meta: dict | None = None,
        refresh_token=None,
        client_kwargs: dict | None = None,
        auth_kind: str | None = None,
    ):
        self._token = api_key
        self._base_url = (base_url or "").rstrip("/")
        self._model_name = model
        self._meta = meta or {}
        self._refresh_token = refresh_token
        self._client_kwargs = dict(client_kwargs or {})
        self._project = str(self._meta.get("project") or "")
        kind = (auth_kind or self._meta.get("auth_kind") or "oauth").strip().lower()
        self._auth_kind = "api_key" if kind == "api_key" else "oauth"

    @property
    def is_api_key_mode(self) -> bool:
        return self._auth_kind == "api_key"

    # ── 客户端与请求头 ────────────────────────────────
    def _headers(self) -> dict[str, str]:
        if self.is_api_key_mode:
            # Gemini 原生接口用 x-goog-api-key，不传 Authorization / Client-Metadata
            return {
                "x-goog-api-key": self._token,
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            }
        return build_headers(self._token, stream=True)

    def _endpoint(self) -> str:
        model = self._model_name
        if self.is_api_key_mode:
            from app.core.config import settings

            base = self._base_url or settings.antigravity_api_key_base_url
            return f"{base.rstrip('/')}/v1beta/models/{model}:streamGenerateContent?alt=sse"
        endpoints = resolve_endpoints(self._base_url)
        return f"{endpoints.gateway}/v1internal:streamGenerateContent?alt=sse"

    def supports_thinking(self) -> bool:
        """CloudCode 网关全部模型都接受 thinkingConfig（是否真的思考由模型决定）。"""
        return True

    # ── 消息转换 ──────────────────────────────────────
    def _convert_messages(self, messages: list[ChatMessage]) -> tuple[list[dict], dict | None]:
        """ChatMessage 列表 → (contents, systemInstruction)。

        - system / developer 合并进 systemInstruction（协议只允许 user/model 两种 role）；
        - assistant → role="model"，正文进 text part，tool_calls → functionCall part
          （挂回 thought_signature）；
        - tool（工具结果）→ role="user" 的 functionResponse part；
        - 连续同 role 合并，满足协议要求。
        """
        system_parts: list[str] = []
        contents: list[dict] = []

        def _push(role: str, parts: list[dict]) -> None:
            if not parts:
                return
            if contents and contents[-1]["role"] == role:
                contents[-1]["parts"].extend(parts)
            else:
                contents.append({"role": role, "parts": list(parts)})

        from app.orchestration.context_sanitizer import sanitize_context_messages
        for msg in sanitize_context_messages(messages):
            role = (msg.role or "").lower()
            if role in ("system", "developer"):
                text = self._text_of(msg)
                if text:
                    system_parts.append(text)
                continue
            if role == "user":
                parts = self._user_parts(msg)
                if parts:
                    _push("user", parts)
                continue
            if role == "assistant":
                parts: list[dict] = []
                text = self._text_of(msg)
                if text:
                    parts.append({"text": text})
                for tc in (msg.tool_calls or []):
                    fc = {
                        "name": str(tc.get("name") or ""),
                        "args": self._args_of(tc),
                    }
                    # 并行工具调用必须带 id，否则多调用无法与结果一一对应
                    if tc.get("id"):
                        fc["id"] = str(tc["id"])
                    part: dict[str, Any] = {"functionCall": fc}
                    sig = tc.get(SIGNATURE_KEY)
                    if sig:
                        # 关键：签名必须与 functionCall 同处一个 part，否则第二轮 400
                        part["thoughtSignature"] = str(sig)
                    if fc["name"]:
                        parts.append(part)
                # 只含思考的历史不进 contents（网关不接受空 model 轮）
                if not parts and msg.reasoning_content:
                    continue
                _push("model", parts)
                continue
            if role == "tool":
                payload = self._tool_response(msg)
                if payload:
                    _push("user", [payload])
                continue

        system_instruction = (
            {"parts": [{"text": "\n\n".join(system_parts)}]} if system_parts else None
        )
        return contents, system_instruction

    @staticmethod
    def _args_of(tc: dict) -> dict:
        """工具参数统一成 dict（历史里可能是 JSON 字符串）。"""
        args = tc.get("arguments")
        if isinstance(args, dict):
            return args
        if isinstance(args, str):
            try:
                parsed = json.loads(args) if args.strip() else {}
                return parsed if isinstance(parsed, dict) else {}
            except ValueError:
                return {}
        return {}

    @staticmethod
    def _text_of(msg: ChatMessage) -> str:
        """取消息正文（content 优先，content_blocks 里的 text 块追加）。"""
        parts: list[str] = []
        if msg.content:
            parts.append(str(msg.content))
        for block in (msg.content_blocks or []):
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                parts.append(str(block["text"]))
        return "\n".join(p for p in parts if p)

    def _user_parts(self, msg: ChatMessage) -> list[dict]:
        """用户消息 → Gemini parts（文本 + 内联图片）。"""
        parts: list[dict] = []
        text = self._text_of(msg)
        if text:
            parts.append({"text": text})
        for block in (msg.content_blocks or []):
            if not isinstance(block, dict) or block.get("type") != "image_url":
                continue
            url = ""
            image_url = block.get("image_url")
            if isinstance(image_url, dict):
                url = str(image_url.get("url") or "")
            elif isinstance(image_url, str):
                url = image_url
            inline = self._inline_data(url)
            if inline:
                parts.append({"inlineData": inline})
        return parts

    @staticmethod
    def _inline_data(data_url: str) -> dict | None:
        """data:image/png;base64,... → {"mimeType","data"}；非 data URL 忽略。"""
        if not data_url.startswith("data:"):
            return None
        try:
            header, _, payload = data_url.partition(",")
            mime = header[5:].split(";")[0] or "image/png"
            if not payload:
                return None
            return {"mimeType": mime, "data": payload}
        except Exception:  # noqa: BLE001 - 解析失败按无图处理
            return None

    def _tool_response(self, msg: ChatMessage) -> dict | None:
        """工具执行结果 → functionResponse part。

        网关要求 role=user 的 functionResponse；工具名从本消息 name 取
        （agent_loop 在 tool 消息上写了 name）；id 回传 tool_call_id，
        供并行调用与 functionCall 一一对应。
        """
        name = str(msg.name or "").strip()
        if not name:
            return None
        content = self._text_of(msg)
        # 尽量给结构化结果：JSON 能解析就原样当 response，否则塞 {"output": ...}
        try:
            parsed = json.loads(content) if content.strip() else {}
            response = parsed if isinstance(parsed, dict) else {"output": parsed}
        except ValueError:
            response = {"output": content}
        payload: dict[str, Any] = {"name": name, "response": response}
        if msg.tool_call_id:
            payload["id"] = str(msg.tool_call_id)
        return {"functionResponse": payload}

    def _convert_tools(self, tools: list[dict] | None) -> list[dict] | None:
        """OpenAI 风格工具 schema → Antigravity 的 functionDeclarations。

        入参形如 {"type":"function","function":{name,description,parameters}}，
        协议要求 {"functionDeclarations":[{name,description,parameters}]}。
        """
        if not tools:
            return None
        declarations: list[dict] = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
            name = str(fn.get("name") or "").strip()
            if not name:
                continue
            decl: dict[str, Any] = {"name": name}
            if fn.get("description"):
                decl["description"] = str(fn["description"])
            params = fn.get("parameters")
            if isinstance(params, dict) and params:
                decl["parameters"] = params
            declarations.append(decl)
        return [{"functionDeclarations": declarations}] if declarations else None

    # ── 请求体 ────────────────────────────────────────
    def _build_payload(self, request: ChatRequest) -> dict:
        contents, system_instruction = self._convert_messages(request.messages)
        inner: dict[str, Any] = {"contents": contents}
        if system_instruction:
            inner["systemInstruction"] = system_instruction
        from app.orchestration.context_sanitizer import sanitize_context_tools
        tools = self._convert_tools(sanitize_context_tools(request.tools))
        if tools:
            inner["tools"] = tools

        generation: dict[str, Any] = {}
        # 思考开启时不下发温度（对齐 temperature_policy 的统一口径）
        temperature = request_temperature(
            request, model_name=request.model or self._model_name,
            extra_names=(self._model_name,), base_url=self._base_url,
        )
        if temperature is not None:
            generation["temperature"] = temperature
        if request.max_tokens:
            generation["maxOutputTokens"] = int(request.max_tokens)
        thinking_cfg = self._thinking_config(request)
        if thinking_cfg:
            generation["thinkingConfig"] = thinking_cfg
        if generation:
            inner["generationConfig"] = generation

        if self.is_api_key_mode:
            # Gemini 原生接口不带 Antigravity 的外层包装（project/model/requestId）
            return inner

        sent_model = request.model or self._model_name
        return {
            "project": self._project or str(self._meta.get("project") or ""),
            "model": self._resolve_model_id(sent_model, request),
            "requestId": _new_request_id(),
            "userAgent": "antigravity",
            "requestType": "agent",
            "request": inner,
        }

    def _resolve_model_id(self, model: str, request: ChatRequest) -> str:
        """把「家族 id + 档位」解析成网关认识的真实变体 id。

        Antigravity 按 (家族, 档位) 拆成独立 id：gemini-3.8-flash + medium →
        gemini-3.8-flash-medium。用户可能直接用家族名（如从合并后的模型列表里选），
        这时必须补上档位后缀，否则网关返回 404。

        已有档位后缀的 id 原样发出；裸家族 id 且请求带了档位时才尝试补齐。
        """
        text = str(model or "")
        if not text:
            return text
        family, level = split_family(text)
        if level:
            # 已经是带档位的具体 id，直接用
            return text
        effort = str(getattr(request, "reasoning_effort", "") or "").strip().lower()
        if not effort:
            return text
        for word, mapped in (
            ("extra-high", "xhigh"), ("extra-low", "minimal"), ("xhigh", "xhigh"),
            ("minimal", "minimal"), ("low", "low"), ("medium", "medium"),
            ("high", "high"),
        ):
            if effort == word:
                return f"{family}-{mapped}"
        return text

    @staticmethod
    def _thinking_config(request: ChatRequest) -> dict | None:
        """thinking 三态：True 开启 / False 显式关闭 / None 不触碰。"""
        thinking = getattr(request, "thinking", None)
        if thinking is False:
            # 协议无「关闭」字段，用 0 预算表达（网关接受 thinkingBudget=0）
            return {"thinkingBudget": 0, "includeThoughts": False}
        effort = str(getattr(request, "reasoning_effort", "") or "").strip().lower()
        if thinking is True or (effort and effort != "none"):
            budget = _THINKING_BUDGET.get(effort) if effort else None
            cfg: dict[str, Any] = {"includeThoughts": True}
            if budget:
                cfg["thinkingBudget"] = budget
            return cfg
        return None

    # ── 流式 ──────────────────────────────────────────
    def _frame_body(self, frame) -> dict | None:
        """取出 SSE 帧里的有效负载。

        - oauth（CloudCode 网关）：帧是 {"response": {candidates...}, "traceId": ...}
        - api_key（Gemini 原生）：帧直接是 {candidates...}，没有外层包装
        两种都兼容，缺失时返回 None（心跳帧/未知帧一律跳过）。
        """
        if not isinstance(frame, dict):
            return None
        inner = frame.get("response")
        if isinstance(inner, dict):
            return inner
        if "candidates" in frame or "usageMetadata" in frame:
            return frame
        return None

    def _iter_sse_parts(self, resp_text: str) -> list[dict]:
        """把整段 SSE 文本拆成有效负载列表（非流式回退用）。"""
        out: list[dict] = []
        for line in resp_text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if not raw or raw == "[DONE]":
                continue
            try:
                frame = json.loads(raw)
            except ValueError:
                continue
            body = self._frame_body(frame)
            if body is not None:
                out.append(body)
        return out

    async def _open_stream(self, payload: dict):
        """发请求并返回 (client, response)；401/403 刷新令牌后重试一次。"""
        headers = self._headers()
        url = self._endpoint()
        for attempt in (1, 2):
            client = httpx.AsyncClient(
                timeout=httpx.Timeout(_READ_TIMEOUT_S, connect=_CONNECT_TIMEOUT_S),
                **self._client_kwargs,
            )
            try:
                req = client.build_request("POST", url, json=payload, headers=headers)
                resp = await client.send(req, stream=True)
            except httpx.HTTPError as e:
                await client.aclose()
                raise RuntimeError(f"antigravity gateway error: {e}") from e

            if resp.status_code in (401, 403) and attempt == 1 and self._refresh_token is not None:
                body = (await resp.aread()).decode("utf-8", errors="replace")
                await resp.aclose()
                await client.aclose()
                logger.warning("[antigravity] 令牌失效（HTTP %s），刷新后重试: %s",
                               resp.status_code, body[:200])
                new_token = await self._refresh_token()
                if new_token:
                    self._token = new_token
                    headers = self._headers()
                    continue
                raise RuntimeError(
                    f"antigravity upstream error (HTTP {resp.status_code}): {body[:200]}")

            if resp.status_code != 200:
                body = (await resp.aread()).decode("utf-8", errors="replace")
                await resp.aclose()
                await client.aclose()
                logger.error("[antigravity] 请求失败 HTTP %s: %s",
                             resp.status_code, body[:300])
                raise RuntimeError(
                    f"antigravity upstream error (HTTP {resp.status_code}): {body[:200]}")
            return client, resp
        raise RuntimeError("antigravity gateway error: 令牌刷新后仍未通过鉴权")

    async def stream_structured(self, request: ChatRequest) -> AsyncIterator[dict]:
        """结构化流式：yield thinking / content / done（方案 §5.4）。

        done 帧必须带 content / thinking / tool_calls / finish_reason / usage，
        agent_loop 按这些键取值（agent_loop.py:4139-4145）。
        """
        payload = self._build_payload(request)
        content_parts: list[str] = []
        thinking_parts: list[str] = []
        # key → {"id","name","arguments","thought_signature"}
        tool_map: dict[int, dict] = {}
        usage = Usage()
        finish_reason = "stop"

        client, resp = await self._open_stream(payload)
        try:
            async for raw_line in resp.aiter_lines():
                line = (raw_line or "").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    frame = json.loads(data)
                except ValueError:
                    continue
                inner = self._frame_body(frame)
                if not isinstance(inner, dict):
                    continue

                raw_usage = inner.get("usageMetadata")
                if isinstance(raw_usage, dict):
                    usage = openai_usage(raw_usage)

                candidates = inner.get("candidates")
                if not isinstance(candidates, list) or not candidates:
                    continue
                candidate = candidates[0] if isinstance(candidates[0], dict) else {}
                content = candidate.get("content")
                parts = content.get("parts") if isinstance(content, dict) else None

                for part in (parts or []):
                    if not isinstance(part, dict):
                        continue
                    # 1) 思考内容：thought=true 的 text part；空串不产出帧
                    if part.get("thought") and isinstance(part.get("text"), str):
                        delta = part["text"]
                        if delta:
                            mark_first_token(request)
                            thinking_parts.append(delta)
                            yield {"type": "thinking", "delta": delta}
                        continue
                    # 2) 正文
                    if isinstance(part.get("text"), str) and not part.get("thought"):
                        delta = part["text"]
                        if delta:
                            mark_first_token(request)
                            content_parts.append(delta)
                            yield {"type": "content", "delta": delta}
                        continue
                    # 3) 工具调用
                    fc = part.get("functionCall")
                    if isinstance(fc, dict):
                        idx = len(tool_map)
                        name = str(fc.get("name") or "")
                        mark_first_token(request)
                        entry = {
                            "id": str(fc.get("id") or "") or _new_call_id(),
                            "name": name,
                            "arguments": fc.get("args") if isinstance(fc.get("args"), dict) else {},
                        }
                        sig = part.get("thoughtSignature")
                        if sig:
                            entry[SIGNATURE_KEY] = str(sig)
                        tool_map[idx] = entry
                        continue
                    # 4) 单独成帧的签名：挂到最近的工具调用上（方案 §5.4 第 4 点）
                    sig = part.get("thoughtSignature")
                    if sig and tool_map:
                        last = tool_map[max(tool_map)]
                        last.setdefault(SIGNATURE_KEY, str(sig))

                fr = candidate.get("finishReason")
                if fr:
                    finish_reason = self._map_finish(str(fr), bool(tool_map))
        finally:
            try:
                await resp.aclose()
            finally:
                await client.aclose()

        tool_calls = []
        for idx in sorted(tool_map.keys()):
            entry = tool_map[idx]
            item = {
                "id": entry["id"],
                "name": entry["name"],
                "arguments": entry["arguments"] if isinstance(entry["arguments"], dict)
                else parse_tool_args(entry["arguments"]),
            }
            if entry.get(SIGNATURE_KEY):
                item[SIGNATURE_KEY] = entry[SIGNATURE_KEY]
            tool_calls.append(item)

        yield {
            "type": "done",
            "content": "".join(content_parts) if content_parts else None,
            "thinking": "".join(thinking_parts) if thinking_parts else None,
            "tool_calls": tool_calls,
            "finish_reason": finish_reason,
            "usage": usage,
        }

    @staticmethod
    def _map_finish(raw: str, has_tools: bool) -> str:
        """网关 finishReason → 项目内部口径（stop / tool_calls / length）。"""
        value = (raw or "").upper()
        if has_tools:
            return "tool_calls"
        if value in ("MAX_TOKENS", "LENGTH"):
            return "length"
        if value in ("SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT"):
            return "content_filter"
        return "stop"

    # ── 非流式（基类要求；实际运行只走 stream_structured） ──
    async def stream(self, request: ChatRequest) -> AsyncIterator[str]:
        async for event in self.stream_structured(request):
            if event.get("type") == "content" and event.get("delta"):
                yield str(event["delta"])

    async def chat(self, request: ChatRequest) -> ChatResponse:
        content_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_calls: list[dict] = []
        usage = Usage()
        finish_reason = "stop"
        frames = 0
        async for event in self.stream_structured(request):
            frames += 1
            kind = event.get("type")
            if kind == "content":
                content_parts.append(str(event.get("delta") or ""))
            elif kind == "thinking":
                thinking_parts.append(str(event.get("delta") or ""))
            elif kind == "done":
                content_parts = [str(event.get("content") or "")]
                thinking_parts = [str(event.get("thinking") or "")]
                tool_calls = list(event.get("tool_calls") or [])
                usage = event.get("usage") or Usage()
                finish_reason = str(event.get("finish_reason") or "stop")
        return ChatResponse(
            content="".join(content_parts) or None,
            thinking="".join(thinking_parts) or None,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=usage,
            model=self._model_name,
            frames_received=frames,
        )
