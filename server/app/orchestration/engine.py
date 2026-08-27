"""Turn 引擎（v2 总控）：一次用户消息 = 一个 turn。

流程：创建快照 → 构建主上下文 → 主代理 loop（可 spawn 子代理）→
收集结果 → turn 完成摘要 → 记忆提取（异步）→ 广播。
"""
import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.enums import MsgType, SenderType
from app.orchestration.agent_events import broadcast, broadcast_turn_updated
from app.orchestration.agent_loop import run_agent_loop
from app.orchestration.context_manager import build_main_context, build_subagent_context
from app.orchestration.subagent import cleanup, get_subagent_manager
from app.orchestration.subagent_tools import append_subagent_tools, load_subagent_type_states
from app.orchestration.tools.registry import tool_registry
from app.orchestration.task_planner import PlannedStep, decompose_request, evaluate_complexity
from app.services import audit_service, message_service, project_service, rollback_service, session_service, task_service, turn_service

logger = logging.getLogger(__name__)

# 会话级运行锁（SQLite 串行写）
_running_turns: set[int] = set()
_cancel_events: dict[int, asyncio.Event] = {}
_turn_managers: dict[int, object] = {}
_turn_tasks: dict[int, asyncio.Task] = {}

# 命令模式：只读审阅（/chat）工具白名单
_READONLY_TOOLS = [
    "fs_read", "fs_list", "fs_grep", "git_diff",
    "memory_search", "web_fetch", "web_search", "view_image",
    "read_attachment", "codebase_search",
]
# 规划模式（/plan）：只读 + 允许写计划文档 + 命令行
# v3.0 (plan-88): 追加 terminal_exec——计划模式 AI 可执行命令（只读命令免审批，
# 其余走审批卡；cwd 是否可越出工作区由 plan_mode_allow_outside_access 开关控制）。
_PLAN_TOOLS = _READONLY_TOOLS + ["fs_write", "terminal_exec"]

_MODE_HINTS = {
    "readonly": (
        "【审阅模式】当前处于只读审阅模式，你只能查看、检索、分析代码，"
        "严禁执行任何修改操作（禁止写入文件、运行命令、应用补丁）。"
        "请直接给出审阅意见、发现的问题与改进建议。"
    ),
    "plan": (
        "【规划模式】请先规划再执行：不要直接修改业务代码。"
        "先在项目根目录创建 ai/ 目录，在其中编写本计划文档 ai/chatcoder-plan-{session_id}-{turn_id}.md，"
        "必须严格使用这个文件名，不要自行改名或加序号/时间戳。"
        "包含目标、步骤拆解、涉及文件与验收标准。方案文档完成后系统会自动把方案步骤"
        "拆成可执行小点并生成待确认任务卡；不要执行计划中的业务修改动作。"
    ),
}

# v2.2 (plan-88): checkpoint GC 触发倒计时——每 checkpoint_gc_interval_turns 个
# turn 完成触发一次（当前工作区），治理 .chatcoder/checkpoints 目录膨胀。
_checkpoint_gc_countdown = 0


async def _maybe_run_checkpoint_gc(db: AsyncSession, workspace: str | None) -> None:
    """低频 checkpoint 垃圾回收（失败不阻塞 turn 收尾）。"""
    global _checkpoint_gc_countdown
    if not workspace:
        return
    _checkpoint_gc_countdown -= 1
    if _checkpoint_gc_countdown > 0:
        return
    _checkpoint_gc_countdown = settings.checkpoint_gc_interval_turns
    try:
        from app.services import checkpoint_gc as _gc
        await _gc.run_cleanup_for_workspace(db, workspace)
    except Exception:
        logger.debug("[engine] checkpoint GC 失败(非阻塞)", exc_info=True)


async def _maybe_restore_plan_mode(db: AsyncSession, session) -> bool:
    """v2.2 (plan-88): plan 会话确认执行后被切为 accept_edits，执行 turn 结束时恢复 plan。

    幂等：标记已清则跳过。成功恢复时广播 session.updated（前端输入框切回「计划模式」）。
    """
    if session is None:
        return False
    if not getattr(session, "plan_restore_after_turn", False):
        return False
    session.plan_restore_after_turn = False
    session.permission_mode = "plan"
    try:
        await db.flush()
        await db.commit()
        await broadcast(session.id, {
            "event": "session.updated",
            "payload": {"session_id": session.id, "permission_mode": "plan"},
        })
        logger.info("[engine] 会话 %s 执行结束，恢复 plan 模式", session.id)
        return True
    except Exception:
        logger.debug("[engine] 恢复 plan 模式失败(非阻塞)", exc_info=True)
        return False


