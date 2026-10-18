"""会话标题归纳（plan-156-619 S8）。

【为什么需要这一层】
改造前 `session_service.auto_title_session` 只是把首条用户消息截掉 30 个字当标题，
于是侧栏里全是「你帮我看一下这个…」「继续」这类无信息量的行。

现在改为**一次轻量 AI 调用**归纳成 ≤14 字的中文标题。三条硬约束：

1. **不阻塞**：命名是锦上添花，绝不能因为模型慢/失败而拖住用户发消息。
   → 调用方用 `asyncio.create_task` 触发，本模块内部再套一层超时。
2. **可回落**：任何异常（无 provider、超时、空输出）都返回 None，
   由调用方回落原有截断逻辑，界面不会出现空标题。
3. **不覆盖用户手改**：是否允许写回由调用方的 `title_source` 判定，
   本模块只负责「算出一个标题」，不碰数据库。
"""
from __future__ import annotations

import asyncio
import logging
import re

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# ── 口径常量（不写裸字面量）──
MAX_TITLE_CHARS = 14       # 标题上限：侧栏一行能舒服显示的宽度
MAX_INPUT_CHARS = 1200     # 送进模型的消息截断：标题只需要开头语义
TIMEOUT_S = 8.0            # 单次调用超时：超过就回落截断，不排队等模型
MAX_OUTPUT_TOKENS = 32     # 只要一个短语，给足余量但不放任长输出
TEMPERATURE = 0.0          # 同一段话应得到稳定标题

# 模型偶尔会带这些包装，统一剥掉
_QUOTES = "\"'“”‘’「」『』《》【】（）()"
_PREFIX_RE = re.compile(r"^(标题|会话标题|title)\s*[:：]\s*", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")


def _clean(raw: str) -> str:
    """把模型输出清洗成「一行、无包装、不超长」的标题。"""
    text = (raw or "").strip()
    if not text:
        return ""
    # 只取第一行：模型可能带解释
    text = text.splitlines()[0].strip()
    text = _PREFIX_RE.sub("", text)
    text = text.strip(_QUOTES).strip()
    # 去掉结尾标点，标题不该以句号收尾
    text = text.rstrip("。.!！?？,，;；:：")
    text = _WS_RE.sub(" ", text).strip()
    if len(text) > MAX_TITLE_CHARS:
        text = text[:MAX_TITLE_CHARS].rstrip()
    return text


def _build_prompt(first_text: str) -> list[dict]:
    """构造一次极简对话：系统给出规则，用户给消息正文。"""
    system = (
        "你是会话标题生成器。把用户的消息概括成一句简短标题。\n"
        "规则：\n"
        f"1. 不超过 {MAX_TITLE_CHARS} 个汉字（英文按单词计）；\n"
        "2. 动宾结构，说清「做什么」，不要「帮我」「请」这类客套；\n"
        "3. 不要标点、不要引号、不要解释，只输出标题本身；\n"
        "4. 使用与用户消息相同的语言。"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": first_text[:MAX_INPUT_CHARS]},
    ]


async def summarize_title(
    db: AsyncSession,
    *,
    first_text: str,
    model_id: int | None = None,
) -> str | None:
    """为一段消息归纳标题；失败返回 None（调用方回落截断）。"""
    text = (first_text or "").strip()
    if not text:
        return None
    # 没有数据库会话就无法解析会话模型 → 直接回落（调用方用截断标题），
    # 不要在 None 上调 .get() 再靠异常兜底（会打出一条误导性的 warning 日志）。
    if db is None:
        return None

    try:
        from app.models.registry import get_model_registry
        from app.models.schemas import ChatMessage, ChatRequest

        registry = get_model_registry()
        provider = None
        if model_id:
            from app.persistence.models.model_reg import Model

            row = await db.get(Model, model_id)
            if row is not None:
                provider, _reason = await registry.get_provider_for_model(db, row)
        if provider is None:
            provider = registry.get_default_provider()
        if provider is None:
            return None

        req = ChatRequest(
            messages=[ChatMessage(**m) for m in _build_prompt(text)],
            model="",  # provider 内部按自身配置补模型名（与压缩摘要同口径）
            temperature=TEMPERATURE,
            max_tokens=MAX_OUTPUT_TOKENS,
        )
        resp = await asyncio.wait_for(provider.chat(req), timeout=TIMEOUT_S)
        title = _clean(resp.content or "")
        if not title:
            return None
        return title
    except asyncio.TimeoutError:
        logger.info("[title] 归纳超时（%.1fs），回落截断", TIMEOUT_S)
        return None
    except Exception:  # noqa: BLE001 — 命名失败绝不能影响消息发送
        logger.warning("[title] 归纳失败，回落截断", exc_info=True)
        return None
