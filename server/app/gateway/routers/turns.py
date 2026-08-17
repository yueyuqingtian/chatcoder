"""轮次（turn）路由：发消息、查询、取消、回滚、续跑、变更审核。"""
import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import MsgType, SenderType
from app.gateway.schemas import (ArtifactOut, FileChangeOut, FileDiffOut, MessageOut, ReviewBatchBody,
                                 RollbackPreviewFile, RollbackPreviewOut,
                                 RollbackResult, TaskConfirmBody, TaskOut, TurnCreate, TurnOut, TurnSnapshotOut)
from app.orchestration import engine
from app.persistence.database import get_db
from app.services import message_service, rollback_service, session_service, task_service, turn_service

router = APIRouter(prefix="/turns", tags=["turns"])

logger = logging.getLogger(__name__)


@router.post("", response_model=TurnOut)
async def create_turn(body: TurnCreate, db: AsyncSession = Depends(get_db)):
    """发送消息并启动一个 turn（异步执行，立即返回 turn）。"""
    session = await session_service.get_session(db, body.session_id)
    if session is None:
        raise HTTPException(404, "会话不存在")

    # 用户消息入库（v14: 附件以文件地址形式持久化到 content.attachments，
    # 前端从历史消息可直接展示附件卡片并点击预览）
    user_content: dict = {"text": body.content}
    if body.attachments:
        user_content["attachments"] = body.attachments
    user_msg = await message_service.create_message(
        db, session_id=body.session_id,
        sender_type=SenderType.USER.value,
        msg_type=MsgType.TEXT.value,
        content=user_content,
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
                await engine.start_turn(s, turn_id=turn.id, attachments=body.attachments, reasoning_effort=body.reasoning_effort, mode=body.mode)
                await s.commit()
            except Exception:
                await s.rollback()
                # v1.1: 异常也必须关 turn——否则 DB 永远 running，左侧会话永远转圈
                logger.exception("turn 执行异常 turn=%s", turn.id)
                try:
                    await turn_service.update_turn_status(s, turn.id, "failed",
                                                          summary="执行异常", completed=True)
                    await s.commit()
                    from app.gateway.ws import manager as ws_manager
                    await ws_manager.broadcast(body.session_id, {
                        "event": "turn.updated",
                        "payload": {"turn_id": turn.id, "status": "failed"},
                    })
                except Exception:
                    logger.debug("turn 异常态落库失败", exc_info=True)

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
                # v1.1: 续跑异常同样必须关 turn（避免 DB 永远 running）
                logger.exception("turn 续跑执行异常 turn=%s", turn_id)
                try:
                    await turn_service.update_turn_status(s, turn_id, "failed",
                                                          summary="执行异常", completed=True)
                    await s.commit()
                    from app.gateway.ws import manager as ws_manager
                    await ws_manager.broadcast(turn.session_id, {
                        "event": "turn.updated",
                        "payload": {"turn_id": turn_id, "status": "failed"},
                    })
                except Exception:
                    logger.debug("turn 异常态落库失败", exc_info=True)

    asyncio.get_event_loop().create_task(_run())
    return turn


@router.post("/{turn_id}/rollback", response_model=RollbackResult)
async def rollback(turn_id: int, restore_to_composer: bool = True,
                   db: AsyncSession = Depends(get_db)):
    """回滚指定 turn（精确文件恢复 + 消息软删 + 可回填输入框）。"""
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


@router.get("/{turn_id}/rollback_preview", response_model=RollbackPreviewOut)
async def rollback_preview(turn_id: int, db: AsyncSession = Depends(get_db)):
    """回滚预览：计算本次回滚将撤销的文件及回滚前后内容（不修改任何文件）。

    前端确认弹窗据此展示文件清单与 diff 对比，供用户审核后确认执行。
    """
    try:
        snap = await rollback_service._snapshot_for_turn(db, turn_id)
        if snap is None:
            raise HTTPException(404, "该 turn 无快照")
        if snap.rolled_back:
            raise HTTPException(400, "该 turn 已回滚，不可重复操作")
        _, workspace = await rollback_service.resolve_turn_workspace(db, turn_id)
        writes = await rollback_service.list_turn_writes(db, snap.session_id, turn_id)
        if not writes or not workspace:
            files: list[RollbackPreviewFile] = []
        else:
            files = [RollbackPreviewFile(**f) for f in await rollback_service.preview_turn_files(workspace, writes)]
        # v12: 连带影响统计（该 turn 及其之后将被取消的任务/软删的消息）
        affected = await rollback_service.count_rollback_affected(db, snap.session_id, turn_id)
        return RollbackPreviewOut(ok=True, turn_id=turn_id, files=files,
                                  affected=affected)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


# ── 变更审核（v11）──

@router.get("/{turn_id}/changes", response_model=list[FileChangeOut])
async def list_turn_changes(turn_id: int, db: AsyncSession = Depends(get_db)):
    """变更审核清单：聚合该 turn 的写盘记录（含审核状态），不含文件全文。"""
    try:
        session_id, workspace = await rollback_service.resolve_turn_workspace(db, turn_id)
        if session_id is None or not workspace:
            logger.info("[review] changes 跳过(无快照/工作区): turn=%s", turn_id)
            return []
        changes = await rollback_service.list_turn_changes(
            db, session_id=session_id, turn_id=turn_id, workspace=workspace,
        )
        logger.info("[review] changes: turn=%s files=%s", turn_id, len(changes))
        return changes
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("[review] changes 失败: turn=%s err=%s", turn_id, e, exc_info=True)
        raise HTTPException(500, str(e))


@router.get("/{turn_id}/changes/diff", response_model=FileDiffOut)
async def get_change_diff(turn_id: int, path: str, db: AsyncSession = Depends(get_db)):
    """单文件变更 diff：按需拉取 before/after（大文件截断），供右侧面板 DiffEditor。"""
    try:
        session_id, workspace = await rollback_service.resolve_turn_workspace(db, turn_id)
        if session_id is None or not workspace:
            raise HTTPException(404, "该 turn 无快照")
        diff = await rollback_service.get_file_diff(
            db, session_id=session_id, turn_id=turn_id, workspace=workspace, path=path,
        )
        if diff is None:
            raise HTTPException(404, "该 turn 无此文件的写盘记录")
        logger.info("[review] diff: turn=%s file=%s truncated=%s",
                    turn_id, path, diff["truncated"])
        return diff
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("[review] diff 失败: turn=%s file=%s err=%s", turn_id, path, e, exc_info=True)
        raise HTTPException(500, str(e))


@router.put("/{turn_id}/reviews", response_model=dict)
async def set_reviews(turn_id: int, body: ReviewBatchBody, db: AsyncSession = Depends(get_db)):
    """批量审核（幂等 upsert）：{paths, reviewed} → {ok, updated}。"""
    try:
        updated = await rollback_service.upsert_file_reviews(
            db, turn_id=turn_id, paths=body.paths, reviewed=body.reviewed,
        )
        await db.commit()
        logger.info("[review] upsert: turn=%s paths=%s reviewed=%s updated=%s",
                    turn_id, len(body.paths), body.reviewed, updated)
        return {"ok": True, "updated": updated}
    except Exception as e:
        await db.rollback()
        logger.warning("[review] upsert 失败: turn=%s err=%s", turn_id, e, exc_info=True)
        raise HTTPException(500, str(e))


@router.get("/{turn_id}/snapshot", response_model=TurnSnapshotOut)
async def get_snapshot(turn_id: int, db: AsyncSession = Depends(get_db)):
    snap = await rollback_service._snapshot_for_turn(db, turn_id)
    if snap is None:
        raise HTTPException(404, "该 turn 无快照")
    return snap


# ── 会话数据查询（挂 turns 下便于前端一次获取）──

@router.get("/sessions/{session_id}/messages", response_model=list[MessageOut])
async def list_messages(session_id: int, thread_id: int | None = None,
                        db: AsyncSession = Depends(get_db)):
    # v2.2 (对齐 zcode 3.13): thread_id 过滤——子代理详情面板数据源
    # v19: 主消息流（thread_id 缺省）仅返回主线程消息，子代理线程消息不再混入主时间线
    if thread_id is None:
        from app.persistence.models.message import Message
        res = await db.execute(
            select(Message).where(
                Message.session_id == session_id,
                Message.thread_id.is_(None),
                Message.deleted == False,  # noqa: E712
            ).order_by(Message.id.asc())
        )
        msgs = list(res.scalars().all())
    else:
        msgs = await message_service.list_messages(db, session_id, thread_id=thread_id)
    return [
        MessageOut(
            id=m.id, session_id=m.session_id, turn_id=m.turn_id, thread_id=m.thread_id,
            sender_type=m.sender_type, sender_id=m.sender_id, msg_type=m.msg_type,
            content=m.content, token_usage=m.token_usage,
            created_at=str(m.created_at) if m.created_at else None,
        ) for m in msgs
    ]


@router.get("/sessions/{session_id}/subagents", response_model=list[dict])
async def list_session_subagents(session_id: int, turn_id: int | None = None,
                                 db: AsyncSession = Depends(get_db)):
    """v19: 会话子代理列表（kind=sub），左联 Task 取状态——前端消息流子代理卡片重建数据源。"""
    from app.persistence.models.agent import Agent
    from app.persistence.models.task import Task
    stmt = select(Agent, Task).outerjoin(
        Task, Task.agent_id == Agent.id
    ).where(Agent.session_id == session_id, Agent.kind == "sub")
    if turn_id is not None:
        stmt = stmt.where(Agent.turn_id == turn_id)
    stmt = stmt.order_by(Agent.id.asc())
    res = await db.execute(stmt)
    out: list[dict] = []
    for agent, task in res.all():
        out.append({
            "agent_id": agent.id,
            "name": agent.name or f"子代理 #{agent.id}",
            "turn_id": agent.turn_id,
            "task_id": task.id if task else None,
            "task_title": task.title if task else None,
            "status": (task.status if task else None) or "running",
        })
    return out


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
    # v9: 与 build_main_context 的 _keep_types 对齐，统计 text + tool_call + tool_result。
    # 旧版仅统计 TEXT，重启后 usage 严重低估（如运行时 120k、重启后只剩 20k），
    # 因为工具调用与结果是会话占用的大头。
    from app.core.enums import MsgType
    _keep = {MsgType.TEXT.value, MsgType.TOOL_CALL.value, MsgType.TOOL_RESULT.value}
    msgs = [m for m in res.scalars().all() if m.msg_type in _keep]

    # v1.1: 优先用最后一次 API 真实占用（与运行时圆环同口径），无则本地估算兜底
    if getattr(session, "last_prompt_tokens", 0) and session.last_prompt_tokens > 0:
        total_tokens = session.last_prompt_tokens
        _source = "api_last"
    else:
        total_tokens = messages_token_total(msgs)
        _source = "est"

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
        "source": _source,  # v1.1: api_last=最后一次 API 真实占用 / est=本地估算
    }


