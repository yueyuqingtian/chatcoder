"""压缩服务：压缩块索引查询、被压缩消息原文还原（v30.1）。

压缩产物（SUMMARY checkpoint 消息 + shared_context.compactions）是"软阴影"——
被压缩消息物理保留在 messages 表，仅从上下文构建中排除。本服务提供：

1. list_compaction_index —— 会话内全部压缩块索引（AI/前端可据此定位压缩前会话）；
2. get_compacted_messages —— 按 compaction_id 取被压缩消息的完整原文；
3. restore_compaction —— 还原：把被压缩消息从 compacted_ids 移除，
   使其重新参与上下文构建（SUMMARY 消息标记 restored，保留可查看）。

索引结构（shared_context.compactions 条目）：
{
  "index": 1,                      # 会话内压缩块序号（从 1 起，AI 索引）
  "compaction_id": "cp-xxx",       # 全局唯一 id
  "summary_message_id": 42,        # checkpoint SUMMARY 消息
  "shadowed_ids": [12,13,14,15],   # 被压缩遮蔽的消息 id（时间序）
  "shadowed_tokens": 3200,
  "saved_tokens": 2900,
  "trigger": "pressure",
  "created_at": "..."
}
"""
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


def _extract_summary_preview(text: str, limit: int = 300) -> str:
    """从 checkpoint 帧文本提取摘要预览（剥掉 preamble 与帧标签）。"""
    if not text:
        return ""
    open_tag, close_tag = "<compacted-summary>", "</compacted-summary>"
    if open_tag in text:
        inner = text.split(open_tag, 1)[1]
        if close_tag in inner:
            inner = inner.split(close_tag, 1)[0]
        text = inner
    return text.strip()[:limit]


def _session_ctx(session) -> dict:
    ctx = getattr(session, "shared_context", None)
    return ctx if isinstance(ctx, dict) else {}


async def list_compaction_index(db: AsyncSession, session_id: int) -> list[dict]:
    """返回会话内全部压缩块索引（按压缩发生顺序）。

    每条含 index / compaction_id / summary_message_id / shadowed_ids /
    shadowed_tokens / saved_tokens / trigger / created_at / summary_preview。
    """
    from app.persistence.models.message import Message, Session

    session = await db.get(Session, session_id)
    if session is None:
        return []
    ctx = _session_ctx(session)
    compactions = ctx.get("compactions") or []
    if not compactions:
        return []
    result: list[dict] = []
    for c in compactions:
        entry = {
            "index": c.get("index"),
            "compaction_id": c.get("compaction_id"),
            "summary_message_id": c.get("summary_message_id"),
            "shadowed_ids": list(c.get("shadowed_ids") or []),
            "shadowed_tokens": c.get("shadowed_tokens", 0),
            "saved_tokens": c.get("saved_tokens", 0),
            "trigger": c.get("trigger", "pressure"),
            "created_at": c.get("created_at"),
            "summary_preview": "",
        }
        msg_id = c.get("summary_message_id")
        if msg_id:
            try:
                m = await db.get(Message, msg_id)
                if m is not None and isinstance(m.content, dict):
                    entry["summary_preview"] = _extract_summary_preview(str(m.content.get("text") or ""))
            except Exception:
                logger.debug("[compression] 摘要预览读取失败(非阻塞)", exc_info=True)
        result.append(entry)
    return result


async def find_compaction(db: AsyncSession, session_id: int, compaction_id: str) -> dict | None:
    """按 compaction_id 查找压缩块记录。"""
    from app.persistence.models.message import Session

    session = await db.get(Session, session_id)
    if session is None:
        return None
    ctx = _session_ctx(session)
    for c in ctx.get("compactions") or []:
        if c.get("compaction_id") == compaction_id:
            return c
    return None


async def get_compacted_messages(
    db: AsyncSession, session_id: int, compaction_id: str,
) -> list:
    """按压缩块 id 返回被压缩消息的完整原文（时间正序）。

    Args:
        db: 数据库会话。
        session_id: 会话 id。
        compaction_id: 压缩块 id（compaction_index 工具返回的 compaction_id）。

    Returns:
        Message 模型列表；压缩块不存在时抛 KeyError。
    """
    from app.persistence.models.message import Message

    comp = await find_compaction(db, session_id, compaction_id)
    if comp is None:
        raise KeyError(f"压缩块不存在: {compaction_id}")
    ids = [i for i in (comp.get("shadowed_ids") or []) if isinstance(i, int)]
    if not ids:
        return []
    res = await db.execute(
        select(Message)
        .where(Message.session_id == session_id, Message.id.in_(ids))
        .order_by(Message.id.asc())
    )
    return list(res.scalars().all())


async def restore_compaction(
    db: AsyncSession, session_id: int, compaction_id: str,
) -> int:
    """还原压缩块：被压缩消息重新参与上下文构建。

    操作（单一事务）：
    1. 从 shared_context.compacted_ids 移除 shadowed_ids（消息重新参与上下文构建）；
    2. compactions 记录保留但标记 restored=True（索引/原文仍可查，避免
       get_compacted_messages 抛 KeyError 与压缩序号重复；已还原块不再注入
       checkpoint、也不再被渐进摘要吞掉）；
    3. 从 injected_compactions 移除该块（避免还原后 checkpoint 重复注入）；
    4. SUMMARY 消息 content 标记 restored=True（前端据此隐藏"还原"入口）。

    Returns:
        还原的消息条数。
    Raises:
        KeyError: 压缩块不存在。
    """
    from app.persistence.models.message import Message, Session

    session = await db.get(Session, session_id)
    if session is None:
        raise KeyError("会话不存在")
    ctx = _session_ctx(session)
    compactions = list(ctx.get("compactions") or [])
    target = next((c for c in compactions if c.get("compaction_id") == compaction_id), None)
    if target is None:
        raise KeyError(f"压缩块不存在: {compaction_id}")

    shadowed_ids = [i for i in (target.get("shadowed_ids") or []) if isinstance(i, int)]

    compacted = set(ctx.get("compacted_ids") or [])
    compacted.difference_update(shadowed_ids)
    # 拷贝新 dict 再赋值：JSON 列同引用赋值不触发 UPDATE（SQLAlchemy 按 identity 检测 dirty）
    new_ctx = dict(ctx)
    new_ctx["compacted_ids"] = sorted(compacted)
    # v33: 保留压缩块记录并标记已还原（此前直接删除导致原文接口失效、序号重复、
    # 且已还原块无法被上下文侧识别排除）
    new_ctx["compactions"] = [
        {**c, "restored": True} if c.get("compaction_id") == compaction_id else c
        for c in compactions
    ]
    injected = set(ctx.get("injected_compactions") or [])
    injected.discard(compaction_id)
    new_ctx["injected_compactions"] = list(injected)

    # SUMMARY 消息标记已还原（保留展示，但不再作为活跃 checkpoint）
    msg_id = target.get("summary_message_id")
    if msg_id:
        try:
            m = await db.get(Message, msg_id)
            if m is not None and isinstance(m.content, dict):
                m.content = {**m.content, "restored": True}
        except Exception:
            logger.debug("[compression] SUMMARY 标记 restored 失败(非阻塞)", exc_info=True)

    session.shared_context = new_ctx
    await db.flush()
    await db.commit()
    logger.info("[compression] session=%s 还原压缩块 %s: %d 条消息恢复上下文",
                session_id, compaction_id, len(shadowed_ids))
    return len(shadowed_ids)