async def start_turn(db: AsyncSession, *, turn_id: int,
                     attachments: list[dict] | None = None,
                     reasoning_effort: str | None = None,
                     mode: str | None = None,
                     existing_task_id: int | None = None,
                     force_direct: bool = False) -> dict:
    """执行一个 turn。turn 与用户消息已由路由创建。

    mode: readonly(只读审阅) / plan(先规划后执行) / None(默认)。
    """
    turn = await turn_service.get_turn(db, turn_id)
    if turn is None:
        return {"ok": False, "error": "turn not found"}
    session_id = turn.session_id

    if turn_id in _running_turns:
        return {"ok": False, "error": "turn already running"}
    _running_turns.add(turn_id)
    cancel_event = _cancel_events.setdefault(turn_id, asyncio.Event())
    # v7: 主 turn 对应的任务记录（任务摘要步骤），创建后跟踪状态与产物
    main_task = None

    try:
        session = await session_service.get_session(db, session_id)
        if session is None:
            return {"ok": False, "error": "session not found"}
        project = await project_service.get_project(db, session.project_id) if session.project_id else None
        if project is None:
            return {"ok": False, "error": "项目不存在，请先关联项目"}

        # v26: 新 turn 开始时，同一会话中更早的待确认提案（awaiting_confirmation 的
        # proposed group）视为过期作废——用户反复调整方案时旧提案不再可确认，
        # 前端旧方案卡片对应的确认请求会得到 409，避免"旧卡片一直可点"的混乱。
        # plan-95: 作废结果立即 commit 并广播——此前仅 flush、commit 在任务创建之后，
        # 前端收到 turn.started 即刻拉取 tasks 仍读到未提交的旧 proposed group，
        # 刚被清掉的计划卡瞬间复活（"不该展示时展示"的竞态根因）。
        try:
            from sqlalchemy import select as _select

            from app.persistence.models.task import Task as _Task
            from app.persistence.models.turn import Turn as _Turn
            stale_res = await db.execute(_select(_Task).where(
                _Task.session_id == session_id,
                _Task.kind == "group",
                _Task.status == "proposed",
                _Task.turn_id.is_not(None),
                _Task.turn_id < turn_id,
            ))
            stale_groups = list(stale_res.scalars().all())
            stale_turn_ids: set[int] = {g.turn_id for g in stale_groups if g.turn_id is not None}
            if stale_groups:
                group_ids = [g.id for g in stale_groups]
                for g in stale_groups:
                    g.status = "cancelled"
                    g.is_hidden = True
                step_res = await db.execute(_select(_Task).where(
                    _Task.parent_task_id.in_(group_ids),
                ))
                for st in step_res.scalars().all():
                    if st.status == "proposed":
                        st.status = "cancelled"
                        st.is_hidden = True
                logger.info("[engine] turn=%s 作废旧提案 groups=%s", turn_id, group_ids)
            # plan-95: 同步关闭旧 turn 的待确认态——前端 refreshTasks 以 turn 状态为
            # 展示守卫，旧 turn 不再是 awaiting_confirmation 后旧卡无法复活
            stale_turn_res = await db.execute(_select(_Turn).where(
                _Turn.session_id == session_id,
                _Turn.status == "awaiting_confirmation",
                _Turn.id < turn_id,
            ))
            stale_turns = list(stale_turn_res.scalars().all())
            for t in stale_turns:
                t.status = "cancelled"
                t.completed_at = t.completed_at or datetime.now(timezone.utc).isoformat()
                stale_turn_ids.add(t.id)
            if stale_groups or stale_turns:
                await db.commit()
                for stale_tid in sorted(stale_turn_ids):
                    try:
                        await broadcast_turn_updated(session_id, stale_tid, "cancelled")
                    except Exception:
                        logger.debug("[engine] 旧提案 turn 广播失败(非阻塞)", exc_info=True)
        except Exception:
            logger.debug("[engine] 旧提案作废失败(非阻塞)", exc_info=True)

        # 主代理
        from app.persistence.models.agent import Agent
        from sqlalchemy import select
        res = await db.execute(select(Agent).where(Agent.kind == "main").limit(1))
        main_agent = res.scalars().first()
        if main_agent is None:
            return {"ok": False, "error": "主代理未初始化"}

        # 用户消息原文（v14: 附件消息仅含文件时，用附件名拼装提示文本，
        # 避免「空消息」导致任务标题/复杂度评估失真）
        user_msg = await message_service.get_message(db, turn.user_message_id) if turn.user_message_id else None
        user_msg_content = (user_msg.content or {}) if user_msg else {}
        user_text = str(user_msg_content.get("text", "")) if user_msg else "(空消息)"
        if not user_text.strip():
            att_names = [
                str(a.get("filename") or "") for a in (attachments or [])
                if isinstance(a, dict) and a.get("filename")
            ]
            user_text = ("请查看以下上传的附件:\n" + "\n".join(f"- {n}" for n in att_names)) if att_names else "(空消息)"

        # 0. 回滚快照（§4.10）
        workspace = _resolve_workspace(session, project)
        # plan-95: 记录本 turn 开始时间——提案解析只认该时刻之后写出的计划文档
        turn_started_ts = time.time()
        if existing_task_id is None:
            try:
                await rollback_service.create_turn_snapshot(
                    db, session_id=session_id, turn_id=turn_id,
                    workspace=workspace, user_message_id=turn.user_message_id,
                )
            except Exception:
                logger.debug("创建回滚快照失败(非阻塞)", exc_info=True)

        await broadcast(session_id, {"event": "turn.started", "payload": {"turn_id": turn_id}})

        # v13: 复杂度由 LLM 语义判断，规则只处理极端输入；/plan 固定进入拆分。
        effective_model_id = session.model_id or main_agent.model_id
        from app.models.registry import get_model_registry
        from app.persistence.models.model_reg import Model
        selected_model = await db.get(Model, effective_model_id) if effective_model_id else None
        planner_provider, _planner_reason = await get_model_registry().get_provider_for_model(db, selected_model)
        verdict = None if existing_task_id is not None or force_direct or mode == "readonly" else await evaluate_complexity(
            planner_provider, user_text=user_text, mode=mode, workspace=workspace,
        )
        task_initial_status = "proposed" if verdict and verdict.decision == "split" else "running"

        # 请求级任务：确认后复用原 request；普通 turn 才创建新的 request。
        if existing_task_id is not None:
            main_task = await task_service.get_task(db, existing_task_id)
            if main_task is None or main_task.session_id != session_id or main_task.turn_id != turn_id:
                return {"ok": False, "error": "任务不存在或不属于当前 turn"}
            main_task.status = "running"
            main_task.is_hidden = False
            await db.flush()
        else:
            main_task = await task_service.create_task(
                db, session_id=session_id, turn_id=turn_id,
                title=(user_text.strip().replace("\n", " ") or "任务")[:60],
                description=user_text, kind="request", status=task_initial_status,
            )
        # v9: 立即提交，保证前端任务面板/右上角卡片实时拉取到新任务步骤
        # （此前仅 flush，turn 结束才 commit，前端 refreshTasks 查不到新任务，
        #   表现为"新任务开始后未展示步骤与执行情况"）
        try:
            await db.commit()
        except Exception:
            logger.debug("[engine] 任务创建提交失败(非阻塞)", exc_info=True)
        await broadcast(session_id, {
            "event": "task.updated",
            "payload": {"task_id": main_task.id, "status": task_initial_status},
        })

        # 普通复杂请求：拆分后直接执行（免确认）。拆分结果仅作右上角任务卡展示，
        # 不再进入 awaiting_confirmation 等待用户点确认。
        if existing_task_id is None and not force_direct and verdict and verdict.decision == "split" and mode != "plan":
            group, steps = await _create_task_proposal(
                db, session_id=session_id, turn_id=turn_id, request_task=main_task,
                provider=planner_provider, source_text=user_text,
                suggested_steps=verdict.suggested_steps, plan_mode=False,
            )
            # v20: 拆分后先并行探索、再主代理串行整合执行（不再直接并行执行步骤）。
            # 提案即刻生效：group/steps 置 pending，请求任务恢复 running。
            group.status = "pending"
            for step in steps:
                step.status = "pending"
            main_task.status = "running"
            await turn_service.update_turn_status(db, turn_id, "running")
            await db.commit()
            # execute_split_then_main 会重新登记 _running_turns / cancel_event，
            # 先释放本 turn 的占用，避免被误判为重复运行。
            _running_turns.discard(turn_id)
            return await execute_split_then_main(db, turn_id=turn_id, group_id=group.id)

        # 1. 主上下文
        # v15: 多模态模型时图片附件直接注入用户消息（AI 直接看图，不再走工具猜测路径）
        _is_multimodal = bool(getattr(selected_model, "is_multimodal", False)) if selected_model else False
        _type_states = await load_subagent_type_states(db)
        _allow_subagents = bool(_type_states.get("explore", True) or _type_states.get("general", True))
        bundle = await build_main_context(
            db, agent=main_agent, session=session, project=project, turn=turn,
            user_message=user_text, attachments=attachments,
            multimodal=_is_multimodal,
            enable_subagents=_allow_subagents,
        )

        # 命令模式：注入模式指令（/chat 只读、/plan 先规划后执行）
        if mode in _MODE_HINTS:
            hint = _MODE_HINTS[mode]
            if mode == "plan":
                # plan-95: 提示词含 {session_id}/{turn_id} 占位符——文档按 turn 唯一命名，
                # 同一会话多次规划不再互相覆盖（此前固定会话名导致解析到上一任务旧文档）
                hint = hint.format(session_id=session_id, turn_id=turn_id)
            bundle.instruction = (hint + "\n\n" + bundle.instruction).strip() if bundle.instruction else hint

        # 2. 工具 schemas（按模式过滤 + 子代理工具）
        if mode == "readonly":
            tool_schemas = tool_registry.all_schemas(_READONLY_TOOLS)
        elif mode == "plan":
            tool_schemas = tool_registry.all_schemas(_PLAN_TOOLS)
        else:
            tool_schemas = tool_registry.all_schemas()
            # v6: 主 turn 路径 MCP 注入（修复：导入的 MCP 工具未注册导致 AI 无法使用）
            # 对齐 agent_runtime 的 per-agent scope 模式，仅当全局 registry 无同名工具时注册。
            try:
                from app.services.skill_service import get_agent_mcp_servers
                from app.orchestration.tools.mcp_wrapper import build_mcp_tools_for_agent
                mcp_servers = await get_agent_mcp_servers(db, main_agent)
                if mcp_servers:
                    _mcp_tools = build_mcp_tools_for_agent(mcp_servers)
                    for mt in _mcp_tools:
                        # v10: 仅当全局 registry 无同名工具时才注册并追加到 schemas。
                        # tool_schemas 已由 all_schemas() 包含已注册工具，若无条件 append，
                        # 第二次 turn 会产生重复工具名，LLM 报 "Tool names must be unique" (HTTP 400)。
                        if not tool_registry.get(mt.name):
                            tool_schemas.append(mt.function_schema())
                            tool_registry.register(mt)
                    logger.info("[engine] turn=%s 注入 %d 个 MCP 工具", turn_id, len(_mcp_tools))
            except Exception:
                logger.warning("[engine] MCP 工具加载失败(非阻塞)", exc_info=True)
        # 子任务由 task_planner + execute_split_then_main 统一编排。
        # v20: 恢复把 spawn_subagent/collect_results 暴露给主代理——
        # 拆分后探索子代理并行调研，主代理用这两个工具 spawn 探索任务并拿回结论，再串行实现。
        # v22: 子代理类型开关前移——类型停用时不再暴露对应工具，避免模型反复尝试。
        # 子代理工具仅追加 schema（executor 不注册），agent_loop 特判执行。
        if mode not in ("readonly", "plan"):
            tool_schemas = append_subagent_tools(tool_schemas, _type_states)
        # 3. 运行主代理
        # v10/v13: 会话级模型覆盖；同一模型也用于规划评估与拆分。
        from app.orchestration.subagent import get_subagent_manager
        mgr = get_subagent_manager(session_id)
        _turn_managers[turn_id] = mgr
        out = await run_agent_loop(
            db, session_id=session_id, turn_id=turn_id,
            agent=main_agent, context_messages=bundle.to_messages(),
            tool_schemas=tool_schemas, workspace=workspace,
            cancel_event=cancel_event,
            reasoning_effort=reasoning_effort,
            task_id=main_task.id,
            model_id=effective_model_id,
            multimodal=_is_multimodal,
            subagent_context={
                "manager": mgr, "session": session, "project": project,
                "cancel_event": cancel_event,
                "main_task_id": main_task.id,
                "model_id": effective_model_id,
            },
        )

        # /plan：方案文档完成后自动生成拆分提案，确认前不执行方案中的业务修改。
        # v2.2 (plan-88): 仅当方案文档真实存在且 AI 正常返回（kind=message）时才生成
        # 提案；未生成文档 / AI 异常结束时 turn 置 failed 并提示，不再广播 task.proposed
        # ——前端计划卡（pendingSplit）由 task.proposed 驱动，因此不会出现"无文档也弹卡"。
        if mode == "plan" and settings.plan_mode_auto_split:
            _plan_path, _plan_source = _resolve_plan_doc(
                workspace, session_id, out.kind, turn_id=turn_id, since_ts=turn_started_ts,
            )
            if _plan_path is None or out.kind != "message":
                # v3.1 (plan-88): 失败原因落库——超时中断等模型侧失败时 out.error 已
                # 携带用户可读原因（如"模型响应因网关空闲超时中断"），带出来便于诊断，
                # 不再笼统提示"未能生成计划文档"。
                _plan_reason = (out.error or "").strip()
                if not _plan_reason:
                    _plan_reason = "模型未生成方案文档" if _plan_path is None else f"AI 异常结束(kind={out.kind})"
                await turn_service.update_turn_status(
                    db, turn_id, "failed",
                    summary=f"计划文档未生成：{_plan_reason[:120]}", completed=True,
                )
                try:
                    await task_service.update_task_status(db, main_task.id, "failed",
                                                          note=f"计划文档未生成: {_plan_reason[:120]}")
                    await broadcast(session_id, {"event": "task.updated",
                                                 "payload": {"task_id": main_task.id, "status": "failed"}})
                except Exception:
                    logger.debug("[engine] plan 失败任务状态更新失败(非阻塞)", exc_info=True)
                try:
                    await message_service.create_message(
                        db, session_id=session_id, turn_id=turn_id,
                        sender_type=SenderType.AGENT.value, sender_id=main_agent.id,
                        msg_type=MsgType.ERROR.value,
                        content={"text": f"未能生成计划文档，任务已终止。{_plan_reason} 请重试或检查执行过程后再次发送。",
                                 "agent_name": main_agent.name},
                    )
                except Exception:
                    logger.debug("[engine] plan 失败提示消息写入失败(非阻塞)", exc_info=True)
                await db.commit()
                await broadcast_turn_updated(session_id, turn_id, "failed")
                return {"ok": True, "failed": True, "reason": "plan document not generated"}

            plan_source = _plan_source or (out.text or user_text)
            group, steps = await _create_task_proposal(
                db, session_id=session_id, turn_id=turn_id, request_task=main_task,
                provider=planner_provider, source_text=plan_source,
                suggested_steps=None, plan_mode=True,
            )
            main_task.status = "proposed"
            await db.flush()
            await turn_service.update_turn_status(
                db, turn_id, "awaiting_confirmation",
                summary="方案已生成，等待确认任务步骤", completed=True,
            )
            await db.commit()
            # v26: 广播实际命中的计划文档路径（AI 可能写时间戳文件名），
            # 前端方案卡"查看完整计划"打开真实文件而非约定名。
            await _broadcast_task_proposed(
                session_id, turn_id, main_task, group, steps,
                reasons=["/plan 方案文档已自动转换为任务步骤"],
                plan_doc_path=str(_plan_path.relative_to(Path(workspace).resolve()).as_posix()),
            )
            await broadcast_turn_updated(session_id, turn_id, "awaiting_confirmation")
            return {"ok": True, "awaiting_confirmation": True, "task_id": main_task.id}

        # 4. turn 完成：取消是用户中断，不应伪装成失败；先落库再广播。
        summary = out.text or ""
        final_status = (
            "interrupted" if cancel_event.is_set() or out.kind in ("cancelled", "interrupted")
            else "completed" if out.kind == "message" else "failed"
        )
        await turn_service.update_turn_status(
            db, turn_id, final_status,
            summary=summary[:500] or ("用户中断" if final_status == "interrupted" else None), completed=True,
        )
        await db.commit()
        await broadcast_turn_updated(session_id, turn_id, final_status)
        await broadcast(session_id, {
            "event": "turn.completed" if final_status == "completed" else "turn.interrupted",
            "payload": {"turn_id": turn_id, "status": final_status, "summary": summary, "artifact_ids": out.artifact_ids},
        })
        # v7: 同步任务状态（步骤进度）并挂接产物
        _task_status = "cancelled" if final_status == "interrupted" else ("done" if out.kind == "message" else "failed")
        _task_note = (summary or "").strip()[:300] or None
        try:
            await task_service.update_task_status(db, main_task.id, _task_status, note=_task_note)
            await task_service.attach_artifacts(db, main_task.id, out.artifact_ids)
            await broadcast(session_id, {
                "event": "task.updated",
                "payload": {"task_id": main_task.id, "status": _task_status, "note": _task_note or ""},
            })
        except Exception:
            logger.debug("[engine] 任务状态更新失败(非阻塞)", exc_info=True)
        await audit_service.log(db, action="turn", session_id=session_id, turn_id=turn_id,
                                detail={"kind": out.kind, "tokens": 0})

        # 5. 记忆提取（异步，不阻塞；受设置中心「AI 主动生成记忆」开关控制）
        if out.kind == "message" and summary:
            await _spawn_memory_extract(db, session_id=session_id, turn_id=turn_id, text=summary)

        # v2.2 (plan-88): plan 会话确认执行后，执行 turn 结束恢复 plan 模式
        try:
            await _maybe_restore_plan_mode(db, session)
        except Exception:
            logger.debug("[engine] 恢复 plan 模式失败(非阻塞)", exc_info=True)

        return {"ok": True, "kind": out.kind, "summary": summary}
    except Exception as _exc:
        # v7: 主 turn 任务异常结束 → 标记 failed，避免任务摘要出现永久 running 的假象
        if main_task is not None:
            try:
                await task_service.update_task_status(db, main_task.id, "failed", note=f"执行异常: {str(_exc)[:200]}")
                await broadcast(session_id, {"event": "task.updated", "payload": {"task_id": main_task.id, "status": "failed"}})
            except Exception:
                logger.debug("[engine] 任务标记失败失败(非阻塞)", exc_info=True)
        raise
    finally:
        _running_turns.discard(turn_id)
        _cancel_events.pop(turn_id, None)
        _turn_managers.pop(turn_id, None)
        cleanup(session_id)
        # v1.1: 无条件广播 session.completed，驱动前端摘除左侧转圈（无论成败）
        try:
            await broadcast(session_id, {"event": "session.completed", "payload": {"session_id": session_id}})
        except Exception:
            pass
        # v2.2 (plan-88): 低频 checkpoint GC
        try:
            await _maybe_run_checkpoint_gc(db, locals().get("workspace"))
        except Exception:
            pass
        # v2.2 (plan-88): plan 模式恢复兜底（异常/中断路径同样恢复）
        try:
            await _maybe_restore_plan_mode(db, locals().get("session"))
        except Exception:
            pass


