"""Turn 引擎（v2 总控）：一次用户消息 = 一个 turn。

流程：创建快照 → 构建主上下文 → 主代理 loop（可 spawn 子代理）→
收集结果 → turn 完成摘要 → 记忆提取（异步）→ 广播。
"""
import asyncio
import logging
import re
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.enums import MsgType, SenderType
from app.orchestration.agent_events import broadcast, broadcast_turn_updated
from app.orchestration.agent_loop import run_agent_loop
from app.orchestration.context_manager import build_main_context, build_subagent_context
from app.orchestration.subagent import cleanup, get_subagent_manager
from app.orchestration.tools.registry import tool_registry
from app.orchestration.task_planner import PlannedStep, decompose_request, evaluate_complexity
from app.services import audit_service, message_service, project_service, rollback_service, session_service, task_service, turn_service

logger = logging.getLogger(__name__)

# 会话级运行锁（SQLite 串行写）
_running_turns: set[int] = set()
_cancel_events: dict[int, asyncio.Event] = {}

# 命令模式：只读审阅（/chat）工具白名单
_READONLY_TOOLS = [
    "fs_read", "fs_list", "fs_grep", "git_diff",
    "memory_search", "web_fetch", "web_search", "view_image",
    "read_attachment", "codebase_search",
]
# 规划模式（/plan）：只读 + 允许写计划文档
_PLAN_TOOLS = _READONLY_TOOLS + ["fs_write"]

