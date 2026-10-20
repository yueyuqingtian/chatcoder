"""会话路由（v2：CRUD/fork/rename/worktree；v30.1：压缩块索引/还原）。"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.gateway.schemas import (
    CompactionIndexOut, GoalOut, GoalSetBody, MessageOut, SessionCreate, SessionOut, SessionUpdate,
    SubsessionCreate,
)
from app.orchestration.agent_events import broadcast
from app.orchestration.workflow import store as workflow_store
from app.persistence.database import get_db
from app.services import compression_service, message_service, session_service, worktree_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/sessions", tags=["sessions"])


async def _to_out(db: AsyncSession, s, flags: "session_service.SessionFlags | None" = None) -> SessionOut:
    """会话 → 输出模型。列表场景传入批量算好的 flags（plan-156-620：避免逐会话 5 条查询）。"""
    # plan-75-332: 模式值统一走内核归一化——存量会话可能是旧值
    # （permission_mode=default/accept_edits），直接丢给前端会让模式菜单选不中任何一项。
    from app.orchestration.approval_policy import (
        normalize_approval_mode, normalize_execution_mode,
    )

    if flags is None:
        flags = session_service.SessionFlags(
            has_running=await session_service.has_running_turn(db, s.id),
            workflow_running=await session_service.has_running_workflow(db, s.id),
            has_interrupted_turn=await session_service.has_interrupted_turn(db, s.id),
            last_activity_at=await session_service.last_activity_at(db, s.id),
            running_started_at=await session_service.running_turn_started_at(db, s.id),
        )
    return SessionOut(
        id=s.id, project_id=s.project_id, title=s.title, model_id=s.model_id,
        status=s.status, pinned=s.pinned,
        permission_mode=normalize_execution_mode(s.permission_mode),
        approval_mode=normalize_approval_mode(getattr(s, "approval_mode", None)),
        pinned_at=getattr(s, "pinned_at", None),
        # plan-20261009-ui U2：首页用量热力图按「创建日」统计会话数
        created_at=str(s.created_at) if getattr(s, "created_at", None) else None,
        fork_parent_id=s.fork_parent_id,
        side_parent_id=getattr(s, "side_parent_id", None),
        side_kind=getattr(s, "side_kind", None),
        worktree_path=s.worktree_path,
        has_running=flags.has_running,
        workflow_running=flags.workflow_running,
        has_interrupted_turn=flags.has_interrupted_turn,
        last_activity_at=flags.last_activity_at,
        running_started_at=flags.running_started_at,
        goal_text=s.goal_text,
        goal_status=s.goal_status or "none",
        goal_turns_used=s.goal_turns_used or 0,
    )


async def _list_out(db: AsyncSession, rows: list) -> list[SessionOut]:
    """批量输出：运行态字段一次聚合查询算完。"""
    flags = await session_service.session_flags_batch(db, rows)
    return [await _to_out(db, r, flags.get(r.id)) for r in rows]


@router.post("", response_model=SessionOut)
async def create_session(body: SessionCreate, db: AsyncSession = Depends(get_db)):
    sid = await session_service.create_session(
        db, project_id=body.project_id, title=body.title, model_id=body.model_id,
        permission_mode=body.permission_mode,
        approval_mode=body.approval_mode,
        goal_text=body.goal_text,
    )
    session = await session_service.get_session(db, sid)  # async 只读
    return await _to_out(db, session)


@router.get("", response_model=list[SessionOut])
async def list_sessions(project_id: int | None = None, include_archived: bool = False,
                        db: AsyncSession = Depends(get_db)):
    """会话列表；include_archived=true 时返回含归档会话（归档恢复面板用）。"""
    sessions = await session_service.list_sessions(db, project_id=project_id, include_archived=include_archived)
    # 工作流状态按会话批量查询一次（侧栏标记），不逐个会话查询
    states = await workflow_store.session_workflow_states([s.id for s in sessions])
    out = await _list_out(db, sessions)
    for item in out:
        item.workflow_state = states.get(item.id)
    return out


@router.get("/subsession-index", response_model=list[SessionOut])
async def list_subsession_index(project_id: int | None = None, db: AsyncSession = Depends(get_db)):
    """全部未归档执行子会话（左栏一次性重建子会话行，前端按 side_parent_id 分桶）。

    plan-156-620：替代「每个主会话各请求一次 /subsessions」——上百个并发请求
    曾把后端请求队列塞满，进入会话时卡片与其它接口都明显变慢。
    必须声明在 `/{session_id}` 之前，否则会被当成会话 id 解析。
    """
    from app.services import subsession_service

    children = await subsession_service.list_all_children(db, project_id=project_id)
    return await _list_out(db, children)


@router.post("/{session_id}/side-sessions", response_model=SessionOut)
async def create_side_session(session_id: int, db: AsyncSession = Depends(get_db)):
    """从主会话开一个侧边会话（子会话）。

    子会话沿用主会话的项目、模型、执行/权限模式与工作树，之后的 turn 与主会话走同一条流水线，
    因此工具调用与消息展示完全一致。

    plan-474-2105：**子会话不能再创建任何同级 / 下级会话**——
    旧实现（plan-156-619 S5）允许子会话派生同级，导致一个任务可以无限套娃并绕开并发上限；
    需求 3 明确禁止。这里按调用方身份直接拒绝（工具层已剥离 create_subsession，这是 REST 入口的防线）。
    """
    parent = await session_service.get_session(db, session_id)
    if parent is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    if parent.side_parent_id is not None:
        raise HTTPException(
            status_code=400,
            detail="子会话不能再创建会话：需要更多执行力时，把需求写进汇报交给主代理",
        )
    if parent.project_id is None:
        raise HTTPException(status_code=400, detail="该会话没有所属项目，无法开启侧边会话")

    from app.core.enums import SideKind

    # plan-156-620：侧边对话与执行子会话是两类会话，容量门禁只约束执行子会话
    root = parent.side_parent_id or parent.id

    sid = await session_service.create_session(
        db, project_id=parent.project_id, title="侧边会话", model_id=parent.model_id,
        permission_mode=parent.permission_mode, approval_mode=parent.approval_mode,
        side_parent_id=root,
        side_kind=SideKind.SIDE.value,
        # plan-156-619 S8：侧边会话标题是固定占位，标 named 不参与自动归纳
        title_source="named",
    )
    if parent.worktree_path:
        await session_service.update_session(db, sid, worktree_path=parent.worktree_path)
    child = await session_service.get_session(db, sid)
    return await _to_out(db, child)


@router.post("/{session_id}/subsessions", response_model=SessionOut)
async def create_subsession(
    session_id: int, body: SubsessionCreate, db: AsyncSession = Depends(get_db),
):
    """创建一个执行子会话并立即下发首条任务（plan-156-619 S5）。

    与 `/side-sessions` 的区别：这里带 `task` / `context` / `mode`，创建即开工，
    供前端「子会话」入口与建议卡「在新子会话执行」使用；
    `/side-sessions` 只建空会话，供侧边对话面板使用。
    """
    from app.services import subsession_service

    try:
        sid = await subsession_service.create_subsession(
            db,
            parent_session_id=session_id,
            title=body.title,
            task=body.task,
            context=body.context or "",
            mode=body.mode,
        )
    except subsession_service.SubsessionError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    child = await session_service.get_session(db, sid)
    return await _to_out(db, child)


@router.get("/{session_id}/subsessions", response_model=list[SessionOut])
async def list_subsessions(session_id: int, db: AsyncSession = Depends(get_db)):
    """某主会话下的全部子会话（含运行态字段）。

    左栏子会话行的**唯一**数据源：`list_sessions` 会过滤掉子会话，
    因此刷新后必须由本接口重建；WS 事件只做增量。
    """
    if await session_service.get_session(db, session_id) is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    children = await subsession_service.list_children(db, session_id)
    return await _list_out(db, children)



@router.get("/{session_id}/side-sessions", response_model=list[SessionOut])
async def list_side_sessions(session_id: int, db: AsyncSession = Depends(get_db)):
    """主会话下的侧边会话列表（按创建先后）。"""
    if await session_service.get_session(db, session_id) is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    children = await session_service.list_side_sessions(db, session_id)
    return await _list_out(db, children)


@router.get("/{session_id}", response_model=SessionOut)
async def get_session(session_id: int, db: AsyncSession = Depends(get_db)):
    session = await session_service.get_session(db, session_id)
    if session is None:
        raise HTTPException(404, "会话不存在")
    return await _to_out(db, session)


@router.patch("/{session_id}", response_model=SessionOut)
async def update_session(session_id: int, body: SessionUpdate, db: AsyncSession = Depends(get_db)):
    # plan-75-332: 模式值走内核归一化，旧值（default/accept_edits）写入也统一成新取值
    from app.orchestration.approval_policy import (
        normalize_approval_mode, normalize_execution_mode,
    )

    result_status = await session_service.update_session(
        db, session_id,
        title=body.title, model_id=body.model_id, pinned=body.pinned, status=body.status,
        permission_mode=normalize_execution_mode(body.permission_mode) if body.permission_mode else None,
        approval_mode=normalize_approval_mode(body.approval_mode) if body.approval_mode else None,
    )
    if result_status is None:
        raise HTTPException(404, "会话不存在")
    if body.status == "archived":
        from app.orchestration.workflow import service as workflow_service

        await workflow_service.on_session_archived(session_id)
    session = await session_service.get_session(db, session_id)
    # plan-20261017-ux-overhaul 阶段 D：模型切换分割线改在用户发送时写入（见 turns._write_model_divider_if_changed），
    # 切换当下不再落库——只切不发不留痕迹，连续切换也只在发送时比较一次。
    return await _to_out(db, session)


@router.delete("/{session_id}", response_model=dict)
async def delete_session(session_id: int, permanent: bool = False,
                         db: AsyncSession = Depends(get_db)):
    """删除会话。默认语义是**归档**（可恢复）；`permanent=true` 为物理删除。

    plan-282-1441（#4）：归档页的「批量删除」需要真正的永久删除——
    旧接口只有归档语义，"删除"了仍能在归档页看到，用户无法真正清理。
    永久删除会级联清理该会话的 messages/turns/tasks/agents/audit/rollback/会话记忆。
    """
    if permanent:
        from app.orchestration.workflow import service as workflow_service
        from app.services import subsession_service

        # 挂靠会话随主会话一并永久删除，不留下不可见的残留会话。
        # plan-156-621：list_side_sessions 现在只返回侧边对话（side），执行子会话必须另行取，
        # 否则 sub 子会话会变成用户既看不到、也删不掉的孤儿行。
        child_ids = [c.id for c in await session_service.list_side_sessions(db, session_id)]
        seen = set(child_ids)
        for child in await subsession_service.list_children(db, session_id):
            if child.id not in seen:
                child_ids.append(child.id)
                seen.add(child.id)
        # plan-20261020-bgtask：会话一删，界面上再没有入口能管理它名下的后台命令——先停掉，免得成为占端口的孤儿
        from app.orchestration.tools.bg_process import bg_process_registry

        for child_id in child_ids:
            await bg_process_registry.kill_session(child_id)
            await workflow_service.on_session_deleted(child_id)
            await session_service.delete_session_permanent(db, child_id)
        await bg_process_registry.kill_session(session_id)
        await workflow_service.on_session_deleted(session_id)
        result = await session_service.delete_session_permanent(db, session_id)
        if result is None:
            raise HTTPException(404, "会话不存在")
        return result
    status = await session_service.update_session(db, session_id, status="archived")
    if status is None:
        raise HTTPException(404, "会话不存在")
    from app.orchestration.workflow import service as workflow_service

    await workflow_service.on_session_archived(session_id)
    return {"ok": True}


@router.post("/{session_id}/stop", response_model=dict)
async def stop_session(session_id: int):
    """停止按钮：中断本会话的一切执行——运行中的主代理（连带子代理）、后台子代理、运行中的工作流（暂停，保留进度）。"""
    from app.orchestration import engine

    result = await engine.stop_session_execution(session_id)
    return {"ok": True, **result}


@router.get("/{session_id}/bg-processes", response_model=dict)
async def list_bg_processes(session_id: int):
    """会话名下仍在运行的后台命令（右上角「后台任务」栏的数据源；已结束的不再列出）。

    plan-20261020-bgtask：`elapsed` 为服务端算好的已运行秒数，前端以收到的时刻为基准累加，不依赖两端时钟一致。
    """
    from app.orchestration.tools.bg_process import bg_process_registry

    return {"items": bg_process_registry.list_session(session_id)}


@router.post("/{session_id}/bg-processes/{shell_id}/stop", response_model=dict)
async def stop_bg_process(session_id: int, shell_id: str):
    """用户在界面上停止一个后台命令：终止整棵进程树。

    进程退出后注册表广播 `bg.process.exited`（卡片上这一行随之消失），并经完成回报把
    「被用户停止」告知所属会话（空闲会话不会被叫醒，下一轮开始时带上这条说明）。
    """
    from app.orchestration.tools.bg_process import bg_process_registry

    # 只能停本会话名下的命令：shell_id 跨会话不可猜，但这里仍显式校验归属
    if bg_process_registry.owner_session(shell_id) != session_id:
        raise HTTPException(404, "后台命令不存在或已结束")
    info = await bg_process_registry.kill(shell_id, by="user")
    if info is None:
        raise HTTPException(404, "后台命令不存在或已结束")
    return {"ok": True, "stopped": bool(info.get("killed")), "returncode": info.get("returncode")}


@router.post("/{session_id}/fork", response_model=SessionOut)
async def fork_session(session_id: int, db: AsyncSession = Depends(get_db)):
    try:
        sid = await session_service.fork_session(db, session_id)
        session = await session_service.get_session(db, sid)
        return await _to_out(db, session)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/{session_id}/rename", response_model=SessionOut)
async def rename_session(session_id: int, title: str, db: AsyncSession = Depends(get_db)):
    # plan-156-619 S8：用户手动重命名 → 标记 title_source=manual，
    # 此后自动归纳不再覆盖（否则用户改的名字会被下一轮 AI 标题冲掉）。
    status = await session_service.update_session(
        db, session_id, title=title, title_source="manual",
    )
    if status is None:
        raise HTTPException(404, "会话不存在")
    session = await session_service.get_session(db, session_id)
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
    await session_service.patch_session(session_id,
                                        goal_text=text[:2000], goal_status="active",
                                        goal_turns_used=0,
                                        goal_created_at=datetime.now(timezone.utc).isoformat())
    await db.refresh(session)  # 强制从 DB 读最新（identity map 可能缓存旧值）
    await broadcast(session_id, {
        "event": "goal.updated",
        "payload": {"text": text[:2000], "status": "active", "turns_used": 0},
    })
    return _goal_out(session)


@router.delete("/{session_id}/goal", response_model=GoalOut)
async def cancel_goal(session_id: int, complete: bool = False, db: AsyncSession = Depends(get_db)):
    """取消目标；complete=true 时由用户确认完成（goal_complete 工具的同义用户路径）。"""
    session = await session_service.get_session(db, session_id)
    if session is None:
        raise HTTPException(404, "会话不存在")
    if session.goal_status == "active":
        await session_service.patch_session(session_id,
                                            goal_status="completed" if complete else "cancelled")
        await db.refresh(session)  # 强制从 DB 读最新（identity map 可能缓存旧值）
        await broadcast(session_id, {
            "event": "goal.updated",
            "payload": {"text": session.goal_text, "status": "completed" if complete else "cancelled",
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
            content=message_service.enrich_content_abs_path(m.content) or {}, token_usage=m.token_usage or 0,
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