async def cancel_turn(turn_id: int) -> bool:
    """Signal a running turn and durably mark its unfinished work interrupted."""
    ev = _cancel_events.get(turn_id)
    if ev is not None:
        ev.set()
        manager = _turn_managers.get(turn_id)
        if manager is not None:
            await manager.cancel_all()

    from app.persistence.database import async_session_factory
    from app.persistence.models.agent import Agent
    from app.persistence.models.task import Task
    from sqlalchemy import select

    async with async_session_factory() as db:
        turn = await turn_service.get_turn(db, turn_id)
        if turn is None:
            return False
        active = turn.status not in ("completed", "failed", "cancelled", "interrupted", "rolled_back")
        if active:
            turn.status = "interrupted"
            turn.summary = turn.summary or "用户中断"
            turn.completed_at = turn.completed_at or datetime.now(timezone.utc).isoformat()
            await task_service.cancel_turn_tasks(db, turn.session_id, turn_id)
            agents = await db.execute(select(Agent).where(Agent.turn_id == turn_id, Agent.status == "running"))
            for agent in agents.scalars().all():
                agent.status = "terminated"
            await db.commit()
            await broadcast_turn_updated(turn.session_id, turn_id, "interrupted")
            await broadcast(turn.session_id, {"event": "turn.interrupted", "payload": {"turn_id": turn_id, "status": "interrupted", "summary": turn.summary or "用户中断"}})
            await broadcast(turn.session_id, {"event": "session.completed", "payload": {"session_id": turn.session_id}})
        main_task = _turn_tasks.pop(turn_id, None)
        if main_task is not None and main_task is not asyncio.current_task() and not main_task.done():
            main_task.cancel()
        return True


