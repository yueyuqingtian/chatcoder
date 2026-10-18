"""后续任务建议服务（plan-156-619 S10）。

【状态机】
    pending ──accept──▶ accepted
        └────dismiss──▶ dismissed（此后永不再展示）

【单条 pending 口径】
同一会话只保留一条 pending：AI 再次提建议时，旧的 pending 自动置 `dismissed`。
理由：建议卡固定在消息流底部，两条并存会让「取消」语义含糊（取消哪一条？）。

【为什么 accept 要先建子会话再改状态】
见 `accept()` 注释：先把子会话建成功，再把建议置 accepted。
顺序反了会出现「建议已消费、子会话没建成」——用户既看不到卡、也没得到子会话。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# ── 状态常量（避免各处写裸字面量）──
STATUS_PENDING = "pending"
STATUS_ACCEPTED = "accepted"
STATUS_DISMISSED = "dismissed"

# 建议载荷的长度上限（防止模型塞一篇文档进来把卡片撑爆）
MAX_TITLE_CHARS = 60
MAX_SUMMARY_CHARS = 200
MAX_STEPS = 8
MAX_STEP_CHARS = 160


class SuggestionError(Exception):
    """建议操作的业务错误（路由转 400）。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_payload(raw: dict) -> dict:
    """把模型给的建议收拾成固定形状。"""
    steps = raw.get("steps") or []
    if not isinstance(steps, list):
        steps = []
    clean_steps = [str(s).strip()[:MAX_STEP_CHARS] for s in steps if str(s).strip()]
    return {
        "title": str(raw.get("title") or "").strip()[:MAX_TITLE_CHARS] or "下一步建议",
        "summary": str(raw.get("summary") or "").strip()[:MAX_SUMMARY_CHARS],
        "steps": clean_steps[:MAX_STEPS],
        "mode": str(raw.get("mode") or "agent").strip() or "agent",
        "rationale": str(raw.get("rationale") or "").strip()[:MAX_SUMMARY_CHARS],
    }


async def create_suggestion(
    db: AsyncSession,
    *,
    session_id: int,
    turn_id: int | None,
    payload: dict,
) -> int:
    """写入一条 pending 建议，并把该会话此前 pending 的置为 dismissed。"""
    from app.persistence.database import run_write_locked
    from app.persistence.models.suggestion import TaskSuggestion

    normalized = _normalize_payload(payload)

    def _do(s) -> int:
        old = s.execute(
            select(TaskSuggestion).where(
                TaskSuggestion.session_id == session_id,
                TaskSuggestion.status == STATUS_PENDING,
            )
        ).scalars().all()
        for row in old:
            row.status = STATUS_DISMISSED
            row.decided_at = _now()
        row = TaskSuggestion(
            session_id=session_id,
            turn_id=turn_id,
            payload_json=normalized,
            status=STATUS_PENDING,
        )
        s.add(row)
        s.flush()
        sid = row.id
        s.commit()
        return sid

    sid = await run_write_locked(_do, label="suggestion.create")
    await _broadcast(session_id, sid, normalized)
    return sid


async def _broadcast(session_id: int, suggestion_id: int, payload: dict) -> None:
    """通知前端有新建议（消息流底部据此渲染卡片）。"""
    try:
        from app.orchestration.agent_events import broadcast

        await broadcast(session_id, {
            "event": "suggestion.created",
            "payload": {"session_id": session_id, "suggestion_id": suggestion_id, **payload},
        })
    except Exception:  # noqa: BLE001 — 广播失败不影响落库结果
        logger.debug("[suggestion] 广播失败 sid=%s", suggestion_id, exc_info=True)


async def latest_pending(db: AsyncSession, session_id: int) -> dict | None:
    """取该会话当前待处理的建议（没有则返回 None）。"""
    from app.persistence.models.suggestion import TaskSuggestion

    row = (await db.execute(
        select(TaskSuggestion)
        .where(TaskSuggestion.session_id == session_id, TaskSuggestion.status == STATUS_PENDING)
        .order_by(TaskSuggestion.id.desc())
        .limit(1)
    )).scalars().first()
    if row is None:
        return None
    return {
        "id": row.id,
        "session_id": row.session_id,
        "turn_id": row.turn_id,
        **(row.payload_json or {}),
    }


async def _get_pending_row(db: AsyncSession, suggestion_id: int):
    from app.persistence.models.suggestion import TaskSuggestion

    row = await db.get(TaskSuggestion, suggestion_id)
    if row is None:
        raise SuggestionError("建议不存在")
    if row.status != STATUS_PENDING:
        raise SuggestionError("该建议已处理过")
    return row


async def dismiss(db: AsyncSession, suggestion_id: int) -> None:
    """取消建议：置 dismissed，此后永不再展示（跨刷新、跨重启都成立）。"""
    from app.persistence.database import run_write_locked
    from app.persistence.models.suggestion import TaskSuggestion

    await _get_pending_row(db, suggestion_id)  # 先校验存在且仍 pending

    def _do(s) -> None:
        row = s.get(TaskSuggestion, suggestion_id)
        if row is None:
            return
        row.status = STATUS_DISMISSED
        row.decided_at = _now()
        s.commit()

    await run_write_locked(_do, label="suggestion.dismiss")


async def accept(db: AsyncSession, suggestion_id: int) -> dict:
    """接受建议 → 建一个子会话执行建议内容。

    **顺序很重要**：先建子会话，成功后再把建议置 accepted。
    反序会出现「建议已消费但子会话没建成」——用户既看不到卡、也没拿到子会话。
    """
    from app.persistence.database import run_write_locked
    from app.persistence.models.suggestion import TaskSuggestion
    from app.services import subsession_service

    row = await _get_pending_row(db, suggestion_id)
    payload = dict(row.payload_json or {})
    session_id = row.session_id

    task_lines = [payload.get("summary") or ""]
    steps = payload.get("steps") or []
    if steps:
        task_lines.append("步骤：")
        task_lines.extend(f"{i + 1}. {s}" for i, s in enumerate(steps))
    task = "\n".join(x for x in task_lines if x)

    subsession_id = await subsession_service.create_subsession(
        db,
        parent_session_id=session_id,
        title=payload.get("title") or "后续任务",
        task=task or payload.get("title") or "继续推进",
        context=payload.get("rationale") or "",
        mode=payload.get("mode") or None,
    )

    def _do(s) -> None:
        r = s.get(TaskSuggestion, suggestion_id)
        if r is None:
            return
        r.status = STATUS_ACCEPTED
        r.decided_at = _now()
        s.commit()

    await run_write_locked(_do, label="suggestion.accept")
    return {"subsession_id": subsession_id, "session_id": session_id}
