"""Ta3Provider — Ta+3 牛码（银海）模型供应商实现。

严格复刻参考项目 ta3-new-coder 的 LLM 请求契约（风控核心），不走 OpenAI SDK
（SDK 自带 User-Agent: OpenAI/Python、x-stainless-* 头，指纹明显），改用
httpx 裸请求完全控制头与体：

- 请求头：X-Call-Source: APP / Bearer llm- + api-key（OpenAI）/ x-api-key +
  anthropic-version（Anthropic）/ Electron 同族 UA
- 请求体：temperature 默认 0.1、prune 空字段、thinking 按模型系别
  （qwen→enable_thinking / 其他→thinking-object / kimi→output_config.effort）、
  zai 加 stream_options + tool_stream
- 工具名伪装：出站 schema/历史 tool_calls → ta3 原生名，入站反伪装回真实执行名
- 流式解析：SSE data: 帧手动解析（OpenAI / Anthropic 双协议）

上下文管理、压缩、审批、编排全部沿用当前项目（本类只负责"请求怎么发"）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator

import httpx

from app.core.config import settings
from app.models.base import ModelProvider
from app.models.providers.ta3_tool_aliases import FROM_TA3, TO_TA3, disguise_args, restore_args
from app.models.providers.ta3_tool_schemas import disguise_tools
from app.models.schemas import ChatMessage, ChatRequest, ChatResponse, Usage

logger = logging.getLogger(__name__)

_DEFAULT_TEMPERATURE = 0.1  # 对齐参考项目 buildRequestBody 默认

# Electron 同族 UA（参考项目为 Electron 打包应用；可经 TA3_USER_AGENT 覆盖）
_DEFAULT_TA3_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "ta3-new-coder-desktop/1.0.0 Chrome/126.0.0.0 Electron/31.0.0 Safari/537.36"
)

_KIMI_IDENT = ("kimi",)
_QWEN_IDENT = ("qwen", "dashscope", "tongyi", "通义")
_ZAI_IDENT = ("zai",)

_OPENAI_THINKING_EFFORTS = ("low", "medium", "high", "xhigh", "max")

# v29 (plan-78): kimi 官方思考档位只有 low/high/max（output_config.effort）。
# 通用档位归一化：medium→low（kimi 无此档，保守降级）、xhigh→max、max→max、
# high→high、low→low；未知档位取 settings.ta3_kimi_thinking_effort（默认 low），
# 避免旧逻辑"非法值默认 max"导致思考时长失控、放大网关断流概率。
_KIMI_EFFORT_MAP = {
    "low": "low",
    "medium": "low",
    "high": "high",
    "xhigh": "max",
    "max": "max",
}


def _first_text(*values) -> str:
    for v in values:
        if v:
            return str(v).strip()
    return ""


def _identity(model_name: str, meta: dict) -> str:
    return " ".join(str(x or "") for x in (
        meta.get("provider"), model_name, meta.get("title"),
    )).lower()


class Ta3Provider(ModelProvider):
    name = "ta3"

    def __init__(self, *, api_key: str, base_url: str, model: str, meta: dict | None = None):
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model_name = model
        self._meta = meta or {}
        self._anthropic = bool(self._meta.get("anthropic"))
        self._completion_opts = self._meta.get("completionOptions") or {}
        self._request_headers = self._meta.get("requestHeaders") or {}
        self._ua = getattr(settings, "ta3_user_agent", "") or _DEFAULT_TA3_UA
        # v28: SSE 空闲超时改读配置——kimi-k3/grok-4.6 长思考时 30s 硬编码会误杀流
        self._stream_idle_timeout = float(getattr(settings, "ta3_stream_idle_timeout", 180) or 180)
        # 禁用 httpx 自动注入的 UA/编码头之外，保持与参考项目一致的压缩协商
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=10.0, write=10.0, read=self._stream_idle_timeout, pool=10.0,
            ),
            headers={"Accept-Encoding": "gzip, deflate"},
            follow_redirects=False,
        )

    # ─────────────────────────── 请求构造 ───────────────────────────

    def _base_headers(self, accept: str = "text/event-stream, application/json") -> dict:
        headers: dict = {
            "Content-Type": "application/json",
            "Accept": accept,
            "X-Call-Source": "APP",
            "User-Agent": self._ua,
        }
        if self._anthropic:
            headers["anthropic-version"] = "2023-06-01"
            if self._api_key:
                headers["x-api-key"] = self._api_key
        else:
            if self._api_key:
                headers["Authorization"] = f"Bearer {self._api_key}"
                headers["api-key"] = self._api_key
        for k, v in (self._request_headers or {}).items():
            if k and v is not None:
                headers[str(k)] = str(v)
        return headers

    def _thinking_intensity(self, request: ChatRequest) -> str:
        return (request.reasoning_effort or "none").strip() or "none"

    def _thinking_enabled(self, request: ChatRequest) -> bool:
        """ta3 目录 thinkingEnabled 且请求显式开启 thinking 时启用。"""
        return bool(request.thinking) and self._completion_opts.get("thinkingEnabled") is True

    def supports_thinking(self) -> bool:
        """是否支持 thinking 参数（kimi 系或目录 thinkingEnabled）。

        v29 (plan-78): 此前 ta3 未实现该方法，agent_loop 的 hasattr 判断为假，
        kimi-k3 请求永远不带 output_config.effort，落到默认（最高档）思考，
        思考时长失控、加剧网关断流导致的空响应。kimi 系默认支持思考档位。
        """
        identity = _identity(self._model_name, self._meta)
        return bool(self._completion_opts.get("thinkingEnabled") is True) or any(
            kw in identity for kw in _KIMI_IDENT
        )

    def _kimi_effort(self, request: ChatRequest) -> str:
        """kimi 官方思考档位归一化（low/high/max），未知档位取保守默认。"""
        effort = self._thinking_intensity(request).lower()
        if effort in _KIMI_EFFORT_MAP:
            return _KIMI_EFFORT_MAP[effort]
        return str(getattr(settings, "ta3_kimi_thinking_effort", "low") or "low")

    def _apply_thinking_openai(self, body: dict, request: ChatRequest) -> None:
        if not self._thinking_enabled(request):
            return
        identity = _identity(self._model_name, self._meta)
        effort = self._thinking_intensity(request)
        if any(kw in identity for kw in _QWEN_IDENT):
            # qwen 系：enable_thinking 布尔 + reasoning_effort
            body["enable_thinking"] = effort != "none"
            if effort != "none":
                body["reasoning_effort"] = effort
        else:
            body["thinking"] = {"type": "enabled" if effort != "none" else "disabled"}
            if effort != "none":
                body["reasoning_effort"] = effort

    # ─────────────────────────── 消息转换 ───────────────────────────

    def _disguise_message(self, m: ChatMessage) -> dict:
        """出站：ChatMessage → OpenAI dict，工具名/参数伪装 + reasoning 策略。

        对齐参考项目 applyPlainTurnReasoningPolicy：工具调用轮次回传
        reasoning_content，普通回复轮次剥离。
        """
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
                    alias = TO_TA3.get(name)
                    if alias is None:
                        # 未映射的历史调用（如 collect_results）→ 转普通文本，避免协议断裂
                        out.pop("tool_calls", None)
                        out["content"] = (m.content or "") + (
                            f"\n\n（历史工具调用 {name} 在当前环境不可用，结果已略）"
                        )
                        return out
                    tc_list.append({
                        "id": str(tc.get("id") or f"call_{len(tc_list):02d}"),
                        "type": "function",
                        "function": {
                            "name": alias,
                            "arguments": json.dumps(disguise_args(name, args), ensure_ascii=False),
                        },
                    })
                out["tool_calls"] = tc_list
                out["content"] = m.content or ""  # 纯 tool_call 回合 content 发空串（部分网关拒绝 null）
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

    def _restore_tool_calls(self, tool_calls: list[dict]) -> list[dict]:
        """入站：模型返回的 ta3 工具调用 → 真实执行名+参数。未知名保持原样（由执行层报错）。"""
        out = []
        for tc in tool_calls or []:
            name = str(tc.get("name") or "")
            args = tc.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args) if args else {}
                except (json.JSONDecodeError, TypeError):
                    args = {"_raw": args}
            if not isinstance(args, dict):
                args = {"_raw": str(args)}
            real = FROM_TA3.get(name)
            if real is not None:
                args = restore_args(name, args)
            out.append({
                "id": str(tc.get("id") or ""),
                "name": real or name,
                "arguments": args,
            })
        return out

    # ─────────────────────────── OpenAI 协议 ───────────────────────────

    def _build_openai_body(self, request: ChatRequest, disguised: list[dict]) -> dict:
        opts = self._completion_opts
        body: dict = {
            "model": request.model or self._model_name,
            "messages": [self._disguise_message(m) for m in request.messages],
            "stream": True,
            # 对齐参考项目：temperature 用目录 completionOptions，默认 0.1
            # （覆盖当前项目 0.3/0.7 —— ta3 网关/训练环境按 0.1 系指纹）
            "temperature": opts.get("temperature", _DEFAULT_TEMPERATURE),
        }
        max_tokens = opts.get("maxTokens") or opts.get("max_tokens")
        if max_tokens:
            body["max_tokens"] = max_tokens
        elif request.max_tokens:
            body["max_tokens"] = request.max_tokens
        if disguised:
            body["tools"] = disguised
            body["tool_choice"] = "auto"
        self._apply_thinking_openai(body, request)
        if any(kw in _identity(self._model_name, self._meta) for kw in _ZAI_IDENT):
            body["stream_options"] = {"include_usage": True}
            body["tool_stream"] = True
        # pruneRequestBody（对齐参考项目：undefined/null/'' 剔除）
        return {k: v for k, v in body.items() if v not in (None, "", {})}

    def _parse_openai_frame(self, line: str, monitor) -> bool:
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
        # usage（含 reasoning/cached 明细）
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
                slot = monitor["tool_calls"].setdefault(idx, {"id": "", "name": "", "arguments": ""})
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

    # ─────────────────────────── Anthropic 协议 ───────────────────────────

    def _convert_anthropic_messages(self, messages: list[ChatMessage]) -> tuple[str, list[dict]]:
        system_parts: list[str] = []
        converted: list[dict] = []
        # v29 (plan-78): kimi 网关要求 assistant 的 thinking 块强制每轮回传（参考项目
        # anthropicAdapter 的 Kimi 特判）；此前仅工具回合回传（has_tool_calls），
        # 纯文本回合的思考丢失，混合策略可能触发网关对历史消息的异常解析。
        _kimi = any(kw in _identity(self._model_name, self._meta) for kw in _KIMI_IDENT)
        for m in messages:
            if m.role in ("system", "developer"):
                system_parts.append(m.content or "")
                continue
            if m.role == "user":
                if m.content_blocks:
                    blocks = []
                    if m.content:
                        blocks.append({"type": "text", "text": m.content})
                    blocks.extend(m.content_blocks or [])
                    converted.append({"role": "user", "content": blocks})
                else:
                    converted.append({"role": "user", "content": m.content or ""})
            elif m.role == "tool":
                converted.append({
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": m.tool_call_id or "",
                                 "content": m.content or ""}],
                })
            elif m.role == "assistant":
                blocks: list[dict] = []
                has_tool_calls = bool(m.tool_calls)
                if m.content:
                    blocks.append({"type": "text", "text": m.content})
                if m.reasoning_content and (has_tool_calls or _kimi):
                    blocks.append({"type": "thinking", "thinking": m.reasoning_content})
                if has_tool_calls:
                    for tc in m.tool_calls or []:
                        name = str(tc.get("name") or "")
                        alias = TO_TA3.get(name)
                        if alias is None:
                            continue
                        args = tc.get("arguments") or {}
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except (json.JSONDecodeError, TypeError):
                                args = {}
                        blocks.append({
                            "type": "tool_use",
                            "id": str(tc.get("id") or ""),
                            "name": alias,
                            "input": disguise_args(name, args if isinstance(args, dict) else {}),
                        })
                if blocks:
                    converted.append({"role": "assistant", "content": blocks})
        return "\n\n".join(p for p in system_parts if p), converted

    def _convert_anthropic_tools(self, disguised: list[dict]) -> list[dict]:
        out = []
        for t in disguised:
            fn = t.get("function") or {}
            out.append({
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters") or {"type": "object"},
            })
        return out

    def _build_anthropic_body(self, request: ChatRequest, disguised: list[dict]) -> dict:
        opts = self._completion_opts
        system, messages = self._convert_anthropic_messages(request.messages)
        identity = _identity(self._model_name, self._meta)
        is_kimi = any(kw in identity for kw in _KIMI_IDENT)
        # v28: max_tokens 以目录 completionOptions.maxTokens 为上限——目录声明的是
        # 网关允许的最大值（kimi-k3=32768），全局 agent_max_output_tokens=131072
        # 直接下发会超上限导致网关截断/异常空响应（对齐 _build_openai_body 的语义）。
        _catalog_max = opts.get("maxTokens") or opts.get("max_tokens")
        if _catalog_max:
            max_tokens = int(min(request.max_tokens or _catalog_max, _catalog_max))
        else:
            max_tokens = request.max_tokens or 2048
        body: dict = {
            "model": request.model or self._model_name,
            "messages": messages,
            "stream": True,
            "temperature": 0.2 if is_kimi else opts.get("temperature", _DEFAULT_TEMPERATURE),
            "max_tokens": max_tokens,
        }
        if system:
            body["system"] = system
        if disguised:
            body["tools"] = self._convert_anthropic_tools(disguised)
        if self._thinking_enabled(request):
            effort = self._thinking_intensity(request)
            if effort == "none":
                if not is_kimi:
                    body["thinking"] = {"type": "disabled"}
            elif is_kimi:
                # kimi 官方：output_config.effort 控制思考档位，不发 thinking 块
                # v29 (plan-78): effort 归一化到 low/high/max，未知档位取保守默认
                body["output_config"] = {"effort": self._kimi_effort(request)}
            else:
                body["thinking"] = {
                    "type": "enabled",
                    "budget_tokens": max(2048, int(max_tokens * 0.8)),
                }
                body["output_config"] = {"effort": effort if effort in ("low", "medium", "high") else "low"}
        return {k: v for k, v in body.items() if v not in (None, "", {})}

    def _parse_anthropic_frame(self, line: str, monitor) -> bool:
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
        etype = payload.get("type")
        if etype == "message_start":
            # v28: Anthropic 协议的 input_tokens 在 message_start，补全 usage
            msg = payload.get("message") or {}
            usage = msg.get("usage") or {}
            if isinstance(usage, dict) and usage.get("input_tokens"):
                monitor["usage"].prompt_tokens = int(usage["input_tokens"])
        elif etype == "content_block_start":
            block = payload.get("content_block") or {}
            if block.get("type") == "tool_use":
                idx = payload.get("index", 0)
                monitor["anthropic_tools"][idx] = {
                    "id": block.get("id", ""),
                    "name": block.get("name", ""),
                    "arguments": "",
                }
        elif etype == "content_block_delta":
            delta = payload.get("delta") or {}
            dtype = delta.get("type")
            idx = payload.get("index", 0)
            # v29 (plan-78): thinking 增量字段变体兜底——Anthropic 规范为
            # thinking_delta.thinking，部分网关用 thinking/reasoning/reasoning_delta
            # 承载；此前只认 thinking_delta.thinking，思考内容丢失会误判空响应。
            _th = (delta.get("thinking") or delta.get("thinking_delta")
                   or delta.get("reasoning") or delta.get("reasoning_delta") or "")
            if dtype == "text_delta" and delta.get("text"):
                monitor["content_parts"].append(delta["text"])
            elif _th:
                monitor["thinking_parts"].append(_th)
            elif dtype == "input_json_delta" and delta.get("partial_json"):
                slot = monitor["anthropic_tools"].get(idx)
                if slot is not None:
                    slot["arguments"] += delta["partial_json"]
        elif etype == "message_delta":
            delta = payload.get("delta") or {}
            stop_reason = delta.get("stop_reason")
            if stop_reason:
                monitor["finish_reason"] = str(stop_reason)
            # v29 (plan-78): 部分网关把思考增量放在 message_delta 的 delta 里
            _th = delta.get("thinking") or delta.get("reasoning") or ""
            if _th:
                monitor["thinking_parts"].append(_th)
            # v28: output_tokens 在 message_delta 的 usage，与 message_start 的 input 合并
            usage = payload.get("usage") or {}
            if isinstance(usage, dict) and usage.get("output_tokens"):
                monitor["usage"].completion_tokens = int(usage["output_tokens"])
                monitor["usage"].total_tokens = (
                    monitor["usage"].prompt_tokens + monitor["usage"].completion_tokens
                )
        elif etype == "ping":
            # v28: Anthropic 协议心跳帧——仅用于诊断网关是否仍在 keep-alive
            monitor["last_heartbeat_at"] = time.time()
        elif etype == "message_stop":
            monitor["terminal"] = True
            return True
        return False

    def _finalize_anthropic_tools(self, monitor: dict) -> list[dict]:
        out = []
        for idx in sorted(monitor["anthropic_tools"].keys()):
            slot = monitor["anthropic_tools"][idx]
            try:
                args = json.loads(slot["arguments"]) if slot["arguments"] else {}
            except (json.JSONDecodeError, TypeError):
                args = {"_raw": slot["arguments"]}
            if not isinstance(args, dict):
                args = {"_raw": str(args)}
            name = slot["name"]
            real = FROM_TA3.get(name)
            if real is not None:
                args = restore_args(name, args)
            out.append({"id": slot["id"], "name": real or name, "arguments": args})
        return out

    # ─────────────────────────── 流式主流程 ───────────────────────────

    def _new_monitor(self) -> dict:
        return {
            "terminal": False,
            "finish_reason": "stop",
            "content_parts": [],
            "thinking_parts": [],
            "tool_calls": {},          # OpenAI 增量
            "anthropic_tools": {},     # Anthropic 增量
            "usage": Usage(),
            "last_heartbeat_at": None,  # v28: Anthropic ping 心跳时间（诊断用）
        }

    async def _stream_llm(self, request: ChatRequest,
                          parse_fn) -> AsyncIterator[dict]:
        """发起请求并按事件格式产出 thinking/content/done。"""
        disguised = disguise_tools(request.tools or [])
        if self._anthropic:
            url = f"{self._base_url}/v1/messages"
            body = self._build_anthropic_body(request, disguised)
        else:
            url = f"{self._base_url}/chat/completions"
            body = self._build_openai_body(request, disguised)
        headers = self._base_headers()
        logger.info("[ta3] model=%s protocol=%s tools=%d → %s",
                    request.model or self._model_name,
                    "anthropic" if self._anthropic else "openai",
                    len(disguised), url)

        monitor = self._new_monitor()
        sent_thinking = 0
        sent_content = 0
        _started_at = time.monotonic()
        # v29 (plan-78): 思考看门狗——思考阶段（已收到 thinking 帧、未产出 content/tool）
        # 连续空闲超过 ta3_thinking_watchdog（默认 240s，v28.1 由 120s 放宽以兼容
        # 更长思考链）主动终止，避免 kimi 长思考被网关静默断流后空转等待；
        # 终止后 finish_reason=thinking_timeout，agent_loop 走空响应重试降级。
        _watchdog = float(getattr(settings, "ta3_thinking_watchdog", 240) or 240)
        _thinking_seen = False
        _content_seen = False
        try:
            async with self._client.stream("POST", url, json=body, headers=headers) as resp:
                if resp.status_code != 200:
                    text = (await resp.aread()).decode("utf-8", errors="replace")[:400]
                    raise RuntimeError(f"模型请求失败 {resp.status_code}：{text}")
                _lines = resp.aiter_lines()
                while True:
                    # 思考阶段用更短的看门狗超时；其他阶段用 httpx read 空闲超时兜底
                    _stage_timeout = (
                        _watchdog if (_thinking_seen and not _content_seen)
                        else self._stream_idle_timeout
                    )
                    try:
                        line = await asyncio.wait_for(_lines.__anext__(), timeout=_stage_timeout)
                    except StopAsyncIteration:
                        break
                    except asyncio.TimeoutError:
                        _in_thinking = _thinking_seen and not _content_seen
                        monitor["finish_reason"] = "thinking_timeout" if _in_thinking else "timeout"
                        logger.warning(
                            "[ta3] model=%s %s超时: thinking=%d content=%d tools=%d, 耗时 %.1fs",
                            request.model or self._model_name,
                            "思考看门狗" if _in_thinking else "空闲",
                            len(monitor["thinking_parts"]), len(monitor["content_parts"]),
                            len(monitor["tool_calls"]) + len(monitor["anthropic_tools"]),
                            time.monotonic() - _started_at,
                        )
                        break
                    terminal = parse_fn(line, monitor)
                    # 实时产出（内容/思考逐段 yield）——parts 全量保留供 done 组装，
                    # 用游标消费避免 pop 丢失终态文本
                    while len(monitor["thinking_parts"]) > sent_thinking:
                        yield {"type": "thinking", "delta": monitor["thinking_parts"][sent_thinking]}
                        sent_thinking += 1
                    while len(monitor["content_parts"]) > sent_content:
                        yield {"type": "content", "delta": monitor["content_parts"][sent_content]}
                        sent_content += 1
                    if monitor["thinking_parts"]:
                        _thinking_seen = True
                    if monitor["content_parts"] or monitor["tool_calls"] or monitor["anthropic_tools"]:
                        _content_seen = True
                    if terminal:
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
        if self._anthropic:
            tool_calls = self._finalize_anthropic_tools(monitor)
        else:
            tool_calls = []
            for idx in sorted(monitor["tool_calls"].keys()):
                slot = monitor["tool_calls"][idx]
                try:
                    args = json.loads(slot["arguments"]) if slot["arguments"] else {}
                except (json.JSONDecodeError, TypeError):
                    args = {"_raw": slot["arguments"]}
                if not isinstance(args, dict):
                    args = {"_raw": str(args)}
                name = slot["name"]
                real = FROM_TA3.get(name)
                if real is not None:
                    args = restore_args(name, args)
                tool_calls.append({"id": slot["id"], "name": real or name, "arguments": args})

        # 退化文本兜底解析（复用 openai_compatible 的 DSML/文本意图解析）
        if not tool_calls and content:
            from app.models.providers.openai_compatible import (
                _parse_degraded_tool_calls, _parse_dsml_tool_calls,
            )
            degraded = _parse_dsml_tool_calls(content) if "DSML" in content else []
            if not degraded:
                degraded = _parse_degraded_tool_calls(content)
            if degraded:
                logger.info("[ta3] 退化文本解析出 %d 个工具调用", len(degraded))
                tool_calls = degraded
                content = None
                monitor["finish_reason"] = "tool_calls"

        # v28: 流式耗时/产出统计 + 空响应兜底日志（诊断"突然停止"现场）
        _usage_desc = (
            monitor["usage"].model_dump()
            if hasattr(monitor["usage"], "model_dump") else monitor["usage"]
        )
        logger.info(
            "[ta3] model=%s done finish=%s thinking=%d content=%d tools=%d usage=%s elapsed=%.1fs",
            request.model or self._model_name, monitor["finish_reason"],
            len(thinking or ""), len(content or ""), len(tool_calls),
            _usage_desc, time.monotonic() - _started_at,
        )
        if not content and not thinking and not tool_calls and monitor["finish_reason"] == "stop":
            logger.warning(
                "[ta3] model=%s 空响应流: 无 content/thinking/tool_calls, finish=stop, 耗时 %.1fs",
                request.model or self._model_name, time.monotonic() - _started_at,
            )

        yield {
            "type": "done",
            "content": content,
            "thinking": thinking,
            "tool_calls": tool_calls,
            "finish_reason": monitor["finish_reason"],
            "usage": monitor["usage"],
        }

    async def stream_structured(self, request: ChatRequest) -> AsyncIterator[dict]:
        parse_fn = self._parse_anthropic_frame if self._anthropic else self._parse_openai_frame
        async for event in self._stream_llm(request, parse_fn):
            yield event

    async def stream(self, request: ChatRequest) -> AsyncIterator[str]:
        """纯文本流（非结构化）。"""
        parse_fn = self._parse_anthropic_frame if self._anthropic else self._parse_openai_frame
        async for event in self._stream_llm(request, parse_fn):
            if event["type"] == "content":
                yield event["delta"]

    async def chat(self, request: ChatRequest) -> ChatResponse:
        """非流式收集（内部走流式，收集完整响应）。"""
        response = ChatResponse(content=None, tool_calls=[], finish_reason="stop", usage=Usage())
        parse_fn = self._parse_anthropic_frame if self._anthropic else self._parse_openai_frame
        async for event in self._stream_llm(request, parse_fn):
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