def _resolve_workspace(session, project) -> str:
    if session.worktree_path:
        return session.worktree_path
    return project.path if project else ""


def _find_plan_document(workspace: str, session_id: int | None = None,
                        turn_id: int | None = None, since_ts: float | None = None) -> Path | None:
    """定位 /plan 阶段约定的方案文件；只读不执行。返回实际文件路径。

    plan-95: 文档按 turn 唯一命名 ai/chatcoder-plan-<sid>-<tid>.md；传入 since_ts
    （本 turn 开始时间）后仅接受该时刻之后有更新的候选——同会话再次规划时，
    即使 AI 自行改名写新文档，也不会解析到上一任务的旧文档。
    since_ts 为 None 时保持旧行为（取最新已知方案，供执行阶段回读）。
    """
    if not workspace:
        return None
    root = Path(workspace).resolve()

    def _safe(path: Path) -> Path | None:
        try:
            resolved = path.resolve()
            if resolved.is_file() and (resolved == root or root in resolved.parents):
                return resolved
        except OSError:
            pass
        return None

    def _fresh(path: Path) -> bool:
        if since_ts is None:
            return True
        try:
            return path.stat().st_mtime >= since_ts
        except OSError:
            return False

    # 1. 本 turn 专属约定名（多次规划互不覆盖）
    if turn_id is not None:
        found = _safe(root / "ai" / f"chatcoder-plan-{session_id}-{turn_id}.md")
        if found is not None and _fresh(found):
            return found

    # 2. 本会话绑定名：仅当本轮确有更新时才可直接命中（防复用上一任务旧文档）
    if session_id is not None:
        found = _safe(root / "ai" / f"chatcoder-plan-{session_id}.md")
        if found is not None and _fresh(found):
            return found

    # 3. 扫描全部变体（chatcoder-plan*.md），过滤陈旧文档后按 mtime 取最新
    ai_dir = root / "ai"
    try:
        candidates = [
            p for p in ai_dir.iterdir()
            if p.is_file() and p.name.startswith("chatcoder-plan") and p.suffix.lower() == ".md"
            and _fresh(p)
        ]
        if candidates:
            return max(candidates, key=lambda p: p.stat().st_mtime)
    except OSError:
        pass

    # 4. 兼容旧通用名（同样受新鲜度约束）
    for name in ("chatcoder-plan.md", "plan.md"):
        found = _safe(root / "ai" / name)
        if found is not None and _fresh(found):
            return found
    return None


def _read_plan_document(workspace: str, session_id: int | None = None) -> str:
    """读取 /plan 阶段约定的方案文件；只读，不执行文件内容。

    返回 _find_plan_document 命中的最新文档全文（上限 24000 字符）。
    """
    path = _find_plan_document(workspace, session_id)
    if path is None:
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:24000]
    except OSError:
        return ""


def _resolve_plan_doc(workspace: str, session_id: int, out_kind: str,
                      turn_id: int | None = None,
                      since_ts: float | None = None) -> tuple[Path | None, str]:
    """v2.2 (plan-88): 解析 /plan 方案文档——命中且内容非空才视为有效。

    plan-95: 按 turn 解析（turn_id + since_ts），只认本轮写出的文档。
    返回 (path, source)；文档缺失或内容为空返回 (None, "")，
    由调用方判定 turn 失败（不生成提案，避免"无文档也弹计划卡"）。
    """
    path = _find_plan_document(workspace, session_id, turn_id=turn_id, since_ts=since_ts)
    if path is None:
        return None, ""
    try:
        source = path.read_text(encoding="utf-8", errors="replace")[:24000]
    except OSError:
        return None, ""
    if not source.strip():
        return None, ""
    return path, source


