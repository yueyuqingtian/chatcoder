"""TraeProvider — TRAE SOLO CN 模型供应商实现（Phase 1：llm_utils_chat 模式）。

TRAE SOLO Lite 没有暴露纯 OpenAI 兼容端点；内置模型对话走云端编排
（create_agent_task，Phase 2）。Phase 1 复用其一次性 LLM 端点
`POST {agent_host}/api/agent/v3/llm_utils_chat`（SSE 流式）：

- 请求头：Cloud-IDE-JWT + 设备指纹头（build_business_headers）
- 请求体：对齐 ai_agent LiteChatRequest 字段（user_info / client_info /
  streamlined_common_params + query 消息 JSON），对话消息转 start_chat 的
  query 块格式（[{"type":"text","data":{"content":...}}]）
- 流式解析：手动 SSE，兼容 text/content/thinking/reasoning 增量字段
- 401：通过 refresh 回调刷新一次后重试（由 registry 注入）

> 待确认（Phase 0 抓包）：llm_utils_chat 完整请求 schema 与事件字段名。
> 若该端点不支持工具调用，agent_loop 会自然降级为纯文本对话（无工具）。
方案: docs/plan-trae-solo-provider-integration.md §5.3。
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.auth.trae.business import build_business_headers
from app.models.base import ModelProvider
from app.models.schemas import ChatMessage, ChatRequest, ChatResponse, Usage

logger = logging.getLogger(__name__)

LLM_UTILS_CHAT_PATH = "/api/agent/v3/llm_utils_chat"
CREATE_AGENT_TASK_PATH = "/api/agent/v3/create_agent_task"

_FIRST_CHUNK_TIMEOUT_S = 30.0


def _is_quota_error(message: str | None) -> bool:
    """判断 IDE utility 额度耗尽错误，供自动切换 Work 编排通道。"""
    text = (message or "").lower()
    return "quota" in text or "credits" in text or "4008" in text


def _is_fallback_error(message: str | None) -> bool:
    """判断 utility 端点不支持或额度耗尽等错误，自动切换主对话 create_agent_task 通道。"""
    text = (message or "").lower()
    return (
        _is_quota_error(message)
        or "model config is empty" in text
        or "not found" in text
        or "empty for model name" in text
        or "resolvebyusage" in text
    )
_STREAM_IDLE_TIMEOUT_S = 30.0


class TraeProvider(ModelProvider):
    name = "trae"
    _credit_cache: dict[str, dict[str, float]] = {}

    def __init__(self, *, api_key: str, base_url: str, model: str, meta: dict | None = None,
                 refresh_token=None):
        self._token = api_key
        self._base_url = base_url.rstrip("/")
        self._model_name = model
        self._meta = meta or {}
        # 401 时调用（async 无参）→ 返回新 token；由 registry 注入（依赖 DB 会话）
        self._refresh_token = refresh_token
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, write=10.0, read=_STREAM_IDLE_TIMEOUT_S, pool=10.0),
            headers={"Accept-Encoding": "gzip, deflate"},
            follow_redirects=False,
        )

    # ─────────────────────────── 请求体 ───────────────────────────

    def _resolve_model_name(self) -> str:
        """请求 model_name：优先用目录下发的档位名（provider_model_name）。

        实测（2026-08-26，TRAE 服务端 0.1.56 体系，create_agent_task）：对
        全新会话服务端按 model_name 精确匹配模型配置——纯配置名
        （DeepSeek-V4-Flash-Official）报 "model config is empty for model
        name: ..."；带档位后缀的底层名（DeepSeek-V4-Flash-Official__dev）
        可被识别（通过模型解析，后续卡在 summary/history 会话校验）。
        config_name / manual_config_name 仍用纯名。目录同步见
        docs/plan-trae-solo-provider-integration.md §5.3 模型解析规则。
        """
        raw = str(self._meta.get("provider_model_name") or "").strip()
        if raw:
            return raw
        return self._resolve_config_name()

    def _resolve_config_name(self) -> str:
        """返回 TRAE 服务端用于 manual_config_name 的纯配置名。"""
        raw = str(self._meta.get("config_name") or self._model_name).strip()
        return raw.split("//")[-1]

    def _build_body(self, request: ChatRequest) -> dict:
        """构造 llm_utils_chat 请求体（对齐 ai_agent LiteChatRequest 关键字段）。

        query = start_chat 同款消息块 JSON 数组（text 块）。user_info /
        client_info / streamlined_common_params 由 meta 提供（registry 注入
        trae_auth 的账号与设备指纹）。
        """
        messages = [self._convert_message(m) for m in request.messages]
        query_blocks = [
            {"type": "text", "data": {"content": m.get("content") or ""}}
            for m in messages if m.get("role") == "user" or m.get("content")
        ]
        if not query_blocks:
            query_blocks = [{"type": "text", "data": {"content": ""}}]

        # model_name 用目录下发的档位名（provider_model_name，__dev 后缀）；
        # config_name / manual_config_name 用纯配置名。实测（服务端 0.1.56）
        # 纯名 model_name 会报 "model config is empty"，档位名可被识别。
        config_name = self._resolve_config_name()
        model_name = self._resolve_model_name()
        if not config_name or not model_name:
            raise RuntimeError("TRAE 模型配置无效：缺少 config_name/model_name")
        # 注意：llm_utils_chat 是无状态的 utility 一次性调用端点，
        # session_id 和 message_id 必须使用全新独立的 UUID hex（32位小写十六进制）；
        # 绝不能传入数据库的数字 ID（如 '67'），否则 TRAE 服务端会尝试去查云端会话
        # 历史，找不到配置或历史就会报 'model config is empty' 或 'missing history'。
        body: dict = {
            "session_id": uuid.uuid4().hex,
            "message_id": uuid.uuid4().hex,
            "message_content": messages,
            "model_name": model_name,
            "config_name": config_name,
            "model_auto_selection": {"strategy": "manual", "manual_config_name": config_name},
            "agent_type": "solo_agent_lite",
            "agent_id": "solo_agent_lite",
            # 关键：llm_utils_chat 是 utility 端点，必须指定 function 才能进入
            # 真实模型调用；缺该字段时服务端返回 code 2001
            # "resolveByUsage function is empty" 并直接 done —— 表现为"无回复"。
            "function": "chat",
            "query": json.dumps(query_blocks, ensure_ascii=False),
            "user_info": self._meta.get("user_info"),
            "client_info": self._meta.get("client_info"),
            "streamlined_common_params": self._meta.get("common_params"),
        }
        return {k: v for k, v in body.items() if v is not None}

    def _build_work_body(self, request: ChatRequest) -> dict[str, Any]:
        """构造 TRAE 主对话编排请求；Work 额度只由此通道消费。"""
        messages = [self._convert_message(m) for m in request.messages]
        query_blocks = [
            {"type": "text", "data": {"content": m.get("content") or ""}}
            for m in messages if m.get("content")
        ]
        query = json.dumps(
            query_blocks or [{"type": "text", "data": {"content": ""}}],
            ensure_ascii=False,
        )
        # 注意：create_agent_task 的 session_id/message_id 同样必须是全新 UUID hex。
        # 绝不能复用本地数据库会话 ID（如 '67'）——TRAE 服务端会按该 ID 去查云端
        # 会话历史，本地新建的会话在云端不存在，报 4000105 "missing history count exceeded"。
        # chatcoder 的 agent_loop 自行管理全量消息（message_content 已含历史），
        # 每次使用新会话即可，不依赖云端会话延续。
        session_id = uuid.uuid4().hex
        message_id = uuid.uuid4().hex
        config_name = self._resolve_config_name()
        model_name = self._resolve_model_name()
        if not config_name or not model_name:
            raise RuntimeError("TRAE 模型配置无效：缺少 config_name/model_name")
        # model_name 为档位名（provider_model_name，如 ...__dev），config_name
        # 为纯配置名；两者用途不同，见 _resolve_model_name 注释。
        user_info = dict(self._meta.get("user_info") or {})
        client_info = dict(self._meta.get("client_info") or {})
        client_info.setdefault("connect_session_id", session_id)
        client_info.setdefault("agent_task_service_strategy", "cloud_agent")
        latest_user = next(
            (m.get("content") or "" for m in reversed(messages) if m.get("role") == "user"),
            "",
        )
        body: dict[str, Any] = {
            "conversation_id": session_id,
            "user_id": user_info.get("user_id") or "",
            "device_id": self._meta.get("device_id") or "",
            "config_name": config_name,
            "ide_version": self._meta.get("ide_version") or "0.1.51",
            "user_input": {"id": message_id, "type": "text", "data": {"content": latest_user}},
            "session_id": session_id,
            "message_id": message_id,
            "message_content": messages,
            "agent_type": "solo_agent_lite",
            "agent_id": "solo_agent_lite",
            "model_name": model_name,
            "model_auto_selection": {"strategy": "manual", "manual_config_name": config_name},
            "query": query,
            "client_info": client_info,
            "user_info": user_info,
            "streamlined_common_params": self._meta.get("common_params"),
        }
        return {k: v for k, v in body.items() if v is not None}

    def _convert_message(self, m: ChatMessage) -> dict:
        """ChatMessage → TRAE 消息 dict（OpenAI 风格 role/content）。

        plan-166-767: 多模态图片块处理——TRAE query 块协议（text 类型）未确认支持
        image 直传，无法保证图片块透传；此处降级为「图片已附加 + read_attachment 提示」
        并保留文本 content，禁止图片在序列化时静默丢失。
        """
        out: dict = {"role": m.role, "content": m.content or ""}
        if m.role in ("system", "developer"):
            out["role"] = "system"
        if m.role == "assistant":
            if m.tool_calls:
                out["tool_calls"] = [
                    {
                        "id": str(tc.get("id") or f"call_{i:02d}"),
                        "type": "function",
                        "function": {
                            "name": str(tc.get("name") or ""),
                            "arguments": json.dumps(tc.get("arguments") or {},
                                                    ensure_ascii=False),
                        },
                    }
                    for i, tc in enumerate(m.tool_calls or [])
                ]
            if m.reasoning_content:
                out["reasoning_content"] = m.reasoning_content
        elif m.role == "tool":
            out["role"] = "tool"
            out["tool_call_id"] = m.tool_call_id or ""
        # plan-166-767: 图片内容块降级提示（TRAE 通道暂不支持图片直传）
        if m.content_blocks:
            _img_count = sum(
                1 for b in m.content_blocks
                if isinstance(b, dict) and b.get("type") == "image_url"
            )
            if _img_count:
                _hint = (
                    f"\n[系统] 本条消息附带 {_img_count} 张图片。当前 TRAE 通道暂无法直接传输图片，"
                    "如需查看图片内容，请调用 read_attachment 工具读取该图片的 path。"
                )
                out["content"] = ((out.get("content") or "") + _hint).strip()
        return out

    # ─────────────────────────── SSE 解析 ───────────────────────────

    def _parse_frame(self, line: str, monitor: dict) -> bool:
        """解析单条 SSE data 帧；返回是否终止。字段名做多形态兼容。"""
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
        if not isinstance(payload, dict):
            return False
        # create_agent_task 的事件常把业务字段包在 data/result/payload 中；
        # 合并一层后复用同一套错误、额度和增量解析，避免错误事件被当作空 done。
        for key in ("data", "result", "payload"):
            nested = payload.get(key)
            if isinstance(nested, dict):
                payload = {**payload, **nested}
                break

        # 额度通知（notify_usage）：先记录 IDE/Work 双额度池余量；错误事件可能
        # 同时携带该字段，必须在错误返回前缓存，否则无法自动切换 Work 通道。
        credits = payload.get("cn_credits_remain_info")
        if isinstance(credits, dict):
            monitor["credits"] = {
                k: credits.get(k) for k in ("ide_credits", "work_credits")
                if isinstance(credits.get(k), (int, float))
            }

        # 业务错误事件：{code, message}（如 function/usage 未配置、quota 超限）。
        # 必须显式抛出，否则解析器会静默产出空回复（表现为"发送后无回复无报错"）。
        error_info = payload.get("error")
        if isinstance(error_info, dict):
            payload = {**payload, **error_info}
        if (
            payload.get("code") is not None or payload.get("error_code") is not None
        ) and isinstance(payload.get("message"), str):
            monitor["error"] = payload["message"]
            monitor["terminal"] = True
            return True

        # 额度通知（notify_usage）：透出 IDE/Work 双额度池余量供错误信息提示。
        credits = payload.get("cn_credits_remain_info")
        if isinstance(credits, dict):
            monitor["credits"] = {
                k: credits.get(k) for k in ("ide_credits", "work_credits")
                if isinstance(credits.get(k), (int, float))
            }
        if payload.get("billing_mode"):
            monitor["billing_mode"] = str(payload["billing_mode"])

        usage = payload.get("usage")
        if isinstance(usage, dict):
            monitor["usage"] = Usage(
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                total_tokens=int(usage.get("total_tokens") or 0),
                cached_input_tokens=int(
                    (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0),
                reasoning_tokens=int(
                    (usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0),
            )

        # 直接增量字段：text / content / delta
        for key in ("text", "content", "delta"):
            val = payload.get(key)
            if isinstance(val, str) and val:
                monitor["content_parts"].append(val)
        # 思考增量：thinking / reasoning / reasoning_content
        for key in ("thinking", "reasoning", "reasoning_content"):
            val = payload.get(key)
            if isinstance(val, str) and val:
                monitor["thinking_parts"].append(val)

        # OpenAI 风格 choices[0].delta
        choices = payload.get("choices") or []
        if choices and isinstance(choices[0], dict):
            delta = choices[0].get("delta") or {}
            if isinstance(delta, dict):
                for key in ("reasoning_content", "reasoning", "thinking"):
                    val = delta.get(key)
                    if isinstance(val, str) and val:
                        monitor["thinking_parts"].append(val)
                val = delta.get("content")
                if isinstance(val, str) and val:
                    monitor["content_parts"].append(val)
            finish = choices[0].get("finish_reason") or choices[0].get("finishReason")
            if finish:
                monitor["finish_reason"] = str(finish)
                monitor["terminal"] = True

        # TRAE 编排风格：{type: "assistant"/"text_delta", data: "..."} 事件
        # （通用字段已处理 text/content/delta，此处仅兜底 data 字段）
        event_type = payload.get("type") or payload.get("event") or ""
        if isinstance(event_type, str) and event_type in ("assistant", "text_delta",
                                                          "message_delta", "content"):
            data = payload.get("data")
            if isinstance(data, str) and data:
                monitor["content_parts"].append(data)
            elif isinstance(data, dict):
                for key in ("text", "content", "delta"):
                    val = data.get(key)
                    if isinstance(val, str) and val:
                        monitor["content_parts"].append(val)
                        break
        if payload.get("finished") is True or event_type in ("finished", "done", "end"):
            monitor["terminal"] = True
        return False

    def _new_monitor(self) -> dict:
        return {
            "terminal": False,
            "finish_reason": "stop",
            "content_parts": [],
            "thinking_parts": [],
            "usage": Usage(),
            "error": None,
            "credits": {},
            "billing_mode": "",
        }

    async def _try_refresh_token(self) -> bool:
        if self._refresh_token is None:
            return False
        try:
            new_token = await self._refresh_token()
        except Exception as e:  # noqa: BLE001
            logger.warning("[trae] model=%s token 刷新失败: %s", self._model_name, e)
            return False
        if new_token:
            self._token = new_token
            logger.info("[trae] model=%s 401 后 token 已刷新并重试", self._model_name)
            return True
        return False

    async def _stream_endpoint(
        self, request: ChatRequest, path: str, body: dict[str, Any]
    ) -> AsyncIterator[dict]:
        """读取一个 TRAE SSE 通道并转换为统一事件。"""
        url = f"{self._base_url}{path}"
        monitor = self._new_monitor()
        logger.info("[trae] model=%s → %s", request.model or self._model_name, url)
        attempt = 0
        while True:
            headers = build_business_headers(self._token, self._meta)
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

        if monitor["credits"]:
            key = str(
                self._meta.get("user_info", {}).get("user_id")
                or self._meta.get("device_id")
                or "default"
            )
            self._credit_cache[key] = monitor["credits"]
        if monitor["error"]:
            msg = monitor["error"]
            if monitor["credits"]:
                pools = "，".join(f"{k}={v}" for k, v in monitor["credits"].items())
                msg = f"{msg}（额度池: {pools}）"
            # 服务端 0.1.56 体系的会话校验阻塞：对全新会话 create_agent_task
            # 要求云端会话存在（summary 模板）且历史完整（missing history），
            # sync_history_state 实测无法写入历史。给出明确提示而不是裸错误。
            low = msg.lower()
            if "missing history" in low or "summary" in low:
                msg = (f"{msg}（TRAE 服务端要求云端会话历史完整，新会话需先经"
                       f"TRAE 客户端建立，外部 API 无法创建；请先在 TRAE "
                       f"客户端发起一次对话，或检查 ide_credits/work_credits 额度）")
            error = RuntimeError(f"TRAE 模型调用失败：{msg}")
            error.trae_credits = monitor["credits"]
            raise error

        yield {
            "type": "done",
            "content": "".join(monitor["content_parts"]) or None,
            "thinking": "".join(monitor["thinking_parts"]) or None,
            "tool_calls": [],
            "finish_reason": monitor["finish_reason"],
            "usage": monitor["usage"],
        }

    async def _stream_llm(self, request: ChatRequest) -> AsyncIterator[dict]:
        """按已知额度选择通道：IDE 有余量走 utility，否则自动走 Work 主对话。

        llm_utils_chat 消费 IDE 额度，create_agent_task 消费 Work 额度；
        IDE 耗尽后必须自动回退 Work 通道（两个通道的 session_id 均为
        每次请求全新 UUID，见 _build_body / _build_work_body）。
        """
        key = str(
            self._meta.get("user_info", {}).get("user_id")
            or self._meta.get("device_id")
            or "default"
        )
        credits = self._credit_cache.get(key, {})
        work_available = float(credits.get("work_credits", 0) or 0) > 0
        ide_exhausted = "ide_credits" in credits and float(credits.get("ide_credits", 0) or 0) <= 0
        if ide_exhausted and work_available:
            logger.info("[trae] IDE 额度为 0，自动切换 Work create_agent_task")
            work_body = self._build_work_body(request)
            async for event in self._stream_endpoint(request, CREATE_AGENT_TASK_PATH, work_body):
                yield event
            return

        try:
            utility_body = self._build_body(request)
            async for event in self._stream_endpoint(request, LLM_UTILS_CHAT_PATH, utility_body):
                yield event
        except RuntimeError as error:
            err_str = str(error)
            credits = getattr(error, "trae_credits", {})
            work_available = float(credits.get("work_credits", 0) or 0) > 0
            # 当 utility 报额度不足、model config is empty、未找到配置等错误时，自动尝试主对话通道
            should_fallback = _is_fallback_error(err_str)
            if not should_fallback:
                raise
            logger.info("[trae] utility 调用失败（%s），自动回退尝试 Work create_agent_task 通道", err_str)
            try:
                work_body = self._build_work_body(request)
                async for event in self._stream_endpoint(request, CREATE_AGENT_TASK_PATH, work_body):
                    yield event
            except Exception as e:
                logger.warning("[trae] Work 通道回退也失败: %s", e)
                raise error from e

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
