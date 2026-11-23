"""plan-20261101-performance：按游标读取展示窗口，查询不随历史正文体量扩张。"""
import re
import time
from sqlalchemy import select, func, cast, Text, LargeBinary
from sqlalchemy.ext.asyncio import AsyncSession
from app.persistence.models.message import Message, Session
from app.services.message_service import _to_out

_REF_LINE_RE = re.compile(r"^(引用文件|引用文件夹|引用会话|使用技能|使用连接器|使用插件|使用微应用)：")
_OUTLINE_TEXT_MAX = 200

stats = {"queries": 0, "last_query_ms": 0.0, "last_rows": 0}
WINDOW_BYTES = 1024 * 1024


async def _take(db: AsyncSession, stmt, limit: int, budget: int = WINDOW_BYTES) -> list[Message]:
    # 先取 ID/长度，避免为了选出 1MB 窗口就把上百条大正文全解码到 Python。
    size = func.octet_length(cast(Message.content, Text)) if db.bind.dialect.name == "postgresql" else func.length(cast(Message.content, LargeBinary))
    candidates = (await db.execute(stmt.with_only_columns(Message.id, size.label("bytes")).limit(limit))).all()
    chosen, used = [], 0
    for mid, length in candidates:
        cost = int(length or 0) + 384
        if chosen and used + cost > budget:
            break
        chosen.append(mid)
        used += cost
    if not chosen:
        return []
    return list((await db.execute(stmt.order_by(None).where(Message.id.in_(chosen)).order_by(Message.id.asc()))).scalars())


def scope(session_id: int, thread_id: int | None):
    return (Message.session_id == session_id, Message.thread_id == thread_id,
            Message.deleted.is_(False))