async def _create_task_proposal(db: AsyncSession, *, session_id: int, turn_id: int,
                                request_task, provider, source_text: str,
                                suggested_steps: list[str] | None,
                                plan_mode: bool):
    """创建 proposed group/step 记录；此函数不启动代理。"""
    planned = await decompose_request(
        provider, source_text=source_text,
        suggested_steps=suggested_steps, plan_mode=plan_mode,
    )
    group = await task_service.create_task(
        db, session_id=session_id, turn_id=turn_id,
        title="建议步骤", description="任务拆分区块", parent_task_id=request_task.id,
        kind="group", status="proposed", priority=0,
    )
    steps = []
    for index, item in enumerate(planned):
        step = await task_service.create_task(
            db, session_id=session_id, turn_id=turn_id,
            title=item.title, description=item.summary or None,
            acceptance_criteria=item.acceptance or None,
            parent_task_id=group.id, kind="step", status="proposed",
            depends_on=list(item.depends_on), estimate=item.estimate, priority=index,
        )
        steps.append(step)
    request_task.status = "proposed"
    await db.flush()
    return group, steps


async def _broadcast_task_proposed(session_id: int, turn_id: int, request_task, group, steps,
                                   reasons: list[str] | None = None,
                                   plan_doc_path: str | None = None) -> None:
    await broadcast(session_id, {
        "event": "task.proposed",
        "payload": {
            "turn_id": turn_id,
            "request_task_id": request_task.id,
            "group_task_id": group.id,
            "reasons": reasons or [],
            # v26: 实际命中的计划文档路径（AI 可能写时间戳文件名），前端据此打开查看
            "plan_doc_path": plan_doc_path,
            "steps": [
                {"task_id": step.id, "title": step.title, "status": step.status,
                 "kind": step.kind, "parent_task_id": step.parent_task_id,
                 "depends_on": step.depends_on or []}
                for step in steps
            ],
        },
    })


# 同层并行上限（对齐 ta3 MAX_RUNNING_SUB_AGENTS）
MAX_PARALLEL_STEPS = 3
# 从步骤标题/描述提取文件路径 token，用于并行冲突检测
_FILE_TOKEN_RE = re.compile(r"[\w\-./\\]+\.[A-Za-z0-9]{1,6}")


def _step_file_tokens(step) -> set[str]:
    text = f"{step.title or ''} {step.description or ''}"
    return {m.group(0).lower().replace("\\", "/") for m in _FILE_TOKEN_RE.finditer(text)}


def _layer_step_indexes(steps) -> list[list[int]]:
    """按 depends_on（步骤索引）拓扑分层；同层步骤互不依赖可并行。成环时剩余兜底为一层。"""
    layers: list[list[int]] = []
    placed: set[int] = set()
    remaining = set(range(len(steps)))
    while remaining:
        layer = [i for i in sorted(remaining)
                 if all(d in placed for d in (steps[i].depends_on or []) if d < len(steps))]
        if not layer:
            layer = sorted(remaining)
        layers.append(layer)
        placed.update(layer)
        remaining -= set(layer)
    return layers


def _split_parallel_batches(indexes: list[int], steps, max_parallel: int = MAX_PARALLEL_STEPS) -> list[list[int]]:
    """把同层步骤切成并行批次：文件路径有交集的步骤降级到后续批次串行，避免写冲突。"""
    batches: list[list[int]] = []
    current: list[int] = []
    used: set[str] = set()
    for i in indexes:
        tokens = _step_file_tokens(steps[i])
        if current and (len(current) >= max_parallel or tokens & used):
            batches.append(current)
            current = []
            used = set()
        current.append(i)
        used |= tokens
    if current:
        batches.append(current)
    return batches


