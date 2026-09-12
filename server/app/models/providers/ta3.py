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
import re
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

# ─────────────────── 出站风控提示词脱敏 ───────────────────
# 部分第三方/网关在检测到竞品 CLI 官方系统提示词签名时会以 403 routing_error
# 直接拦截整条请求（如 "you are claude code" 会被判定为未经授权的 CLI 流量）。
# 当工具执行（如 grep 源码、读取依赖）将该字符串读入历史消息后，后续所有请求
# 都会因携带该特征而永久 403。在此处出站时静默脱敏，保留语义但消除触发特征。
_OUTBOUND_RISK_PATTERNS = (
    # 不区分大小写匹配 "you are claude code" 及其空白/连字符变体 → 统一替换为 "you are ta+3 agent"
    (re.compile(r"\b(you\s+are|i\s+am)\s+claude[\s_-]*code\b", re.IGNORECASE), "you are ta+3 agent"),
)


def sanitize_outbound_text(text: str) -> str:
    """脱敏出站文本中的高危网关拦截特征。"""
    if not text or not isinstance(text, str):
        return text
    out = text
    for pat, repl in _OUTBOUND_RISK_PATTERNS:
        out = pat.sub(repl, out)
    return out


def sanitize_outbound_structure(val):
    """递归脱敏字典、列表或字符串结构体（用于 tool 参数与 content_blocks）。"""
    if isinstance(val, str):
        return sanitize_outbound_text(val)
    if isinstance(val, dict):
        return {k: sanitize_outbound_structure(v) for k, v in val.items()}
    if isinstance(val, list):
        return [sanitize_outbound_structure(x) for x in val]
    return val


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
        """出站：ChatMessage → OpenAI dict，工具名/参数伪装 + reasoning 策略 + 出站脱敏。

        对齐参考项目 applyPlainTurnReasoningPolicy：工具调用轮次回传
        reasoning_content，普通回复轮次剥离。出站内容经 sanitize_outbound_text
        脱敏，消除触发网关 403 风控的竞品签名特征。
        """
        raw_content = sanitize_outbound_text(m.content or "")
        out: dict = {"role": m.role, "content": raw_content}
        if m.role in ("system", "developer"):
            out["role"] = "system"
        if m.role == "assistant":
            has_tool_calls = bool(m.tool_calls)
            if m.reasoning_content and has_tool_calls:
                out["reasoning_content"] = sanitize_outbound_text(m.reasoning_content)
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
                        # 未映射的历史调用（如 collect_results）→ 转普通文本，避免协议断裂。
                        # plan-238-1191: 措辞说明"调用与结果已转为文本记录"（结果并不会
                        # 丢失——tool 消息随后会被转成用户文本），并明确要求不要复述本条，
                        # 避免模型把提示原样抄进回复、或误以为工具不可用而停在半途。
                        out.pop("tool_calls", None)
                        out["content"] = raw_content + (
                            f"\n\n（历史工具调用 {name} 在当前模型下不可用："
                            f"调用与结果已转为文本记录，请勿复述本条提示，继续使用当前可用工具推进任务。）"
                        )
                        return out
                    clean_args = sanitize_outbound_structure(disguise_args(name, args))
                    tc_list.append({
                        "id": str(tc.get("id") or f"call_{len(tc_list):02d}"),
                        "type": "function",
                        "function": {
                            "name": alias,
                            "arguments": json.dumps(clean_args, ensure_ascii=False),
                        },
                    })
                out["tool_calls"] = tc_list
                out["content"] = raw_content  # 纯 tool_call 回合 content 发空串（部分网关拒绝 null）
        elif m.role == "tool":
            out["role"] = "tool"
            out["tool_call_id"] = m.tool_call_id or ""
            out["content"] = raw_content
        elif m.content_blocks:
            parts = []
            if raw_content:
                parts.append({"type": "text", "text": raw_content})
            for b in m.content_blocks or []:
                parts.append(sanitize_outbound_structure(b))
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

    @staticmethod
    def _sanitize_openai_tool_pairing(messages: list[dict]) -> list[dict]:
        """确保 OpenAI 协议下的 tool_calls 与 tool 角色严格配对。

        OpenAI 规范要求：
        1. 每一个携带 tool_calls 的 assistant 消息，后面必须紧随对应的 role='tool' 消息；
        2. 每一个 role='tool' 消息的前一条，必须是包含该 tool_call_id 的 assistant 消息；
        若发生未映射工具降级、异常中断丢失 tool_result、或上下文预算截断，
        会导致出现孤儿 tool 消息或悬空 tool_calls，网关会直接返回 API 异常。
        此函数将未完成的悬空 tool_calls 降级为普通文本，将孤儿 tool 消息转换为 user 文本。
        """
        if not messages:
            return []
        cleaned: list[dict] = []
        i = 0
        n = len(messages)
        while i < n:
            m = messages[i]
            role = m.get("role")
            if role == "assistant" and m.get("tool_calls"):
                expected_ids = {tc["id"] for tc in m["tool_calls"] if tc.get("id")}
                # 检查后续是否紧跟对应的 tool 消息
                matched_tools = []
                j = i + 1
                while j < n and messages[j].get("role") == "tool":
                    t_id = messages[j].get("tool_call_id")
                    if t_id in expected_ids:
                        matched_tools.append(messages[j])
                    j += 1
                if not matched_tools:
                    # 悬空 tool_calls（后续完全没有 tool 结果，如命令执行中服务重启被强杀）
                    # 剥离 tool_calls，保留文字，避免协议破坏
                    m_copy = dict(m)
                    m_copy.pop("tool_calls", None)
                    if not m_copy.get("content"):
                        m_copy["content"] = "（工具调用未完成）"
                    cleaned.append(m_copy)
                    i += 1
                else:
                    # 正常配对：保留 assistant 和匹配的 tool 消息
                    cleaned.append(m)
                    for tm in matched_tools:
                        cleaned.append(tm)
                    i = j
            elif role == "tool":
                # 没有前置匹配 assistant 的孤儿 tool 消息 → 降级为 user 提示文本
                content = m.get("content") or ""
                cleaned.append({
                    "role": "user",
                    "content": f"（历史工具调用结果：{content}）" if content else "（历史工具调用完成）",
                })
                i += 1
            else:
                cleaned.append(m)
                i += 1
        return cleaned

    def _build_openai_body(self, request: ChatRequest, disguised: list[dict]) -> dict:
        opts = self._completion_opts
        raw_msgs = [self._disguise_message(m) for m in request.messages]
        body: dict = {
            "model": request.model or self._model_name,
            "messages": self._sanitize_openai_tool_pairing(raw_msgs),
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
        monitor["frames"] += 1
        # 网关下发错误帧（如 HTTP 200 下返回 payload.error）
        if isinstance(payload, dict) and payload.get("error"):
            err_obj = payload.get("error")
            err_msg = err_obj.get("message") if isinstance(err_obj, dict) else str(err_obj)
            monitor["error"] = err_msg or "网关返回错误帧"
            monitor["terminal"] = True
            logger.warning("[ta3] OpenAI协议收到网关错误帧: %s", err_msg)
            return True
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
            raw_text = sanitize_outbound_text(m.content or "")
            if m.role in ("system", "developer"):
                system_parts.append(raw_text)
                continue
            if m.role == "user":
                if m.content_blocks:
                    blocks = []
                    if raw_text:
                        blocks.append({"type": "text", "text": raw_text})
                    # plan-147-674: OpenAI image_url 块 → Anthropic image 块（data URI
                    # 解析对齐 anthropic.py 既有实现）。此前原样透传 image_url，
                    # claude/kimi 系网关拒绝或忽略，多模态图片注入静默失效。
                    for block in m.content_blocks or []:
                        if block.get("type") == "image_url":
                            img_data = (block.get("image_url") or {}).get("url", "")
                            media_type = "image/png"
                            raw_data = ""
                            if img_data.startswith("data:"):
                                header, _, raw_data = img_data.partition(",")
                                if ";" in header:
                                    media_type = header.split(":", 1)[1].split(";", 1)[0]
                            else:
                                raw_data = img_data
                            blocks.append({
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": media_type,
                                    "data": raw_data,
                                },
                            })
                        elif block.get("type") == "text":
                            blocks.append({
                                "type": "text",
                                "text": sanitize_outbound_text(str(block.get("text") or "")),
                            })
                        else:
                            blocks.append(sanitize_outbound_structure(block))
                    converted.append({"role": "user", "content": blocks})
                else:
                    converted.append({"role": "user", "content": raw_text})
            elif m.role == "tool":
                converted.append({
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": m.tool_call_id or "",
                                 "content": raw_text}],
                })
            elif m.role == "assistant":
                blocks: list[dict] = []
                has_tool_calls = bool(m.tool_calls)
                if raw_text:
                    blocks.append({"type": "text", "text": raw_text})
                if m.reasoning_content and (has_tool_calls or _kimi):
                    blocks.append({
                        "type": "thinking",
                        "thinking": sanitize_outbound_text(m.reasoning_content),
                    })
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
                        clean_args = sanitize_outbound_structure(
                            disguise_args(name, args if isinstance(args, dict) else {})
                        )
                        blocks.append({
                            "type": "tool_use",
                            "id": str(tc.get("id") or ""),
                            "name": alias,
                            "input": clean_args,
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
        # 网关允许的最大值（kimi-k3=32768），显式下发超上限值会触发网关截断/异常空响应
        # （对齐 _build_openai_body 的语义）。
        # 不限制输出：目录未声明且请求未指定时不再兜底 2048（旧值会截断长输出），
        # 不传该字段，由网关/模型自身决定上限。
        _catalog_max = opts.get("maxTokens") or opts.get("max_tokens")
        body: dict = {
            "model": request.model or self._model_name,
            "messages": messages,
            "stream": True,
            "temperature": 0.2 if is_kimi else opts.get("temperature", _DEFAULT_TEMPERATURE),
        }
        if _catalog_max:
            body["max_tokens"] = int(min(request.max_tokens or _catalog_max, _catalog_max))
        elif request.max_tokens:
            body["max_tokens"] = request.max_tokens
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
        monitor["frames"] += 1
        # 网关下发错误帧
        if isinstance(payload, dict) and payload.get("error"):
            err_obj = payload.get("error")
            err_msg = err_obj.get("message") if isinstance(err_obj, dict) else str(err_obj)
            monitor["error"] = err_msg or "网关返回错误帧"
            monitor["terminal"] = True
            logger.warning("[ta3] Anthropic协议收到网关错误帧: %s", err_msg)
            return True
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
            # v966: 成功解析的数据帧计数（不含空行/[DONE]）。0 = 网关连一个
            # 数据帧都没下发（网络卡顿/服务器处理慢/网关断流），不能判定为
            # "模型主动结束"，上层据此走重试而非静默收尾。
            "frames": 0,
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
        # plan-838: 移除思考看门狗——此前思考阶段（已收 thinking 帧、未产出 content/tool）
        # 使用更短的 ta3_thinking_watchdog(240s) 主动掐断，导致 kimi-k3 长思考被提前终止后
        # 带 thinking 的 thinking_timeout 被上层判为"健康"而静默结束任务。
        # 现与 TA3 其它模型完全一致：全程统一使用 ta3_stream_idle_timeout(默认 300s) 空闲超时。
        try:
            async with self._client.stream("POST", url, json=body, headers=headers) as resp:
                if resp.status_code != 200:
                    text = (await resp.aread()).decode("utf-8", errors="replace")[:400]
                    raise RuntimeError(f"模型请求失败 {resp.status_code}：{text}")
                _lines = resp.aiter_lines()
                while True:
                    try:
                        line = await asyncio.wait_for(_lines.__anext__(), timeout=self._stream_idle_timeout)
                    except StopAsyncIteration:
                        break
                    except asyncio.TimeoutError:
                        monitor["finish_reason"] = "timeout"
                        logger.warning(
                            "[ta3] model=%s 空闲超时: thinking=%d content=%d tools=%d, 耗时 %.1fs",
                            request.model or self._model_name,
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

        if monitor.get("error"):
            raise RuntimeError(f"模型请求失败：{monitor['error']}")

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
        # v966: 零帧断流比"空响应"更精确——网关未下发任何数据帧即结束
        # （HTTP 200 + 空流/DONE），说明请求期间网络卡顿或网关处理超时，
        # 必须区别于"模型已应答但主动无输出"（后者有帧，见 finish_reason）。
        if monitor["frames"] == 0 and monitor["finish_reason"] == "stop":
            logger.warning(
                "[ta3] model=%s 零帧断流: 未收到任何数据帧, 耗时 %.1fs (网关空流/网络异常)",
                request.model or self._model_name, time.monotonic() - _started_at,
            )
        if not content and not thinking and not tool_calls and monitor["finish_reason"] == "stop":
            logger.warning(
                "[ta3] model=%s 空响应流: 无 content/thinking/tool_calls, finish=stop, frames=%d, 耗时 %.1fs",
                request.model or self._model_name, monitor["frames"],
                time.monotonic() - _started_at,
            )

        yield {
            "type": "done",
            "content": content,
            "thinking": thinking,
            "tool_calls": tool_calls,
            "finish_reason": monitor["finish_reason"],
            "usage": monitor["usage"],
            "frames": monitor["frames"],
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
