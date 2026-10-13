"""续跑与重试前的模型可达性预检（设计 §6.3 第 2 步）。

按会话实际使用的模型（与代理循环的解析顺序一致：会话级模型优先，否则服务端默认模型）解析供应商，
发一条 max_tokens=1 的最小请求。不可达时调用方保持暂停、不改动节点，也不消耗重试次数。
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from app.models.schemas import ChatMessage, ChatRequest

logger = logging.getLogger(__name__)

PROBE_TIMEOUT_SEC = 12.0

# 注册表返回的原因码 → 界面文案；未知原因保留原码，便于排查
_REASON_TEXT = {
    "no_default_configured": "未配置默认模型",
    "provider_disabled": "模型所属供应商已停用",
}


def _reason_text(reason: str | None) -> str:
    if not reason:
        return "模型不可用"
    if reason in _REASON_TEXT:
        return _REASON_TEXT[reason]
    if reason.endswith("_login_required"):
        return "供应商登录已失效，请在模型设置中重新登录"
    if reason.endswith("_model_missing"):
        return "尚未选择模型"
    return f"模型不可用（{reason}）"


def _describe(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status is not None:
        return f"模型服务返回 HTTP {status}"
    if isinstance(exc, httpx.RequestError):
        return f"无法连接模型服务（{type(exc).__name__}）"
    return str(exc)[:160] or type(exc).__name__


async def probe_session_model(session_id: int | None) -> tuple[bool, str]:
    """返回 (可达, 不可达时的原因)。"""
    from app.models.registry import get_model_registry
    from app.persistence.database import async_session_factory
    from app.persistence.models.message import Session as ChatSession
    from app.persistence.models.model_reg import Model

    async with async_session_factory() as db:
        model = None
        if session_id is not None:
            row = await db.get(ChatSession, session_id)
            model_id = getattr(row, "model_id", None) if row is not None else None
            if model_id:
                model = await db.get(Model, model_id)
        provider, reason = await get_model_registry().get_provider_for_model(db, model)
    if provider is None:
        return False, _reason_text(reason)

    request = ChatRequest(
        messages=[ChatMessage(role="user", content="ping")], model="", max_tokens=1
    )
    try:
        await asyncio.wait_for(provider.chat(request), timeout=PROBE_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        return False, "模型服务响应超时"
    except Exception as exc:  # 任何失败都视为暂不可用，原因交给界面展示
        logger.info("[workflow] 模型可达性预检失败：%s", exc)
        return False, _describe(exc)
    return True, ""