async def execute_split_then_main(db: AsyncSession, *, turn_id: int, group_id: int) -> dict:
    """v20: 拆分后编排——探索子代理并行调研 → 主代理串行整合执行。

    取代旧 execute_confirmed_plan 的"每步一个子代理并行执行 + 拼接 summary"。
    核心改动：
    1. 拆分出的步骤不直接分发给子代理执行；而是先并行 spawn 只读探索子代理，
       收集对工作区/相关代码/方案的调研结论（explore=true，同步拿回 findings）。
    2. 探索结论注入主代理上下文后，由主代理**串行**执行：自行读代码、写文件、
       验证；需要并行调研时可用 spawn_subagent(explore=true) 再派发探索任务。
    3. 步骤任务卡仍按拆分展示，探索子代理的执行情况实时广播。
    """
    if turn_id in _running_turns:
        return {"ok": False, "error": "turn already running"}
    _running_turns.add(turn_id)
    cancel_event = _cancel_events.setdefault(turn_id, asyncio.Event())
    _session_id: int | None = None
    try:
        from sqlalchemy import select
        from app.persistence.models.agent import Agent
        from app.persistence.models.task import Task
        from app.orchestration.context_manager import build_main_context
        from app.orchestration.subagent import get_subagent_manager
        from app.orchestration.subagent_tools import append_subagent_tools, load_subagent_type_states
        from app.orchestration.tools.registry import tool_registry
        turn = await turn_service.get_turn(db, turn_id)
        if turn is None:
            return {"ok": False, "error": "turn not found"}
        session = await session_service.get_session(db, turn.session_id)
        _session_id = session.id if session else turn.session_id
        project = await project_service.get_project(db, session.project_id) if session and session.project_id else None
        if session is None or project is None:
            return {"ok": False, "error": "session/project not found"}
        group = await db.get(Task, group_id)
        if group is None or group.turn_id != turn_id or group.kind != "group":
            return {"ok": False, "error": "task group not found"}
        request_task = await db.get(Task, group.parent_task_id) if group.parent_task_id else None
        main_agent = (await db.execute(select(Agent).where(Agent.kind == "main").limit(1))).scalars().first()
        if main_agent is None:
            return {"ok": False, "error": "主代理未初始化"}
        steps = list((await db.execute(
            select(Task).where(Task.parent_task_id == group.id, Task.is_hidden == False).order_by(Task.priority.asc(), Task.id.asc())  # noqa: E712
        )).scalars().all())
        workspace = _resolve_workspace(session, project)
        manager = get_subagent_manager(session.id)
        _turn_managers[turn_id] = manager
        effective_model_id = session.model_id or main_agent.model_id
        await broadcast(session.id, {"event": "turn.started", "payload": {"turn_id": turn_id}})
        if request_task:
            request_task.status = "running"
        group.status = "running"
        await db.commit()
        await broadcast(session.id, {"event": "task.planned", "payload": {
            "turn_id": turn_id, "request_task_id": request_task.id if request_task else None,
            "group_task_id": group.id,
            "steps": [{"task_id": s.id, "title": s.title, "status": "pending", "kind": "step"} for s in steps],
        }})

        # 确认执行后自动初始化 todo 清单（首项 in_progress，其余 pending）：
        # 任务卡/输入框贴条/任务摘要立即按拆分步骤展示进度，不依赖模型首次自觉调用 todo_write；
        # 模型后续 todo_write 全量提交时自然覆盖初始清单。
        _initial_todos = [
            {"content": s.title, "activeForm": "", "status": "in_progress" if i == 0 else "pending"}
            for i, s in enumerate(steps)
        ]
        if _initial_todos:
            await broadcast(session.id, {"event": "todo.updated", "payload": {
                "turn_id": turn_id, "todos": _initial_todos, "persisted": False,
            }})

        # 用户原始请求（主代理与探索子代理上下文继承）
        original_request = ""
        if request_task:
            original_request = f"{request_task.title or ''}\n{request_task.description or ''}".strip()
        if not original_request:
            try:
                from app.persistence.models.message import Message
                _ures = await db.execute(
                    select(Message).where(
                        Message.session_id == session.id,
                        Message.sender_type == "user",
                    ).order_by(Message.id.desc()).limit(1)
                )
                _um = _ures.scalars().first()
                if _um and isinstance(_um.content, dict):
                    original_request = str(_um.content.get("text") or "")
            except Exception:
                original_request = ""

        # v23: 移除"每个拆分步骤自动 spawn 探索子代理"的编排（对齐 codex 默认不派生子代理原则）：
        # 该路径是"1 个任务开六七个子代理"的 token 浪费根源，且完全绕过设置里的子代理开关。
        # 拆分步骤仅作任务卡展示；步骤进度改由主代理 todo_write 清单同步驱动（见 tools/todo.py）。
        _type_states = await load_subagent_type_states(db)
        _allow_subagents = bool(_type_states.get("explore", True) or _type_states.get("general", True))

        # —— 主代理串行执行 ——
        bundle = await build_main_context(
            db, agent=main_agent, session=session, project=project, turn=turn,
            user_message=original_request,
        )
        bundle.instruction = (
            "以下是已拆分出的执行步骤（任务卡据此展示进度）：\n"
            + "\n".join(f"{i + 1}. {s.title}" for i, s in enumerate(steps))
            + "\n\n执行要求：\n"
              "- 先调用 todo_write 按上面步骤逐条建立执行清单（content 与步骤标题保持完全一致），再串行推进：自行阅读、编辑、验证。\n"
              "- 每完成一步，立即用 todo_write 把该项标记为 completed、下一项标记为 in_progress。"
            + (
                "\n- 如确需并行调研多个相互独立的问题，可少量使用 spawn_subagent(explore=true) 拿回结论；"
                "简单任务或几次工具调用就能查清的问题不要派生子代理。"
                if _allow_subagents else ""
            )
        )
        main_tools = tool_registry.all_schemas()
        # v22: 拆分路径补齐 MCP 工具注入（此前仅 start_turn 路径注入，导致拆分任务中 AI 无法使用 MCP）
        try:
            from app.services.skill_service import get_agent_mcp_servers
            from app.orchestration.tools.mcp_wrapper import build_mcp_tools_for_agent
            mcp_servers = await get_agent_mcp_servers(db, main_agent)
            if mcp_servers:
                _mcp_tools = build_mcp_tools_for_agent(mcp_servers)
                for mt in _mcp_tools:
                    if not tool_registry.get(mt.name):
                        main_tools.append(mt.function_schema())
                        tool_registry.register(mt)
        except Exception:
            logger.warning("[engine] 拆分路径 MCP 工具加载失败(非阻塞)", exc_info=True)
        main_tools = append_subagent_tools(main_tools, _type_states)
        mgr = get_subagent_manager(session.id)
        _turn_managers[turn_id] = mgr
        out = await run_agent_loop(
            db, session_id=session.id, turn_id=turn_id,
            agent=main_agent, context_messages=bundle.to_messages(),
            tool_schemas=main_tools, workspace=workspace,
            cancel_event=cancel_event,
            task_id=request_task.id if request_task else None,
            model_id=effective_model_id,
            subagent_context={
                "manager": mgr, "session": session, "project": project,
                "cancel_event": cancel_event,
                "main_task_id": request_task.id if request_task else None,
                "model_id": effective_model_id,
            },
        )

        # —— 收尾：状态与产物 ——
        summary = out.text or ""
        final_status = (
            "interrupted" if cancel_event.is_set() or out.kind in ("cancelled", "interrupted")
            else "completed" if out.kind == "message" else "failed"
        )
        await turn_service.update_turn_status(
            db, turn_id, final_status,
            summary=summary[:500] or ("用户中断" if final_status == "interrupted" else None), completed=True,
        )
        cancelled_task_status = "cancelled" if final_status == "interrupted" else None
        if group is not None:
            group.status = cancelled_task_status or ("done" if out.kind == "message" else "failed")
        if request_task is not None:
            request_task.status = cancelled_task_status or ("done" if out.kind == "message" else "failed")
            if out.kind == "message" and out.artifact_ids:
                await task_service.attach_artifacts(db, request_task.id, out.artifact_ids)
        # v20: 主代理完成后统一收尾步骤状态（探索阶段步骤保持 running）
        _final_step_status = cancelled_task_status or ("done" if out.kind == "message" else "failed")
        for _s in steps:
            _row = await db.get(Task, _s.id)
            if _row is not None:
                _row.status = _final_step_status
                if _row.note is None or _row.note.startswith("探索"):
                    _row.note = None
                await broadcast(session.id, {"event": "task.updated", "payload": {
                    "task_id": _s.id, "status": _final_step_status}})
        await db.commit()
        await broadcast_turn_updated(session.id, turn_id, final_status)
        await broadcast(session.id, {
            "event": "turn.completed" if final_status == "completed" else "turn.interrupted",
            "payload": {"turn_id": turn_id, "status": final_status, "summary": summary,
                        "artifact_ids": out.artifact_ids},
        })
        await broadcast(session.id, {"event": "task.updated", "payload": {
            "task_id": request_task.id if request_task else group.id, "status": request_task.status if request_task else group.status}})
        if out.kind == "message" and summary:
            await _spawn_memory_extract(db, session_id=session.id, turn_id=turn_id, text=summary)
        # v2.2 (plan-88): plan 会话确认执行后，执行 turn 结束恢复 plan 模式
        try:
            await _maybe_restore_plan_mode(db, session)
        except Exception:
            logger.debug("[engine] 恢复 plan 模式失败(非阻塞)", exc_info=True)
        return {"ok": True, "kind": out.kind, "summary": summary}
    except Exception as exc:
        logger.exception("拆分后主代理执行失败 turn=%s group=%s", turn_id, group_id)
        try:
            await db.rollback()
            await turn_service.update_turn_status(db, turn_id, "failed", summary=str(exc)[:500], completed=True)
            await db.commit()
            await broadcast_turn_updated(_session_id or 0, turn_id, "failed")
        except Exception:
            pass
        return {"ok": False, "error": str(exc)}
    finally:
        _running_turns.discard(turn_id)
        _cancel_events.pop(turn_id, None)
        _turn_managers.pop(turn_id, None)
        cleanup(_session_id or 0)
        if _session_id:
            try:
                await broadcast(_session_id, {"event": "session.completed", "payload": {"session_id": _session_id}})
            except Exception:
                pass
        # v2.2 (plan-88): 低频 checkpoint GC（workspace 仅在 try 内定义，用 locals 兜底）
        try:
            await _maybe_run_checkpoint_gc(db, locals().get("workspace"))
        except Exception:
            pass
        # v2.2 (plan-88): plan 模式恢复兜底（异常/中断路径同样恢复）
        try:
            await _maybe_restore_plan_mode(db, locals().get("session"))
        except Exception:
            pass


