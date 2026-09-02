"""会话路由（v2：CRUD/fork/rename/worktree；v30.1：压缩块索引/还原）。"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.gateway.schemas import (
    CompactionIndexOut, GoalOut, GoalSetBody, MessageOut, SessionCreate, SessionOut, SessionUpdate,
)
from app.orchestration.agent_events import broadcast
from app.persistence.database import get_db
from app.services import compression_service, session_service, worktree_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/sessions", tags=["sessions"])


async def _to_out(db: AsyncSession, s) -> SessionOut:
    return SessionOut(
        id=s.id, project_id=s.project_id, title=s.title, model_id=s.model_id,
        status=s.status, pinned=s.pinned, permission_mode=s.permission_mode,
        fork_parent_id=s.fork_parent_id,
        worktree_path=s.worktree_path,
        has_running=await session_service.has_running_turn(db, s.id),
        has_interrupted_turn=await session_service.has_interrupted_turn(db, s.id),
        last_activity_at=await session_service.last_activity_at(db, s.id),
        goal_text=s.goal_text,
        goal_status=s.goal_status or "none",
        goal_turns_used=s.goal_turns_used or 0,
    )


@router.post("", response_model=SessionOut)
async def create_session(body: SessionCreate, db: AsyncSession = Depends(get_db)):
    session = await session_service.create_session(
        db, project_id=body.project_id, title=body.title, model_id=body.model_id,
        permission_mode=body.permission_mode,
        goal_text=body.goal_text,
    )
    await db.commit()
    return await _to_out(db, session)


@router.get("", response_model=list[SessionOut])
async def list_sessions(project_id: int | None = None, include_archived: bool = False,
                        db: AsyncSession = Depends(get_db)):
    """会话列表；include_archived=true 时返回含归档会话（归档恢复面板用）。"""
    sessions = await session_service.list_sessions(db, project_id=project_id, include_archived=include_archived)
    return [await _to_out(db, s) for s in sessions]


@router.get("/{session_id}", response_model=SessionOut)
async def get_session(session_id: int, db: AsyncSession = Depends(get_db)):
    session = await session_service.get_session(db, session_id)
    if session is None:
        raise HTTPException(404, "会话不存在")
    return await _to_out(db, session)


@router.patch("/{session_id}", response_model=SessionOut)
async def update_session(session_id: int, body: SessionUpdate, db: AsyncSession = Depends(get_db)):
    # 先取旧值：模型切换 divider 仅在模型真正发生变化时写入，
    # 否则重复 PATCH（同一模型/自动绑定）会在消息流顶部刷出多条「模型已切换」。
    old_model_id: int | None = None
    if body.model_id is not None:
        old = await session_service.get_session(db, session_id)
        old_model_id = old.model_id if old else None
    session = await session_service.update_session(
        db, session_id,
        title=body.title, model_id=body.model_id, pinned=body.pinned, status=body.status,
        permission_mode=body.permission_mode,
    )
    if session is None:
        raise HTTPException(404, "会话不存在")
    # v2.2 (对齐 zcode 3.11): 模型切换 divider——换模型时写一条 SYSTEM 分割消息
    if body.model_id is not None and old_model_id is not None and old_model_id != body.model_id:
        try:
            from sqlalchemy import select

            from app.persistence.models.model_reg import Model
            _m = await db.get(Model, body.model_id)
            _model_name = _m.name if _m else f"#{body.model_id}"
            await session_service.create_system_message(
                db, session_id=session_id,
                content={"text": f"模型已切换为 {_model_name}", "divider": "model_changed"},
            )
        except Exception:
            logger.warning("[sessions] 模型切换消息写入失败(非阻塞)", exc_info=True)
    await db.commit()
    return await _to_out(db, session)


@router.delete("/{session_id}", response_model=dict)
async def delete_session(session_id: int, db: AsyncSession = Depends(get_db)):
    session = await session_service.update_session(db, session_id, status="archived")
    if session is None:
        raise HTTPException(404, "会话不存在")
    await db.commit()
    return {"ok": True}


@router.post("/{session_id}/fork", response_model=SessionOut)
async def fork_session(session_id: int, db: AsyncSession = Depends(get_db)):
    try:
        session = await session_service.fork_session(db, session_id)
        await db.commit()
        return await _to_out(db, session)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/{session_id}/rename", response_model=SessionOut)
async def rename_session(session_id: int, title: str, db: AsyncSession = Depends(get_db)):
    session = await session_service.update_session(db, session_id, title=title)
    if session is None:
        raise HTTPException(404, "会话不存在")
    await db.commit()
    return await _to_out(db, session)


@router.post("/{session_id}/worktree", response_model=dict)
async def create_worktree(session_id: int, branch: str | None = None,
                          db: AsyncSession = Depends(get_db)):
    try:
        result = await worktree_service.create_worktree(db, session_id, branch=branch)
        await db.commit()
        return result
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.delete("/{session_id}/worktree", response_model=dict)
async def remove_worktree(session_id: int, db: AsyncSession = Depends(get_db)):
    try:
        result = await worktree_service.remove_worktree(db, session_id)
        await db.commit()
        return result
    except ValueError as e:
        raise HTTPException(400, str(e))


# ── 目标模式（plan-671，对齐 zcode goal-continuation）──

def _goal_out(s) -> GoalOut:
    from app.core.config import settings
    return GoalOut(
        text=s.goal_text,
        status=s.goal_status or "none",
        turns_used=s.goal_turns_used or 0,
        max_turns=settings.goal_max_continuation_turns,
        created_at=s.goal_created_at,
    )


@router.get("/{session_id}/goal", response_model=GoalOut)
async def get_goal(session_id: int, db: AsyncSession = Depends(get_db)):
    session = await session_service.get_session(db, session_id)
    if session is None:
        raise HTTPException(404, "会话不存在")
    return _goal_out(session)


@router.post("/{session_id}/goal", response_model=GoalOut)
async def set_goal(session_id: int, body: GoalSetBody, db: AsyncSession = Depends(get_db)):
    """设定/替换目标：覆盖时旧目标自然失效（新状态直接覆盖）。"""
    from datetime import datetime, timezone

    text = body.text.strip()
    if not text:
        raise HTTPException(400, "目标文本不能为空")
    session = await session_service.get_session(db, session_id)
    if session is None:
        raise HTTPException(404, "会话不存在")
    session.goal_text = text[:2000]
    session.goal_status = "active"
    session.goal_turns_used = 0
    session.goal_created_at = datetime.now(timezone.utc).isoformat()
    await db.commit()
    await broadcast(session_id, {
        "event": "goal.updated",
        "payload": {"text": session.goal_text, "status": "active", "turns_used": 0},
    })
    return _goal_out(session)


@router.delete("/{session_id}/goal", response_model=GoalOut)
async def cancel_goal(session_id: int, complete: bool = False, db: AsyncSession = Depends(get_db)):
    """取消目标；complete=true 时由用户确认完成（goal_complete 工具的同义用户路径）。"""
    session = await session_service.get_session(db, session_id)
    if session is None:
        raise HTTPException(404, "会话不存在")
    if session.goal_status == "active":
        session.goal_status = "completed" if complete else "cancelled"
        await db.commit()
        await broadcast(session_id, {
            "event": "goal.updated",
            "payload": {"text": session.goal_text, "status": session.goal_status,
                        "turns_used": session.goal_turns_used or 0},
        })
    return _goal_out(session)


# ── v30.1: 压缩块索引 / 原文还原 ──

@router.get("/{session_id}/compactions", response_model=list[CompactionIndexOut])
async def list_compactions(session_id: int, db: AsyncSession = Depends(get_db)):
    """会话内全部压缩块索引（按压缩发生顺序）。AI 可据此定位压缩前会话。"""
    return await compression_service.list_compaction_index(db, session_id)


@router.get("/{session_id}/compactions/{compaction_id}/messages", response_model=list[MessageOut])
async def get_compacted_messages(session_id: int, compaction_id: str,
                                 db: AsyncSession = Depends(get_db)):
    """按压缩块 id 取被压缩消息的完整原文（还原查看用）。"""
    try:
        msgs = await compression_service.get_compacted_messages(db, session_id, compaction_id)
    except KeyError as e:
        raise HTTPException(404, str(e))
    return [
        MessageOut(
            id=m.id, session_id=m.session_id, turn_id=m.turn_id, thread_id=m.thread_id,
            sender_type=m.sender_type, sender_id=m.sender_id, msg_type=m.msg_type,
            content=m.content or {}, token_usage=m.token_usage or 0,
            created_at=str(m.created_at) if m.created_at else None,
        )
        for m in msgs
    ]


@router.post("/{session_id}/compactions/{compaction_id}/restore", response_model=dict)
async def restore_compaction(session_id: int, compaction_id: str,
                             db: AsyncSession = Depends(get_db)):
    """还原压缩块：被压缩消息重新参与上下文构建。"""
    try:
        count = await compression_service.restore_compaction(db, session_id, compaction_id)
    except KeyError as e:
        raise HTTPException(404, str(e))
    return {"ok": True, "restored_messages": count}