_MODE_HINTS = {
    "readonly": (
        "【审阅模式】当前处于只读审阅模式，你只能查看、检索、分析代码，"
        "严禁执行任何修改操作（禁止写入文件、运行命令、应用补丁）。"
        "请直接给出审阅意见、发现的问题与改进建议。"
    ),
    "plan": (
        "【规划模式】请先规划再执行：不要直接修改业务代码。"
        "先在项目根目录创建 ai/ 目录，在其中编写任务计划文档 ai/chatcoder-plan.md，"
        "包含目标、步骤拆解、涉及文件与验收标准。方案文档完成后系统会自动把方案步骤"
        "拆成可执行小点并生成待确认任务卡；不要执行计划中的业务修改动作。"
    ),
}


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
    cancel_event = asyncio.Event()
    _cancel_events[turn_id] = cancel_event
    # v7: 主 turn 对应的任务记录（任务摘要步骤），创建后跟踪状态与产物
    main_task = None

    try:
        session = await session_service.get_session(db, session_id)
        if session is None:
            return {"ok": False, "error": "session not found"}
        project = await project_service.get_project(db, session.project_id) if session.project_id else None
        if project is None:
            return {"ok": False, "error": "项目不存在，请先关联项目"}

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

        # 普通复杂请求先生成待确认提案，不启动主代理或子代理。
        if existing_task_id is None and not force_direct and verdict and verdict.decision == "split" and mode != "plan":
            group, steps = await _create_task_proposal(
                db, session_id=session_id, turn_id=turn_id, request_task=main_task,
                provider=planner_provider, source_text=user_text,
                suggested_steps=verdict.suggested_steps, plan_mode=False,
            )
            await turn_service.update_turn_status(
                db, turn_id, "awaiting_confirmation",
                summary="任务已拆分，等待确认后执行", completed=True,
            )
            await db.commit()
            await _broadcast_task_proposed(
                session_id, turn_id, main_task, group, steps,
                reasons=verdict.reasons,
            )
            await broadcast_turn_updated(session_id, turn_id, "awaiting_confirmation")
            return {"ok": True, "awaiting_confirmation": True, "task_id": main_task.id}

        # 1. 主上下文
        # v15: 多模态模型时图片附件直接注入用户消息（AI 直接看图，不再走工具猜测路径）
        _is_multimodal = bool(getattr(selected_model, "is_multimodal", False)) if selected_model else False
        bundle = await build_main_context(
            db, agent=main_agent, session=session, project=project, turn=turn,
            user_message=user_text, attachments=attachments,
            multimodal=_is_multimodal,
        )

        # 命令模式：注入模式指令（/chat 只读、/plan 先规划后执行）
        if mode in _MODE_HINTS:
            bundle.instruction = (_MODE_HINTS[mode] + "\n\n" + bundle.instruction).strip() if bundle.instruction else _MODE_HINTS[mode]

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
        # 子任务由 task_planner + execute_confirmed_plan 统一编排。
        # 不再把 spawn_subagent 暴露给主代理，避免模型绕过提案确认自行制造任务卡条目。
        # 3. 运行主代理
        # v10/v13: 会话级模型覆盖；同一模型也用于规划评估与拆分。
        from app.orchestration.subagent import get_subagent_manager
        mgr = get_subagent_manager(session_id)
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
        if mode == "plan" and settings.plan_mode_auto_split:
            plan_source = _read_plan_document(workspace) or (out.text or user_text)
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
            await _broadcast_task_proposed(
                session_id, turn_id, main_task, group, steps,
                reasons=["/plan 方案文档已自动转换为任务步骤"],
            )
            await broadcast_turn_updated(session_id, turn_id, "awaiting_confirmation")
            return {"ok": True, "awaiting_confirmation": True, "task_id": main_task.id}

        # 4. turn 完成：取消是用户中断，不应伪装成失败；先落库再广播。
        summary = out.text or ""
        final_status = (
            "interrupted" if out.kind in ("cancelled", "interrupted")
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
        _task_status = "done" if out.kind == "message" else "failed"
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

        # 5. 记忆提取（异步，不阻塞）
        if out.kind == "message" and summary:
            _spawn_memory_extract(db, session_id=session_id, turn_id=turn_id, text=summary)

        # 6. 会话自动命名（首条消息）
        if not session.title and turn.user_message_id:
            await _auto_title(db, session, user_text)

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
        cleanup(session_id)
        # v1.1: 无条件广播 session.completed，驱动前端摘除左侧转圈（无论成败）
        try:
            await broadcast(session_id, {"event": "session.completed", "payload": {"session_id": session_id}})
        except Exception:
            pass


async def cancel_turn(turn_id: int) -> bool:
    ev = _cancel_events.get(turn_id)
    if ev is None:
        return False
    ev.set()
    return True


def _resolve_workspace(session, project) -> str:
    if session.worktree_path:
        return session.worktree_path
    return project.path if project else ""


def _read_plan_document(workspace: str) -> str:
    """读取 /plan 阶段约定的方案文件；只读，不执行文件内容。"""
    if not workspace:
        return ""
    root = Path(workspace).resolve()
    candidates = (root / "ai" / "chatcoder-plan.md", root / "ai" / "plan.md")
    for path in candidates:
        try:
            resolved = path.resolve()
            if resolved.is_file() and (resolved == root or root in resolved.parents):
                return resolved.read_text(encoding="utf-8", errors="replace")[:24000]
        except OSError:
            continue
    return ""


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
                                   reasons: list[str] | None = None) -> None:
    await broadcast(session_id, {
        "event": "task.proposed",
        "payload": {
            "turn_id": turn_id,
            "request_task_id": request_task.id,
            "group_task_id": group.id,
            "reasons": reasons or [],
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

        # —— 拓扑分层并行执行：同层无依赖步骤并行（上限 3），层间串行 ——
        from app.persistence.database import async_session_factory
        step_status: dict[int, str] = {i: "pending" for i in range(len(steps))}
        done_summaries: list[str] = []

        def _prior_summaries_text() -> str:
            """前序已完成步骤摘要链：最近 3 步、总长 ≤800 字，消除上下文断裂。"""
            text = "\n".join(done_summaries[-3:])
            return text[:800]

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
                handoff = step.description or step.title
                prior = _prior_summaries_text()
                if prior:
                    handoff = f"{handoff}\n\n【前序步骤结论】\n{prior}"
                context = await build_subagent_context(
                    sdb, agent=sub_agent, session=session, project=project, task=step_row,
                    handoff_summary=handoff,
                )
                handle_id = manager.spawn(
                    sdb, agent=sub_agent, turn_id=turn_id, task=step_row,
                    handoff_summary=handoff,
                    context_bundle=context, tool_schemas=tool_schemas,
                    workspace=workspace, cancel_event=cancel_event,
                )
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
        cleanup(_session_id or 0)
        # v1.1: 无条件广播 session.completed，驱动前端摘除左侧转圈
        if _session_id:
            try:
                await broadcast(_session_id, {"event": "session.completed", "payload": {"session_id": _session_id}})
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
    context = await build_subagent_context(
        db, agent=sub_agent, session=session, project=project, task=step,
        handoff_summary=handoff,
    )
    handle_id = manager.spawn(
        db, agent=sub_agent, turn_id=turn_id, task=step,
        handoff_summary=handoff,
        context_bundle=context, tool_schemas=tool_schemas,
        workspace=workspace, cancel_event=cancel_event,
    )
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


async def _spawn_memory_extract(db, *, session_id: int, turn_id: int, text: str) -> None:
    """异步提取记忆（D8）。不阻塞 turn 返回。"""

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
                    'Return a JSON array like [{"kind": "fact", "text": "..."}]. '
                    "Return [] if nothing durable.")
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


async def _auto_title(db: AsyncSession, session, first_text: str) -> None:
    """用首条消息自动命名会话（截断即可，不做 LLM 调用以保持轻量）。"""
    title = first_text.strip().replace("\n", " ")[:30]
    if title:
        session.title = title or "新会话"
        await db.flush()