async def execute_confirmed_plan(db: AsyncSession, *, turn_id: int, group_id: int) -> dict:
    """确认后按依赖顺序执行小点，每个小点使用一个隔离子代理。"""
    if turn_id in _running_turns:
        return {"ok": False, "error": "turn already running"}
    _running_turns.add(turn_id)
    cancel_event = asyncio.Event()
    _cancel_events[turn_id] = cancel_event
    _session_id: int | None = None
    try:
        from sqlalchemy import select
        from app.persistence.models.agent import Agent
        from app.persistence.models.task import Task
        from app.orchestration.context_manager import build_subagent_context
        from app.orchestration.subagent import get_subagent_manager
        turn = await turn_service.get_turn(db, turn_id)
        if turn is None:
            return {"ok": False, "error": "turn not found"}
        session = await session_service.get_session(db, turn.session_id)
        _session_id = session.id if session else turn.session_id
        project = await project_service.get_project(db, session.project_id) if session and session.project_id else None
        if session is None or project is None:
            return {"ok": False, "error": "session/project not found"}
        group = await db.get(Task, group_id)
        if group is None or group.turn_id != turn_id or group.kind != "group":
            return {"ok": False, "error": "task group not found"}
        request_task = await db.get(Task, group.parent_task_id) if group.parent_task_id else None
        main_agent = (await db.execute(select(Agent).where(Agent.kind == "main").limit(1))).scalars().first()
        if main_agent is None:
            return {"ok": False, "error": "主代理未初始化"}
        steps = list((await db.execute(
            select(Task).where(Task.parent_task_id == group.id, Task.is_hidden == False).order_by(Task.priority.asc(), Task.id.asc())  # noqa: E712
        )).scalars().all())
        workspace = _resolve_workspace(session, project)
        manager = get_subagent_manager(session.id)
        _turn_managers[turn_id] = manager
        effective_model_id = session.model_id or main_agent.model_id
        tool_schemas = tool_registry.all_schemas()
        completed = 0
        failed = 0
        summaries: list[str] = []
        await broadcast(session.id, {"event": "turn.started", "payload": {"turn_id": turn_id}})
        if request_task:
            request_task.status = "running"
        group.status = "running"
        await db.commit()
        await broadcast(session.id, {"event": "task.planned", "payload": {
            "turn_id": turn_id, "request_task_id": request_task.id if request_task else None,
            "group_task_id": group.id,
            "steps": [{"task_id": s.id, "title": s.title, "status": "pending", "kind": "step"} for s in steps],
        }})

        # 确认执行后自动初始化 todo 清单（与 execute_split_then_main 同口径）
        _initial_todos = [
            {"content": s.title, "activeForm": "", "status": "in_progress" if i == 0 else "pending"}
            for i, s in enumerate(steps)
        ]
        if _initial_todos:
            await broadcast(session.id, {"event": "todo.updated", "payload": {
                "turn_id": turn_id, "todos": _initial_todos, "persisted": False,
            }})

        # —— 拓扑分层并行执行：同层无依赖步骤并行（上限 3），层间串行 ——
        from app.persistence.database import async_session_factory
        step_status: dict[int, str] = {i: "pending" for i in range(len(steps))}
        done_summaries: list[str] = []

        def _prior_summaries_text() -> str:
            """前序已完成步骤摘要链：v19 扩为全部已完成步骤、总长 ≤2000 字，消除上下文断裂。"""
            text = "\n".join(done_summaries)
            return text[:2000]

        # v19: 用户原始请求（子代理上下文继承）——优先主请求任务，缺则最近 user 消息
        original_request = ""
        if request_task:
            original_request = f"{request_task.title or ''}\n{request_task.description or ''}".strip()
        if not original_request:
            try:
                from app.persistence.models.message import Message
                _ures = await db.execute(
                    select(Message).where(
                        Message.session_id == session.id,
                        Message.sender_type == "user",
                    ).order_by(Message.id.desc()).limit(1)
                )
                _um = _ures.scalars().first()
                if _um and isinstance(_um.content, dict):
                    original_request = str(_um.content.get("text") or "")
            except Exception:
                original_request = ""

        async def _run_one_step(index: int) -> None:
            nonlocal completed, failed
            step = steps[index]
            async with async_session_factory() as sdb:
                step_row = await sdb.get(Task, step.id)

                async def _mark(status: str, note: str) -> None:
                    step_status[index] = status
                    step_row.status = status
                    step_row.note = note or None
                    await sdb.commit()
                    await broadcast(session.id, {"event": "task.updated", "payload": {
                        "task_id": step.id, "status": status, "note": note or ""}})

                if cancel_event.is_set():
                    await _mark("cancelled", "")
                    return
                deps = step.depends_on or []
                if any(d >= len(steps) or step_status.get(d) != "done" for d in deps):
                    failed += 1
                    summaries.append(f"× {step.title}：依赖步骤未完成")
                    await _mark("cancelled", "依赖步骤未完成")
                    return
                sub_agent = Agent(
                    kind="sub", name=step.title[:40], model_id=effective_model_id,
                    session_id=session.id, turn_id=turn_id, parent_agent_id=main_agent.id,
                )
                sdb.add(sub_agent)
                await sdb.flush()
                step_row.agent_id = sub_agent.id
                step_row.status = "running"
                await sdb.commit()
                await broadcast(session.id, {"event": "task.updated", "payload": {"task_id": step.id, "status": "running"}})
                # v23: 任务消息落库到子代理线程，右侧子代理面板可见"主 AI 下发的任务"
                try:
                    await message_service.create_message(
                        sdb, session_id=session.id, turn_id=turn_id, thread_id=sub_agent.id,
                        sender_type=SenderType.USER.value, msg_type=MsgType.TEXT.value,
                        content={"text": (f"{step.title}\n\n{step.description or ''}").strip()},
                    )
                except Exception:
                    logger.warning("[engine] 子代理任务消息落库失败(非阻塞)", exc_info=True)
                handoff = step.description or step.title
                prior = _prior_summaries_text()
                if prior:
                    handoff = f"{handoff}\n\n【前序步骤结论】\n{prior}"
                context = await build_subagent_context(
                    sdb, agent=sub_agent, session=session, project=project, task=step_row,
                    handoff_summary=handoff,
                    original_request=original_request,
                )
                handle_id = manager.spawn(
                    sdb, agent=sub_agent, turn_id=turn_id, task=step_row,
                    handoff_summary=handoff,
                    context_bundle=context, tool_schemas=tool_schemas,
                    workspace=workspace, cancel_event=cancel_event,
                )
                # v19: 与 spawn_subagent 工具路径对齐——广播子代理启动事件，前端消息流卡片据此实时展示
                await broadcast(session.id, {
                    "event": "agent.started",
                    "payload": {"agent_id": sub_agent.id, "kind": "sub",
                                "name": sub_agent.name, "turn_id": turn_id,
                                "task_id": step_row.id},
                })
                handle = manager.get(handle_id)
                if handle and handle.task:
                    await handle.task
                if handle and handle.status == "done":
                    if handle.artifact_ids:
                        await task_service.attach_artifacts(sdb, step.id, handle.artifact_ids)
                    completed += 1
                    if handle.summary:
                        summaries.append(f"✓ {step.title}：{handle.summary[:240]}")
                        done_summaries.append(f"{step.title}：{handle.summary[:240]}")
                    await _mark("done", "")
                else:
                    note = (handle.error if handle else "子代理未返回结果")[:500]
                    failed += 1
                    summaries.append(f"× {step.title}：{note}")
                    await _mark("failed", note)

        for layer in _layer_step_indexes(steps):
            for batch in _split_parallel_batches(layer, steps):
                await asyncio.gather(*(_run_one_step(i) for i in batch))

        active_steps = [step for step in steps if not step.is_hidden]
        group.status = "done" if failed == 0 and completed == len(active_steps) else ("partial" if completed else "failed")
        if request_task:
            request_task.status = "done" if group.status == "done" else ("partial" if completed else "failed")
            request_task.note = f"完成 {completed}/{len(active_steps)} 个步骤"
        summary = "任务步骤执行完成。\n\n" + "\n".join(summaries)
        await message_service.create_message(
            db, session_id=session.id, turn_id=turn_id,
            sender_type=SenderType.AGENT.value, sender_id=main_agent.id,
            msg_type=MsgType.TEXT.value, content={"text": summary, "agent_name": main_agent.name},
        )
        await turn_service.update_turn_status(db, turn_id, "completed", summary=summary[:500], completed=True)
        await db.commit()
        await broadcast_turn_updated(session.id, turn_id, "completed")
        await broadcast(session.id, {"event": "turn.completed", "payload": {"turn_id": turn_id, "summary": summary, "artifact_ids": []}})
        # v2.2 (plan-88): plan 会话确认执行后，执行 turn 结束恢复 plan 模式
        try:
            await _maybe_restore_plan_mode(db, session)
        except Exception:
            logger.debug("[engine] 恢复 plan 模式失败(非阻塞)", exc_info=True)
        return {"ok": True, "summary": summary}
    except Exception as exc:
        logger.exception("确认任务执行失败 turn=%s group=%s", turn_id, group_id)
        try:
            await db.rollback()
            await turn_service.update_turn_status(db, turn_id, "failed", summary=str(exc)[:500], completed=True)
            await db.commit()
            # v1.1: 失败态先落库再广播（铁律A），前端据此复位
            await broadcast_turn_updated(_session_id or 0, turn_id, "failed")
        except Exception:
            pass
        return {"ok": False, "error": str(exc)}
    finally:
        _running_turns.discard(turn_id)
        _cancel_events.pop(turn_id, None)
        _turn_managers.pop(turn_id, None)
        cleanup(_session_id or 0)
        # v1.1: 无条件广播 session.completed，驱动前端摘除左侧转圈
        if _session_id:
            try:
                await broadcast(_session_id, {"event": "session.completed", "payload": {"session_id": _session_id}})
            except Exception:
                pass
        # v2.2 (plan-88): 低频 checkpoint GC
        try:
            await _maybe_run_checkpoint_gc(db, locals().get("workspace"))
        except Exception:
            pass
        # v2.2 (plan-88): plan 模式恢复兜底（异常/中断路径同样恢复）
        try:
            await _maybe_restore_plan_mode(db, locals().get("session"))
        except Exception:
            pass


