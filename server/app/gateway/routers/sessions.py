"""会话路由（v2：CRUD/fork/rename/worktree）。"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.gateway.schemas import SessionCreate, SessionOut, SessionUpdate
from app.persistence.database import get_db
from app.services import session_service, worktree_service

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
    )


@router.post("", response_model=SessionOut)
async def create_session(body: SessionCreate, db: AsyncSession = Depends(get_db)):
    session = await session_service.create_session(
        db, project_id=body.project_id, title=body.title, model_id=body.model_id,
    )
    await db.commit()
    return await _to_out(db, session)


@router.get("", response_model=list[SessionOut])
async def list_sessions(project_id: int | None = None, db: AsyncSession = Depends(get_db)):
    sessions = await session_service.list_sessions(db, project_id=project_id)
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