async def page(db: AsyncSession, session_id: int, thread_id: int | None = None,
               before_id: int | None = None, after_id: int | None = None,
               around_id: int | None = None, limit: int = 100) -> dict:
    if sum(value is not None for value in (before_id, after_id, around_id)) > 1:
        raise ValueError("before_id、after_id、around_id 只能提供一个")
    limit = max(1, min(200, limit))
    started = time.monotonic()
    revision = (await db.execute(select(Session.message_revision).where(Session.id == session_id))).scalar_one_or_none()
    if revision is None:
        raise LookupError("会话不存在")
    base = select(Message).where(*scope(session_id, thread_id))
    if around_id is not None:
        left = await _take(db, base.where(Message.id < around_id).order_by(Message.id.desc()), limit // 2, WINDOW_BYTES // 2)
        right = await _take(db, base.where(Message.id >= around_id).order_by(Message.id.asc()), limit - len(left), WINDOW_BYTES // 2)
        rows = left + right
    elif after_id is not None:
        rows = await _take(db, base.where(Message.id > after_id).order_by(Message.id.asc()), limit)
    else:
        query = base.where(Message.id < before_id) if before_id is not None else base
        rows = await _take(db, query.order_by(Message.id.desc()), limit)
    lower = rows[0].id if rows else before_id
    upper = rows[-1].id if rows else after_id
    older = bool(lower and (await db.execute(select(Message.id).where(*scope(session_id, thread_id), Message.id < lower).limit(1))).first())
    newer = bool(upper and (await db.execute(select(Message.id).where(*scope(session_id, thread_id), Message.id > upper).limit(1))).first())
    # 只补结果所需的调用，不补整轮正文；上下文消息不参与分页游标。
    calls = {str((m.content or {}).get("call_key")) for m in rows if m.msg_type == "tool_call"}
    missing = {str((m.content or {}).get("call_key")) for m in rows if m.msg_type == "tool_result"} - calls - {"None", ""}
    context = []
    if missing:
        context = list((await db.execute(base.where(Message.msg_type == "tool_call",
            Message.turn_id.in_({m.turn_id for m in rows if m.msg_type == "tool_result"}),
            Message.content["call_key"].as_string().in_(missing)).order_by(Message.id.asc()).limit(200))).scalars())
    turn_ids = list(dict.fromkeys(m.turn_id for m in rows if m.turn_id is not None))
    if turn_ids:
        first_ids = select(func.min(Message.id)).where(*scope(session_id, thread_id), Message.turn_id.in_(turn_ids)).group_by(Message.turn_id)
        # 续跑边界只补轻量系统消息，让现有轮次合并规则在截断页上也能识别唤醒。
        continuations = list((await db.execute(base.where(Message.id.in_(first_ids), Message.sender_type == "system"))).scalars())
        known = {m.id for m in rows + context}
        context.extend(m for m in continuations if m.id not in known)
    query_ms = (time.monotonic() - started) * 1000
    result = {"items": [_to_out(m).model_dump() for m in rows],
            "context": [_to_out(m).model_dump() for m in context],
            "before_id": lower, "after_id": upper, "has_before": older, "has_after": newer,
            "revision": revision,
            "boundary_turn_ids": turn_ids}
    stats.update(queries=stats["queries"] + 1, last_query_ms=round(query_ms, 2), last_rows=len(rows),
                 last_convert_ms=round((time.monotonic() - started) * 1000 - query_ms, 2))
    return result


async def search(db: AsyncSession, session_id: int, query: str, thread_id: int | None = None,
                 before_id: int | None = None, limit: int = 100) -> dict:
    # 只投影摘要字段，搜索结果不把大工具输出重新传给客户端。
    text = func.coalesce(Message.content["text"].as_string(),
                         Message.content["output"].as_string(), "")
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    stmt = select(Message.id, Message.turn_id, text.label("text")).where(
        *scope(session_id, thread_id), text.ilike(f"%{escaped}%", escape="\\"))
    if before_id is not None:
        stmt = stmt.where(Message.id < before_id)
    rows = (await db.execute(stmt.order_by(Message.id.desc()).limit(min(200, limit) + 1))).all()
    return {"items": [{"id": row.id, "turn_id": row.turn_id,
                        "snippet": row.text[max(0, row.text.lower().find(query.lower()) - 50):][:240]}
                       for row in rows[:limit]], "has_more": len(rows) > limit,
            "before_id": rows[min(limit, len(rows)) - 1].id if rows else None}


def _strip_ref_lines(text: str, has_refs: bool) -> str:
    """剥离发送时追加的引用文本行，避免长文本截断后前缀污染定位浮窗摘要（plan-20261121-jumpdots-v2）。"""
    if not has_refs or not text:
        return text.strip()
    lines = [ln for ln in text.split("\n") if not _REF_LINE_RE.match(ln.strip())]
    return "\n".join(lines).strip()


def _compact_attachments(raw: object) -> list[dict]:
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "")
        filename = str(item.get("filename") or "")
        if not url and not filename:
            continue
        out.append({
            "file_id": str(item.get("file_id") or ""),
            "filename": filename,
            "url": url,
            "size": int(item.get("size") or 0),
            "mime_type": str(item.get("mime_type") or ""),
            "type": str(item.get("type") or ""),
        })
    return out


def _compact_refs(raw: object) -> list[dict]:
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        value = item.get("value")
        if not isinstance(kind, str) or not isinstance(value, str):
            continue
        entry: dict = {
            "kind": kind,
            "value": value,
            "label": str(item.get("label") or value),
        }
        if isinstance(item.get("project_label"), str) and item["project_label"]:
            entry["project_label"] = item["project_label"]
        out.append(entry)
    return out


async def outline(db: AsyncSession, session_id: int, thread_id: int | None = None) -> dict:
    """plan-20261121-jumpdots-v2：全会话用户提问轻量大纲，支撑跨分页定位条与附件预览。"""
    revision = (await db.execute(select(Session.message_revision).where(Session.id == session_id))).scalar_one_or_none()
    if revision is None:
        raise LookupError("会话不存在")
    rows = (await db.execute(
        select(Message.id, Message.turn_id, Message.content, Message.created_at)
        .where(*scope(session_id, thread_id), Message.sender_type == "user")
        .order_by(Message.id.asc())
    )).all()
    items: list[dict] = []
    for row in rows:
        content = row.content if isinstance(row.content, dict) else {}
        if (content.get("subagent_wakeup") is True
                or content.get("workflow_wakeup") is True
                or content.get("goal_continuation") is True):
            continue
        refs = _compact_refs(content.get("refs"))
        attachments = _compact_attachments(content.get("attachments"))
        clean_text = _strip_ref_lines(str(content.get("text") or ""), bool(refs))
        if len(clean_text) > _OUTLINE_TEXT_MAX:
            clean_text = clean_text[:_OUTLINE_TEXT_MAX] + "…"
        items.append({
            "message_id": row.id,
            "turn_id": row.turn_id,
            "created_at": str(row.created_at) if row.created_at else None,
            "text": clean_text,
            "attachments": attachments,
            "refs": refs,
        })
    return {"items": items, "revision": revision}