async def retry_failed_step(db: AsyncSession, *, turn_id: int, task_id: int) -> dict:
    """重试单个失败/已取消的步骤：重新 spawn 子代理执行该步（路由在后台任务中调用）。"""
    from sqlalchemy import select
    from app.persistence.models.agent import Agent
    from app.persistence.models.task import Task
    step = await db.get(Task, task_id)
    if step is None or step.turn_id != turn_id or step.kind != "step":
        return {"ok": False, "error": "step not found"}
    if step.status not in ("failed", "cancelled"):
        return {"ok": False, "error": "step not retryable"}
    turn = await turn_service.get_turn(db, turn_id)
    if turn is None:
        return {"ok": False, "error": "turn not found"}
    session = await session_service.get_session(db, turn.session_id)
    project = await project_service.get_project(db, session.project_id) if session and session.project_id else None
    if session is None or project is None:
        return {"ok": False, "error": "session/project not found"}
    main_agent = (await db.execute(select(Agent).where(Agent.kind == "main").limit(1))).scalars().first()
    if main_agent is None:
        return {"ok": False, "error": "主代理未初始化"}
    workspace = _resolve_workspace(session, project)
    manager = get_subagent_manager(session.id)
    effective_model_id = session.model_id or main_agent.model_id
    tool_schemas = tool_registry.all_schemas()
    cancel_event = asyncio.Event()
    sub_agent = Agent(
        kind="sub", name=step.title[:40], model_id=effective_model_id,
        session_id=session.id, turn_id=turn_id, parent_agent_id=main_agent.id,
    )
    db.add(sub_agent)
    await db.flush()
    step.agent_id = sub_agent.id
    step.status = "running"
    step.note = None
    await db.commit()
    await broadcast(session.id, {"event": "task.updated", "payload": {"task_id": step.id, "status": "running"}})
    handoff = step.description or step.title
    # v19: 重试步骤同样继承用户原始请求
    original_request = ""
    try:
        from app.persistence.models.message import Message
        _ures = await db.execute(
            select(Message).where(
                Message.session_id == session.id,
                Message.sender_type == "user",
            ).order_by(Message.id.desc()).limit(1)
        )
        _um = _ures.scalars().first()
        if _um and isinstance(_um.content, dict):
            original_request = str(_um.content.get("text") or "")
    except Exception:
        original_request = ""
    context = await build_subagent_context(
        db, agent=sub_agent, session=session, project=project, task=step,
        handoff_summary=handoff,
        original_request=original_request,
    )
    handle_id = manager.spawn(
        db, agent=sub_agent, turn_id=turn_id, task=step,
        handoff_summary=handoff,
        context_bundle=context, tool_schemas=tool_schemas,
        workspace=workspace, cancel_event=cancel_event,
    )
    # v19: 重试也广播子代理启动事件
    await broadcast(session.id, {
        "event": "agent.started",
        "payload": {"agent_id": sub_agent.id, "kind": "sub",
                    "name": sub_agent.name, "turn_id": turn_id,
                    "task_id": step.id},
    })
    handle = manager.get(handle_id)
    if handle and handle.task:
        await handle.task
    ok = bool(handle and handle.status == "done")
    if ok:
        if handle.artifact_ids:
            await task_service.attach_artifacts(db, step.id, handle.artifact_ids)
        step.status = "done"
        step.note = (handle.summary or "")[:300] or None
    else:
        step.status = "failed"
        step.note = (handle.error if handle else "子代理未返回结果")[:500]
    await db.commit()
    await broadcast(session.id, {"event": "task.updated", "payload": {
        "task_id": step.id, "status": step.status, "note": step.note or ""}})
    return {"ok": ok, "status": step.status}


async def _spawn_memory_extract(db, *, session_id: int, turn_id: int, text: str,
                                auto_memory_enabled: bool | None = None) -> None:
    """异步提取记忆（D8）。不阻塞 turn 返回。

    受设置中心「AI 主动生成记忆」开关控制：关闭时（settings.auto_memory_enabled=False）
    跳过提取。
    """
    if auto_memory_enabled is None:
        auto_memory_enabled = settings.auto_memory_enabled
    if not auto_memory_enabled:
        return

    async def _extract():
        try:
            from app.models.registry import get_model_registry
            from app.models.schemas import ChatMessage, ChatRequest
            provider = get_model_registry().get_default_provider()
            if provider is None:
                return
            req = ChatRequest(messages=[
                ChatMessage(role="system", content=(
                    "Extract 1-3 durable, useful facts about the project from the text below "
                    "(code conventions, pitfalls, architecture decisions). "
                    'Return a JSON array like [{"kind": "fact", "text": "...", "importance": 0.8}]. '
                    "importance must be 0..1; return [] for low-importance or transient content.")
                ),
                ChatMessage(role="user", content=text[:4000]),
            ], model="")
            resp = await provider.chat(req)
            import json as _json
            import re as _re
            raw = resp.content or ""
            match = _re.search(r"\[.*\]", raw, _re.DOTALL)
            if not match:
                return
            data = _json.loads(match.group(0))
            if not isinstance(data, list):
                return
            from app.persistence.database import async_session_factory
            from app.services.memory_service import save_memories
            async with async_session_factory() as s:
                await save_memories(s, session_id=session_id, turn_id=turn_id, memories=data)
                await s.commit()
        except Exception:
            logger.debug("记忆提取失败(非阻塞)", exc_info=True)

    asyncio.get_event_loop().create_task(_extract())
