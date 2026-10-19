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
# plan-156-620：接受处理中（已占用、子会话尚未建成）。双击 / 并发请求只有一个能占用成功
STATUS_ACCEPTING = "accepting"
STATUS_ACCEPTED = "accepted"
STATUS_DISMISSED = "dismissed"

# 「接受处理中」的占用超过该时长视为异常残留（进程在建子会话途中退出），允许重新接受
ACCEPTING_STALE_S = 120

# 建议的执行模式白名单（模型可能给出任意字符串）
ALLOWED_MODES = frozenset({"readonly", "plan", "agent", "workflow"})
DEFAULT_MODE = "agent"

# 建议载荷的长度上限（防止模型塞一篇文档进来把卡片撑爆）
MAX_TITLE_CHARS = 60
MAX_SUMMARY_CHARS = 200
MAX_STEPS = 8
MAX_STEP_CHARS = 160


class SuggestionError(Exception):
    """建议操作的业务错误（路由转 400）。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_mode(raw) -> str:
    mode = str(raw or "").strip().lower()
    return mode if mode in ALLOWED_MODES else DEFAULT_MODE


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
        "mode": _normalize_mode(raw.get("mode")),
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
        .where(
            TaskSuggestion.session_id == session_id,
            TaskSuggestion.status.in_((STATUS_PENDING, STATUS_ACCEPTING)),
        )
        .order_by(TaskSuggestion.id.desc())
        .limit(1)
    )).scalars().first()
    # 处理中的不展示；超时残留的重新展示，让用户可以再次执行
    if row is None or (row.status == STATUS_ACCEPTING and not _is_stale_accepting(row)):
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
    if row.status != STATUS_PENDING and not _is_stale_accepting(row):
        raise SuggestionError("该建议已处理过")
    return row


async def dismiss(db: AsyncSession, suggestion_id: int) -> None:
    """取消建议：置 dismissed，此后永不再展示（跨刷新、跨重启都成立）。

    plan-156-621：状态判断必须放进写事务做条件更新。此前在事务外读、事务内无条件写，
    与 accept 的占用竞态时会把刚进入 accepting 的建议抹成 dismissed——卡片消失、
    子会话却已经建出来，用户失去取消入口。
    """
    from app.persistence.database import run_write_locked
    from app.persistence.models.suggestion import TaskSuggestion

    await _get_pending_row(db, suggestion_id)  # 先识别「不存在」，事务内再判一次状态

    def _do(s) -> bool:
        row = s.get(TaskSuggestion, suggestion_id)
        if row is None:
            return False
        # 与 _claim 同一口径：pending 或超时残留的 accepting 才允许取消
        if row.status != STATUS_PENDING and not _is_stale_accepting(row):
            return False
        row.status = STATUS_DISMISSED
        row.decided_at = _now()
        s.commit()
        return True

    if not await run_write_locked(_do, label="suggestion.dismiss"):
        raise SuggestionError("该建议已处理过")


async def dismiss_pending_for_session(session_id: int) -> int:
    """用户发了新消息而没处理建议卡：把该会话所有 pending 建议默认置 dismissed，此后不再展示。

    只动 pending 与超时残留的 accepting——未超时的 accepting（正在建子会话）不能被抹掉，否则用户失去取消入口。
    返回被取消的条数。
    """
    from app.persistence.database import run_write_locked
    from app.persistence.models.suggestion import TaskSuggestion

    def _do(s) -> int:
        rows = s.execute(
            select(TaskSuggestion).where(
                TaskSuggestion.session_id == session_id,
                TaskSuggestion.status.in_((STATUS_PENDING, STATUS_ACCEPTING)),
            )
        ).scalars().all()
        # 超时残留的 accepting 会被 latest_pending 重新展示，同样要清；未超时的是正在建子会话，保留
        rows = [r for r in rows if r.status == STATUS_PENDING or _is_stale_accepting(r)]
        for row in rows:
            row.status = STATUS_DISMISSED
            row.decided_at = _now()
        if rows:
            s.commit()
        return len(rows)

    return int(await run_write_locked(_do, label="suggestion.dismiss_pending"))


async def accept(db: AsyncSession, suggestion_id: int) -> dict:
    """接受建议 → 建一个子会话执行建议内容。

    **顺序很重要**：先建子会话，成功后再把建议置 accepted。
    反序会出现「建议已消费但子会话没建成」——用户既看不到卡、也没拿到子会话。
    """
    from app.services import subsession_service

    row = await _get_pending_row(db, suggestion_id)
    payload = dict(row.payload_json or {})
    session_id = row.session_id

    # plan-156-620：先原子占用（pending → accepting）。此前只在读阶段校验 pending，
    # 双击或重复请求会各建一个子会话。占用失败说明已被别的请求处理。
    if not await _claim(suggestion_id):
        raise SuggestionError("该建议已处理过")

    task_lines = [payload.get("summary") or ""]
    steps = payload.get("steps") or []
    if steps:
        task_lines.append("步骤：")
        task_lines.extend(f"{i + 1}. {s}" for i, s in enumerate(steps))
    task = "\n".join(x for x in task_lines if x)

    try:
        subsession_id = await subsession_service.create_subsession(
            db,
            parent_session_id=session_id,
            title=payload.get("title") or "后续任务",
            task=task or payload.get("title") or "继续推进",
            context=payload.get("rationale") or "",
            mode=_normalize_mode(payload.get("mode")),
        )
    except Exception as e:
        # 建子会话失败：释放占用，卡片可重试（刷新后仍是 pending）
        await _transition(suggestion_id, STATUS_ACCEPTING, STATUS_PENDING, "suggestion.release")
        if isinstance(e, subsession_service.SubsessionError):
            raise SuggestionError(str(e)) from e
        raise

    await _transition(suggestion_id, STATUS_ACCEPTING, STATUS_ACCEPTED, "suggestion.accept")
    # 子会话总是挂在根主会话下：返回根 id，前端据此刷新左栏对应分组
    root_id = await subsession_service.root_parent_of(db, session_id)
    return {"subsession_id": subsession_id, "session_id": root_id}


def _is_stale_accepting(row) -> bool:
    """占用是否已超时（进程中途退出留下的 accepting 残留）。"""
    if row.status != STATUS_ACCEPTING or not row.decided_at:
        return False
    try:
        at = datetime.fromisoformat(str(row.decided_at))
    except ValueError:
        return True
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - at).total_seconds() > ACCEPTING_STALE_S


async def _claim(suggestion_id: int) -> bool:
    """原子占用：pending（或超时残留的 accepting）→ accepting。"""
    from app.persistence.database import run_write_locked
    from app.persistence.models.suggestion import TaskSuggestion

    def _do(s) -> bool:
        r = s.get(TaskSuggestion, suggestion_id)
        if r is None or not (r.status == STATUS_PENDING or _is_stale_accepting(r)):
            return False
        r.status = STATUS_ACCEPTING
        r.decided_at = _now()
        s.commit()
        return True

    return bool(await run_write_locked(_do, label="suggestion.claim"))


async def _transition(suggestion_id: int, expected: str, target: str, label: str) -> bool:
    """条件状态迁移：当前状态为 expected 才改为 target，返回是否成功（单写线程内比较并设置）。"""
    from app.persistence.database import run_write_locked
    from app.persistence.models.suggestion import TaskSuggestion

    def _do(s) -> bool:
        r = s.get(TaskSuggestion, suggestion_id)
        if r is None or r.status != expected:
            return False
        r.status = target
        r.decided_at = _now()
        s.commit()
        return True

    return bool(await run_write_locked(_do, label=label))