@router.get("/sessions/{session_id}/tasks", response_model=list[TaskOut])
async def list_tasks(session_id: int, db: AsyncSession = Depends(get_db)):
    return await task_service.list_tasks(db, session_id)


@router.post("/{turn_id}/tasks/{task_id}/retry", response_model=dict)
async def retry_task_step(turn_id: int, task_id: int, db: AsyncSession = Depends(get_db)):
    """重试失败/已取消的步骤：后台重新执行该步的子代理。"""
    from app.persistence.models.task import Task
    step = await db.get(Task, task_id)
    if step is None or step.turn_id != turn_id or step.kind != "step":
        raise HTTPException(404, "步骤不存在")
    if step.status not in ("failed", "cancelled"):
        raise HTTPException(409, "仅失败或已取消的步骤可重试")

    async def _run_retry():
        from app.persistence.database import async_session_factory
        async with async_session_factory() as session_db:
            try:
                await engine.retry_failed_step(session_db, turn_id=turn_id, task_id=task_id)
            except Exception:
                await session_db.rollback()
                logger.exception("步骤重试失败 turn=%s task=%s", turn_id, task_id)
                # v1.1: 重试异常兜底：把 step 标 failed 并广播 task.updated，避免永久 running
                try:
                    from app.gateway.ws import manager as ws_manager
                    step_row = await session_db.get(Task, task_id)
                    if step_row is not None:
                        step_row.status = "failed"
                        step_row.note = "执行异常"
                        await session_db.commit()
                        await ws_manager.broadcast(step_row.session_id, {
                            "event": "task.updated",
                            "payload": {"task_id": task_id, "status": "failed", "note": "执行异常"},
                        })
                except Exception:
                    logger.debug("步骤异常态落库失败", exc_info=True)

    asyncio.get_event_loop().create_task(_run_retry())
    return {"ok": True}


