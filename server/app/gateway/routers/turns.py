"""轮次（turn）路由：发消息、查询、取消、回滚、续跑。"""
import asyncio

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import MsgType, SenderType
from app.gateway.schemas import (MessageOut, RollbackResult, TaskOut, TurnCreate,
                                 TurnOut, TurnSnapshotOut)
from app.orchestration import engine
from app.persistence.database import get_db
from app.services import message_service, rollback_service, session_service, task_service, turn_service

router = APIRouter(prefix="/turns", tags=["turns"])


@router.post("", response_model=TurnOut)
async def create_turn(body: TurnCreate, db: AsyncSession = Depends(get_db)):
    """发送消息并启动一个 turn（异步执行，立即返回 turn）。"""
    session = await session_service.get_session(db, body.session_id)
    if session is None:
        raise HTTPException(404, "会话不存在")

    # 用户消息入库
    user_msg = await message_service.create_message(
        db, session_id=body.session_id,
        sender_type=SenderType.USER.value,
        msg_type=MsgType.TEXT.value,
        content={"text": body.content},
        broadcast=False,
    )
    turn = await turn_service.create_turn(db, session_id=body.session_id, user_message_id=user_msg.id)
    # 回填用户消息的 turn_id：保证前端按 turn 分组时用户消息归入该 turn，
    # 且（消息按 id 升序）用户消息排在 AI 回复之前（修复消息顺序颠倒）
    user_msg.turn_id = turn.id
    await db.commit()

    # 广播用户消息到前端（带 turn_id），实现即时显示
    try:
        from app.gateway.ws import manager as ws_manager
        from app.services.message_service import _to_out
        await ws_manager.broadcast(body.session_id, {
            "event": "message.created",
            "payload": {"msg": _to_out(user_msg).model_dump()},
        })
    except Exception:
        pass

    # 异步执行 turn（后台任务）
    async def _run():
        from app.persistence.database import async_session_factory
        async with async_session_factory() as s:
            try:
                await engine.start_turn(s, turn_id=turn.id, attachments=body.attachments, reasoning_effort=body.reasoning_effort)
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    asyncio.get_event_loop().create_task(_run())
    return turn


@router.get("/sessions/{session_id}", response_model=list[TurnOut])
async def list_turns(session_id: int, db: AsyncSession = Depends(get_db)):
    return await turn_service.list_turns(db, session_id)


@router.post("/{turn_id}/cancel", response_model=dict)
async def cancel_turn(turn_id: int, db: AsyncSession = Depends(get_db)):
    ok = await engine.cancel_turn(turn_id)
    if not ok:
        turn = await turn_service.get_turn(db, turn_id)
        if turn is None:
            raise HTTPException(404, "turn 不存在")
    return {"ok": ok}


@router.post("/{turn_id}/resume", response_model=TurnOut)
async def resume_turn(turn_id: int, db: AsyncSession = Depends(get_db)):
    """断点续跑：将 interrupted turn 重新置为 running 并启动。"""
    turn = await turn_service.get_turn(db, turn_id)
    if turn is None:
        raise HTTPException(404, "turn 不存在")
    if turn.status != "interrupted":
        raise HTTPException(400, "仅 interrupted 状态的 turn 可续跑")
    await turn_service.update_turn_status(db, turn_id, "running")
    await db.commit()

    async def _run():
        from app.persistence.database import async_session_factory
        async with async_session_factory() as s:
            try:
                await engine.start_turn(s, turn_id=turn_id)
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    asyncio.get_event_loop().create_task(_run())
    return turn


@router.post("/{turn_id}/rollback", response_model=RollbackResult)
async def rollback(turn_id: int, restore_to_composer: bool = True,
                   db: AsyncSession = Depends(get_db)):
    """回滚指定 turn（文件恢复 + 消息软删 + 可回填输入框）。"""
    try:
        result = await rollback_service.rollback_turn(
            db, turn_id=turn_id, restore_to_composer=restore_to_composer,
        )
        await db.commit()
        if not result.get("ok"):
            raise HTTPException(400, result.get("reason", "回滚失败"))
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/{turn_id}/snapshot", response_model=TurnSnapshotOut)
async def get_snapshot(turn_id: int, db: AsyncSession = Depends(get_db)):
    snap = await rollback_service._snapshot_for_turn(db, turn_id)
    if snap is None:
        raise HTTPException(404, "该 turn 无快照")
    return snap


# ── 会话数据查询（挂 turns 下便于前端一次获取）──

@router.get("/sessions/{session_id}/messages", response_model=list[MessageOut])
async def list_messages(session_id: int, db: AsyncSession = Depends(get_db)):
    msgs = await message_service.list_messages(db, session_id)
    return [
        MessageOut(
            id=m.id, session_id=m.session_id, turn_id=m.turn_id, thread_id=m.thread_id,
            sender_type=m.sender_type, sender_id=m.sender_id, msg_type=m.msg_type,
            content=m.content, token_usage=m.token_usage,
            created_at=str(m.created_at) if m.created_at else None,
        ) for m in msgs
    ]


@router.get("/sessions/{session_id}/usage")
async def get_session_usage(session_id: int, db: AsyncSession = Depends(get_db)):
    """返回当前会话的 token 占用估算 + context_window。

    前端 switchSession 后调用此接口，解决重启后 usage 为 null、占用显示 0% 的问题。
    仅统计主线程（thread_id IS NULL）消息，子代理线程不计入主会话占用。
    """
    from app.orchestration.token_counter import messages_token_total, get_agent_context_window
    from app.services import session_service
    from sqlalchemy import select
    from app.persistence.models.message import Message

    session = await session_service.get_session(db, session_id)
    if session is None:
        raise HTTPException(404, "会话不存在")

    # 仅主线程消息
    res = await db.execute(
        select(Message).where(
            Message.session_id == session_id,
            Message.deleted == False,  # noqa: E712
            Message.thread_id.is_(None),
        ).order_by(Message.id.asc())
    )
    # v6.5: 过滤 thinking 消息（思考块不发给AI，与 build_main_context 保持一致），
    # 使初始估算值与 API 真实 prompt_tokens 接近，避免进入会话显示 145k、
    # 发消息后骤降到 18.7k 的不一致问题。
    from app.core.enums import MsgType
    msgs = [m for m in res.scalars().all() if m.msg_type == MsgType.TEXT.value]
    total_tokens = messages_token_total(msgs)

    # 获取 context_window：优先 session.model_id，否则用默认
    context_window = 0
    if session.model_id:
        from app.persistence.models.model_reg import Model
        model = await db.get(Model, session.model_id)
        if model and model.context_window:
            context_window = model.context_window
    if context_window == 0:
        from app.core.config import settings
        context_window = settings.default_context_window

    return {
        "total": total_tokens,
        "context_window": context_window,
        "message_count": len(msgs),
        "input": total_tokens,
        "cached_input": 0,
        "output": 0,
        "reasoning_output": 0,
        "agent_name": "main",
    }


@router.get("/sessions/{session_id}/tasks", response_model=list[TaskOut])
async def list_tasks(session_id: int, db: AsyncSession = Depends(get_db)):
    return await task_service.list_tasks(db, session_id)


@router.get("/sessions/{session_id}/snapshots", response_model=list[TurnSnapshotOut])
async def list_snapshots(session_id: int, db: AsyncSession = Depends(get_db)):
    return await rollback_service.list_snapshots(db, session_id)


@router.get("/sessions/{session_id}/audit", response_model=list)
async def list_audit(session_id: int, db: AsyncSession = Depends(get_db)):
    from app.services import audit_service
    return await audit_service.list_logs(db, session_id)