@router.post("/{turn_id}/tasks/{group_id}/confirm", response_model=dict)
async def confirm_task_plan(turn_id: int, group_id: int, body: TaskConfirmBody,
                            db: AsyncSession = Depends(get_db)):
    """确认/拒绝任务拆分提案；确认后在后台启动真实执行。"""
    from app.persistence.models.task import Task
    from sqlalchemy import select

    turn = await turn_service.get_turn(db, turn_id)
    group = await db.get(Task, group_id)
    if turn is None or group is None or group.turn_id != turn_id or group.kind != "group":
        raise HTTPException(404, "任务拆分提案不存在")
    if turn.status != "awaiting_confirmation" or group.status != "proposed":
        raise HTTPException(409, "该任务提案已处理或不在待确认状态")

    steps = list((await db.execute(
        select(Task).where(Task.parent_task_id == group.id).order_by(Task.priority.asc(), Task.id.asc())
    )).scalars().all())
    request_task = await db.get(Task, group.parent_task_id) if group.parent_task_id else None
    if request_task is None:
        raise HTTPException(400, "任务提案缺少请求任务")

    # 调整只允许编辑可见标题和顺序；隐藏字段仍由后端保留。
    if body.steps is not None:
        submitted = [item for item in body.steps if item.title.strip()]
        if not submitted:
            raise HTTPException(400, "至少保留一个任务步骤")
        by_id = {step.id: step for step in steps}
        used: set[int] = set()
        for index, item in enumerate(submitted[:12]):
            target = by_id.get(item.task_id) if item.task_id is not None else None
            if target is None:
                target = next((step for step in steps if step.id not in used), None)
            if target is None:
                # 调整新增的小点只继承区块的真实执行上下文，不接收用户提交的隐藏字段。
                target = Task(
                    session_id=turn.session_id, turn_id=turn_id, parent_task_id=group.id,
                    kind="step", status="proposed", priority=index,
                    title=item.title.strip()[:200], description=item.title.strip()[:1000],
                )
                db.add(target)
                await db.flush()
            target.title = item.title.strip()[:200]
            target.priority = index
            target.status = "proposed"
            target.is_hidden = False
            used.add(target.id)
        for step in steps:
            if step.id not in used:
                step.is_hidden = True
                step.status = "cancelled"

    if not body.accepted:
        group.status = "cancelled"
        group.is_hidden = True
        for step in steps:
            step.status = "cancelled"
            step.is_hidden = True
        request_task.status = "running"
        await turn_service.update_turn_status(db, turn_id, "running")
        await db.commit()

        async def _run_direct():
            from app.persistence.database import async_session_factory
            async with async_session_factory() as session_db:
                try:
                    await engine.start_turn(
                        session_db, turn_id=turn_id, existing_task_id=request_task.id,
                        force_direct=True,
                    )
                    await session_db.commit()
                except Exception:
                    await session_db.rollback()
                    logger.exception("直接执行提案失败 turn=%s", turn_id)

        asyncio.get_event_loop().create_task(_run_direct())
        return {"ok": True, "mode": "direct"}

    group.status = "pending"
    group.is_hidden = False
    for step in steps:
        if not step.is_hidden:
            step.status = "pending"
    request_task.status = "running"
    await turn_service.update_turn_status(db, turn_id, "running")
    await db.commit()

    async def _run_plan():
        from app.persistence.database import async_session_factory
        async with async_session_factory() as session_db:
            try:
                # v20: 拆分确认后走"探索并行 + 主代理串行"编排
                await engine.execute_split_then_main(session_db, turn_id=turn_id, group_id=group_id)
            except Exception:
                await session_db.rollback()
                logger.exception("确认任务执行失败 turn=%s group=%s", turn_id, group_id)

    asyncio.get_event_loop().create_task(_run_plan())
    return {"ok": True, "mode": "split"}


@router.get("/sessions/{session_id}/artifacts", response_model=list[ArtifactOut])
async def list_session_artifacts(session_id: int, db: AsyncSession = Depends(get_db)):
    """产物聚合（v12）：Artifact 表全量（含 title/summary/files），前端按任务分组展示。"""
    return await task_service.list_artifacts(db, session_id)


@router.get("/sessions/{session_id}/snapshots", response_model=list[TurnSnapshotOut])
async def list_snapshots(session_id: int, db: AsyncSession = Depends(get_db)):
    return await rollback_service.list_snapshots(db, session_id)


@router.get("/sessions/{session_id}/audit", response_model=list)
async def list_audit(session_id: int, db: AsyncSession = Depends(get_db)):
    from app.services import audit_service
    return await audit_service.list_logs(db, session_id)
