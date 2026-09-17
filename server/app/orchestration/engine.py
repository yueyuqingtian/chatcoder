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

from app.core.config import resolve_workspace_root, settings
from app.core.enums import MsgType, SenderType
from app.orchestration.agent_events import (broadcast, broadcast_session_completed,
                                            broadcast_turn_updated)
from app.orchestration.agent_loop import run_agent_loop
from app.orchestration.context_manager import (
    _collect_plan_history, build_main_context, build_subagent_context,
)
from app.orchestration.subagent import cleanup, get_subagent_manager
from app.orchestration.subagent_tools import append_subagent_tools, load_subagent_type_states
from app.orchestration.tools.registry import tool_registry
from app.persistence.write_behind import write_behind
from app.services import audit_service, message_service, project_service, rollback_service, session_service, task_service, turn_service

logger = logging.getLogger(__name__)

# 会话级运行锁（SQLite 串行写）
_running_turns: set[int] = set()
_cancel_events: dict[int, asyncio.Event] = {}
_turn_managers: dict[int, object] = {}
_turn_tasks: dict[int, asyncio.Task] = {}


async def _flush_turn_buffer(session_id: int, turn_id: int) -> None:
    """在 turn 结束/状态直写前排空 write-behind 缓冲，并释放该 turn 的缓冲槽。"""
    if turn_id is None:
        return
    try:
        buf = write_behind.get(session_id, turn_id)
        await buf.flush()
        await write_behind.detach(session_id, turn_id)  # turn 收尾后释放，防注册表累积
    except Exception:
        logger.debug("[engine] flush turn buffer failed turn=%s", turn_id, exc_info=True)


async def _patch_turn(turn_id: int, **fields) -> None:
    """以写引擎单写线程提交 Turn 字段补丁（无锁单写者；None 值跳过，status 除外）。"""
    from app.persistence.database import run_write_locked

    def patch(s):
        from app.persistence.models.turn import Turn
        t = s.get(Turn, turn_id)
        if t is None:
            return
        for k, v in fields.items():
            if v is not None or k == "status":
                setattr(t, k, v)
        s.commit()

    await run_write_locked(patch, label=f"turn.patch.{turn_id}")


async def _patch_task(task_id: int, **fields) -> None:
    """以写引擎单写线程提交 Task 字段补丁。"""
    from app.persistence.database import run_write_locked

    def patch(s):
        from app.persistence.models.task import Task
        t = s.get(Task, task_id)
        if t is None:
            return
        for k, v in fields.items():
            if v is not None or k == "status":
                setattr(t, k, v)
        s.commit()

    await run_write_locked(patch, label=f"task.patch.{task_id}")


async def _patch_turn_bulk(turn_ids: list[int], **fields) -> None:
    """以写引擎单写线程对多个 Turn 应用同一字段补丁（如批量 superseded）。"""
    if not turn_ids:
        return
    from app.persistence.database import run_write_locked

    def patch(s):
        from app.persistence.models.turn import Turn
        rows = s.query(Turn).filter(Turn.id.in_(turn_ids)).all()
        for t in rows:
            for k, v in fields.items():
                if v is not None or k == "status":
                    setattr(t, k, v)
        s.commit()

    await run_write_locked(patch, label="turn.patch.bulk")


async def _patch_task_bulk(task_ids: list[int], **fields) -> None:
    """以写引擎单写线程对多个 Task 应用同一字段补丁（如批量 cancelled）。"""
    if not task_ids:
        return
    from app.persistence.database import run_write_locked

    def patch(s):
        from app.persistence.models.task import Task
        rows = s.query(Task).filter(Task.id.in_(task_ids)).all()
        for t in rows:
            for k, v in fields.items():
                if v is not None or k == "status":
                    setattr(t, k, v)
        s.commit()

    await run_write_locked(patch, label="task.patch.bulk")


async def _patch_message(message_id: int, **fields) -> None:
    """写引擎单写线程提交 Message 字段补丁（如 turn_id 回填）。"""
    from app.persistence.database import run_write_locked

    def patch(s):
        from app.persistence.models.message import Message
        m = s.get(Message, message_id)
        if m is None:
            return
        for k, v in fields.items():
            setattr(m, k, v)
        s.commit()

    await run_write_locked(patch, label=f"message.patch.{message_id}")


async def _patch_session(session_id: int, **fields) -> None:
    """写引擎单写线程提交 Session 字段补丁（如 goal_turns_used）。"""
    from app.persistence.database import run_write_locked

    def patch(s):
        from app.persistence.models.message import Session as _Sess
        row = s.get(_Sess, session_id)
        if row is None:
            return
        for k, v in fields.items():
            setattr(row, k, v)
        s.commit()

    await run_write_locked(patch, label=f"session.patch.{session_id}")


async def _patch_turn_cancel(turn_rows: list, turn_ids: list[int]) -> None:
    """批量置 cancelled：status=cancelled，completed_at 缺失时补当前时间（写引擎内）。"""
    from datetime import datetime, timezone
    from app.persistence.database import run_write_locked

    def patch(s):
        from app.persistence.models.turn import Turn
        rows = s.query(Turn).filter(Turn.id.in_(turn_ids)).all()
        for t in rows:
            t.status = "cancelled"
            t.completed_at = t.completed_at or datetime.now(timezone.utc).isoformat()
        s.commit()

    await run_write_locked(patch, label="turn.patch.cancel")
_cancel_finalizers: dict[int, asyncio.Task] = {}
# plan-547: 运行中 turn 的用户消息注入队列（turn_id -> 待注入项）。
# 注入项在 agent_loop 每次 LLM 调用前被 drain 进上下文（下次调用前传达给 AI）。
_pending_inputs: dict[int, list[dict]] = {}


def inject_input(turn_id: int, item: dict) -> bool:
    """向运行中的 turn 注入一条用户消息。turn 未在运行时返回 False（调用方走常规发送）。"""
    if turn_id not in _running_turns:
        return False
    _pending_inputs.setdefault(turn_id, []).append(item)
    return True


def drain_injected_inputs(turn_id: int) -> list[dict]:
    """取走并清空该 turn 的待注入项（agent_loop 每次 LLM 调用前调用）。"""
    return _pending_inputs.pop(turn_id, [])


def discard_injected_inputs(turn_id: int) -> None:
    """turn 结束时丢弃残留注入项（防内存泄漏；正常路径已被 drain 清空）。"""
    _pending_inputs.pop(turn_id, None)

# 命令模式：只读审阅（/chat）工具白名单
# v7: 追加 ask_user_question/todo_write 为通用工具——四种模式（只读/计划/完全访问/计划执行）
# 全部可用：结构化提问（需求澄清）与执行清单是通用能力，不因模式受限。
_READONLY_TOOLS = [
    "fs_read", "fs_list", "fs_grep", "git_diff",
    "memory_search", "web_fetch", "web_search", "view_image",
    "read_attachment", "codebase_search",
    "ask_user_question", "todo_write",
    # plan-230-1144 M1.2: 技能加载是纯读操作，四种模式全部可用——
    # 只读/计划模式下 AI 同样需要查阅技能知识（如审阅规范、计划模板）。
    "skill_view",
    # plan-230-1144 M1.2 配套: 压缩回看同属纯读，计划/只读模式也应可检索早期上下文（问题4）
    "compaction_index", "compaction_view",
    # plan-230-1144 M3: 符号索引检索/文件骨架（纯读探索能力；正常路径由
    # permission_profile_service 白名单供给，此处兜底保持行为一致）
    "symbol_search", "outline",
]
# 规划模式（/plan）：只读 + 允许写计划文档 + 命令行
# v3.0 (plan-88): 追加 terminal_exec——计划模式 AI 可执行命令（只读命令免审批，
# 其余走审批卡；cwd 是否可越出工作区由 plan_mode_allow_outside_access 开关控制）。
# todo_write/ask_user_question 已属通用工具（见 _READONLY_TOOLS），此处不再重复。
_PLAN_TOOLS = _READONLY_TOOLS + ["fs_write", "terminal_exec"]

_MODE_HINTS = {
    "readonly": (
        "【审阅模式】当前处于只读审阅模式，你只能查看、检索、分析代码，"
        "严禁执行任何修改操作（禁止写入文件、运行命令、应用补丁）。"
        "请直接给出审阅意见、发现的问题与改进建议。"
    ),
    "plan": (
        "【规划模式】请先规划再执行：不要直接修改业务代码。"
        "执行流程：\n"
        "1. 先用 todo_write 建立调研清单（要改哪些文件、要先查清哪些事实），"
        "再按清单逐项探索；每完成一项立即标记为 completed。\n"
        "1a. 若上下文提供「Plan History」（本会话此前各轮计划与状态），"
        "按 Collect → Merge → Replan 三步产出新方案：Collect——逐轮读取，"
        "找出所有未完结轮中尚未完成的需求（done/cancelled 轮的已完成内容"
        "不重复列入，cancelled 除非用户本轮重提）；Merge——把历史未完成项"
        "与本轮新增需求合并为统一需求全集，同一需求多轮出现时合并去重、"
        "以最新表述为准，仅用户明确移除的才剔除；Replan——以合并后的全集"
        "重新完整规划（目标、步骤、顺序、涉及文件、验收标准），生成全新"
        "文档，不在旧文档上打补丁、不只写新需求增量。计划文档形态多变，"
        "禁止文本匹配比对，按语义继承每个未完成项。\n"
        "2. 探索完成后，在项目根目录创建 ai/ 目录，用 fs_write 编写本计划文档 "
        "ai/chatcoder-plan-{session_id}-{turn_id}.md，"
        "必须严格使用这个文件名，不要自行改名或加序号/时间戳。"
        "文档包含目标、步骤拆解、涉及文件与验收标准。\n"
        "3. 文档写出后本轮即结束，交付用户确认；此时不要执行方案中的业务修改动作。\n"
        "4. 执行期间保持清单状态实时更新：每完成一项立即用 todo_write 标记 completed，系统会在每次调用时向你提供计划状态全集。\n"
        "【重要】方案文档必须通过调用 fs_write 工具写入文件才算完成——"
        "仅在回复正文或思考中输出方案内容不会被保存，未调用 fs_write 写入文档将导致任务失败。"
        "【注意】系统不会替你拆分步骤，也不会要求你确认步骤；分步完全由你通过 todo_write 决定。"
    ),
}


async def _inject_mcp_tools(
    db, agent, tool_schemas: list, turn_id: int, *, readonly_only: bool = False,
    allowed: set[str] | None = None,
) -> int:
    """主 turn 路径 MCP 工具注入（plan-230-1144 M1.3）。

    此前同一段逻辑在 engine.py 里复制粘贴了 3 份，且 readonly/plan 分支根本不调用
    它——导致计划/审阅模式下 MCP 工具完全不可用。现收敛为单一入口：

    - 按 schema 名去重（v10 修复：无条件 append 会使第二次 turn 出现重复工具名，
      LLM 报 "Tool names must be unique" HTTP 400）；
    - 全局 registry 缺失时才注册，使 executor 可执行；
    - readonly_only=True（只读/计划模式）时仅注入 low 风险（只读类）MCP 工具，
      让 codegraph 这类检索工具在规划/审阅时同样可用；写类 MCP 仍不暴露；
    - allowed 非 None 时只注入其中列出的 MCP 工具（设置页白名单勾选对 MCP 生效，
      由 permission_profile_service.mcp_allowed_tools 计算；None = 不限制）。

    失败不阻塞（返回 0）。返回实际注入数量。
    """
    try:
        from app.orchestration.tools.mcp_wrapper import build_mcp_tools_for_agent
        from app.services.skill_service import get_agent_mcp_servers
        mcp_servers = await get_agent_mcp_servers(db, agent)
        if not mcp_servers:
            return 0
        _mcp_tools = build_mcp_tools_for_agent(mcp_servers)
        if allowed is not None:
            _mcp_tools = [t for t in _mcp_tools if t.name in allowed]
        if readonly_only:
            _mcp_tools = [t for t in _mcp_tools if getattr(t, "risk_level", "medium") == "low"]
        existing = {str(s.get("function", {}).get("name") or "") for s in tool_schemas}
        injected = 0
        for mt in _mcp_tools:
            if mt.name in existing:
                continue
            tool_schemas.append(mt.function_schema())
            existing.add(mt.name)
            if not tool_registry.get(mt.name):
                tool_registry.register(mt)
            injected += 1
        if injected:
            logger.info(
                "[engine] turn=%s 注入 %d 个 MCP 工具%s",
                turn_id, injected, "（仅只读类）" if readonly_only else "",
            )
        return injected
    except Exception:
        logger.debug("[engine] turn=%s MCP 工具注入失败(非阻塞)", turn_id, exc_info=True)
        return 0


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


# ── 目标模式（plan-671，对齐 zcode goal-continuation）──

def _goal_active(session) -> bool:
    return settings.goal_mode_enabled and (getattr(session, "goal_status", None) or "none") == "active"


def _should_continue_goal(session, final_status: str, cancel_event: asyncio.Event) -> bool:
    """turn 完成后的续跑判定：目标激活 + 正常完成 + 轮次未耗尽 + 未被取消。"""
    return (
        _goal_active(session)
        and final_status == "completed"
        and (session.goal_turns_used or 0) < settings.goal_max_continuation_turns
        and not cancel_event.is_set()
    )


async def _goal_todo_summary(db: AsyncSession, session_id: int) -> str:
    """最新 todo 清单状态摘要（续跑提示词附带给模型，失败返回空串）。"""
    try:
        from sqlalchemy import select

        from app.persistence.models.task import Task
        group = (await db.execute(
            select(Task).where(
                Task.session_id == session_id, Task.kind == "group", Task.title == "任务清单",
            ).order_by(Task.id.desc()).limit(1)
        )).scalars().first()
        if group is None:
            return ""
        steps = (await db.execute(
            select(Task).where(Task.parent_task_id == group.id)
            .order_by(Task.priority.asc(), Task.id.asc())
        )).scalars().all()
        if not steps:
            return ""
        lines = [f"- [x] {s.title}" if s.status == "done" else f"- [ ] {s.title}" for s in steps]
        return "当前清单状态：\n" + "\n".join(lines) + "\n\n"
    except Exception:
        logger.debug("[goal] todo 摘要读取失败(非阻塞)", exc_info=True)
        return ""


async def _continue_goal_turn(session_id: int, prev_turn_id: int) -> None:
    """目标续跑：间隔窗口后由服务端创建新 turn 继续推进（客户端断线不中断）。

    - 间隔窗口内被取消（用户停止）→ 直接返回，目标保持 active 可手动继续；
    - 续跑消息带 goal_continuation 标记（zcode model-only 语义：进模型上下文，
      前端渲染为细分隔线而非用户气泡）。
    """
    from app.persistence.database import async_session_factory

    try:
        await asyncio.sleep(settings.goal_continuation_interval_sec)
    except asyncio.CancelledError:
        logger.info("[goal] 续跑在间隔窗口被取消 session=%s", session_id)
        return

    async with async_session_factory() as db:
        try:
            session = await session_service.get_session(db, session_id)
            if session is None or not _goal_active(session):
                return
            if (session.goal_turns_used or 0) >= settings.goal_max_continuation_turns:
                return
            max_turns = settings.goal_max_continuation_turns
            next_n = (session.goal_turns_used or 0) + 1
            todo_summary = await _goal_todo_summary(db, session_id)
            prompt = (
                "[系统提醒] 会话目标尚未完成，请继续朝目标推进。\n\n"
                f"当前目标：{session.goal_text}\n"
                f"已自动续跑：{next_n}/{max_turns} 轮\n\n"
                f"{todo_summary}"
                "继续执行剩余工作；若目标已实际达成，请调用 goal_complete 工具标记完成并给出总结。"
            )
            user_msg = await message_service.create_message(
                db, session_id=session_id,
                sender_type=SenderType.USER.value,
                msg_type=MsgType.TEXT.value,
                content={"text": prompt, "goal_continuation": True,
                         "goal_turn": next_n, "goal_max_turns": max_turns},
                broadcast=True,
            )
            turn_id_new = await turn_service.create_turn(
                db, session_id=session_id, user_message_id=user_msg.id,
            )
            await _patch_message(user_msg.id, turn_id=turn_id_new)
            await _patch_session(session_id, goal_turns_used=next_n)
            logger.info("[goal] 续跑 turn=%s 创建（第 %d/%d 轮）session=%s", turn_id_new, next_n, max_turns, session_id)
            await broadcast(session_id, {
                "event": "goal.continued",
                "payload": {"turn_id": turn_id_new, "prev_turn_id": prev_turn_id, "turns_used": next_n},
            })

            async def _run():
                async with async_session_factory() as s:
                    try:
                        await start_turn(s, turn_id=turn_id_new)
                        await s.commit()
                    except Exception:
                        await s.rollback()
                        logger.warning("[goal] 续跑 turn=%s 执行异常", turn.id, exc_info=True)
                        try:
                            await turn_service.update_turn_status(s, turn_id_new, "failed", summary="续跑执行异常", completed=True)
                            await s.commit()
                            await broadcast_turn_updated(session_id, turn_id_new, "failed")
                        except Exception:
                            pass

            _turn_tasks[turn_id_new] = asyncio.get_event_loop().create_task(_run())
        except Exception:
            await db.rollback()
            logger.warning("[goal] 续跑 turn 创建失败 session=%s", session_id, exc_info=True)


async def _emit_goal_exhausted(db: AsyncSession, session_id: int, turn_id: int) -> None:
    """续跑轮次耗尽：落一条系统提示消息并广播 goal.stopped（幂等由调用方保证单次触发）。"""
    max_turns = settings.goal_max_continuation_turns
    try:
        await message_service.create_message(
            db, session_id=session_id, turn_id=turn_id,
            sender_type=SenderType.SYSTEM.value,
            msg_type=MsgType.TEXT.value,
            content={"text": f"目标续跑已达上限 {max_turns} 轮，已停止。可重新设定目标或手动继续。", "goal_stopped": True},
        )
    except Exception:
        logger.debug("[goal] 耗尽提示消息写入失败(非阻塞)", exc_info=True)
    await broadcast(session_id, {
        "event": "goal.stopped",
        "payload": {"turn_id": turn_id, "reason": "max_turns", "max_turns": max_turns},
    })


async def start_turn(db: AsyncSession, *, turn_id: int,
                     attachments: list[dict] | None = None,
                     reasoning_effort: str | None = None,
                     mode: str | None = None,
                     existing_task_id: int | None = None,
                     force_direct: bool = False,
                     # plan-166-767: 请求携带的权威模型（切换模型后立即发送时优先），
                     # 避免 PATCH 与 POST 竞态导致后端仍用旧模型处理该轮。
                     model_id: int | None = None) -> dict:
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
                step_res = await db.execute(_select(_Task).where(
                    _Task.parent_task_id.in_(group_ids),
                ))
                step_ids = [st.id for st in step_res.scalars().all() if st.status == "proposed"]
                if step_ids:
                    await _patch_task_bulk(step_ids, status="cancelled", is_hidden=True)
                await _patch_task_bulk(group_ids, status="cancelled", is_hidden=True)
                logger.info("[engine] turn=%s 作废旧提案 groups=%s", turn_id, group_ids)
            # plan-95: 同步关闭旧 turn 的待确认态——前端 refreshTasks 以 turn 状态为
            # 展示守卫，旧 turn 不再是 awaiting_confirmation 后旧卡无法复活
            stale_turn_res = await db.execute(_select(_Turn).where(
                _Turn.session_id == session_id,
                _Turn.status == "awaiting_confirmation",
                _Turn.id < turn_id,
            ))
            stale_turns = list(stale_turn_res.scalars().all())
            if stale_turns:
                await _patch_turn_cancel(stale_turns, [t.id for t in stale_turns])
                stale_turn_ids.update(t.id for t in stale_turns)
            if stale_groups or stale_turns:
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

        await broadcast(session_id, {"event": "turn.started", "payload": {"turn_id": turn_id, "session_id": session_id}})

        # v38 (plan-482): 删除系统预拆分——是否分步、分几步完全由主代理通过
        # todo_write 自主决定（系统提示词已明确该职责）。此前由 evaluate_complexity +
        # decompose_request 生成 group/steps，该链路长期失败且违背"决策权在模型"原则。
        # plan-166-767: 有效模型优先级 = 请求携带 model_id → session.model_id → main_agent.model_id。
        # 记录来源，便于验证「切换后立即发送用新模型」；is_multimodal/摘要窗口/注入全部基于同一 effective_model_id。
        _model_source = "request" if model_id else ("session" if session.model_id else "agent")
        effective_model_id = model_id or session.model_id or main_agent.model_id
        logger.info(
            "[engine] turn=%s effective_model_id=%s source=%s",
            turn_id, effective_model_id, _model_source,
        )
        from app.models.registry import get_model_registry
        from app.persistence.models.model_reg import Model
        selected_model = await db.get(Model, effective_model_id) if effective_model_id else None
        task_initial_status = "running"

        # 请求级任务：确认后复用原 request；普通 turn 才创建新的 request。
        if existing_task_id is not None:
            main_task = await task_service.get_task(db, existing_task_id)
            if main_task is None or main_task.session_id != session_id or main_task.turn_id != turn_id:
                return {"ok": False, "error": "任务不存在或不属于当前 turn"}
            await _patch_task(existing_task_id, status="running", is_hidden=False)
        else:
            _main_task_id = await task_service.create_task(
                db, session_id=session_id, turn_id=turn_id,
                title=(user_text.strip().replace("\n", " ") or "任务")[:60],
                description=user_text, kind="request", status=task_initial_status,
            )
            main_task = await task_service.get_task(db, _main_task_id)  # async 只读对象
        # v9: 立即提交（写引擎单写线程内完成；前端任务面板/右上角卡片实时拉取）
        # get_task/create_task 已写引擎提交，此处不再需要 async 主 db commit。
        await broadcast(session_id, {
            "event": "task.updated",
            "payload": {"task_id": main_task.id, "status": task_initial_status},
        })

        # 1. 主上下文
        # v15: 多模态模型时图片附件直接注入用户消息（AI 直接看图，不再走工具猜测路径）
        _is_multimodal = bool(getattr(selected_model, "is_multimodal", False)) if selected_model else False
        # plan-156-739: 诊断——附件含图片但模型未标记多模态时记录，定位"AI 只收到元信息"
        # 类问题（图片直注入 + 工具结果回填双路径都会因 multimodal=False 被跳过）。
        if not _is_multimodal and attachments:
            _has_img = any(isinstance(a, dict) and a.get("type") == "image" for a in attachments)
            if _has_img:
                logger.warning(
                    "[engine] turn=%s model=%s is_multimodal=False 但附件含图片，图片仅注入路径（不直注入/不回填）",
                    turn_id, getattr(selected_model, "name", "?") if selected_model else "?",
                )
        # plan-147-674: 实际暴露给模型的工具名集合（模式白名单 + ta3 伪装层过滤），
        # 供附件引导文案降级——避免提示词引导模型调用不存在的工具
        # plan-230-1144 M2: 白名单外置到 permission_profile_service（内置 4 模式 +
        # 用户自定义模式），支持模式自定义与自由权限分配；旧硬编码仅作兜底
        try:
            from app.services import permission_profile_service as _pps
            _mode_whitelist = _pps.resolve_tools(
                mode or "default", {t.name for t in tool_registry.all()},
            )
        except Exception:
            logger.debug("[engine] turn=%s 权限配置解析失败，回退硬编码白名单", turn_id, exc_info=True)
            _mode_whitelist = _READONLY_TOOLS if mode == "readonly" else (_PLAN_TOOLS if mode == "plan" else None)
        _available_tools = {t.name for t in tool_registry.for_agent(_mode_whitelist)}
        if selected_model is not None and getattr(selected_model, "api_format", "") == "ta3":
            from app.models.providers.ta3_mcp import is_mcp_tool
            from app.models.providers.ta3_tool_aliases import TO_TA3
            # MCP 工具名（mcp_<server>_<tool>）动态生成、不在静态映射表内，单独放行——
            # 它们经 ta3_mcp 改名为 ta3 风格伪装名后照常下发给模型。
            _available_tools = {
                n for n in _available_tools if n in TO_TA3 or is_mcp_tool(n)
            }
        _type_states = await load_subagent_type_states(db)
        _allow_subagents = bool(_type_states.get("explore", True) or _type_states.get("general", True))
        # plan-644: plan 模式收集本会话此前各轮计划需求全集并注入（多轮迭代
        # 零丢失的机制保证）；非 plan 模式不注入、零开销
        _plan_history = ""
        # 沙箱模式解析（与 run_agent_loop 同口径：项目配置 > 全局设置 > 默认）
        _eff_sandbox = "workspace-write"
        try:
            from app.services import config_service
            _eff = await config_service.effective_config(db, project_path=workspace)
            _eff_sandbox = str(_eff.get("sandbox_mode") or "workspace-write")
            if _eff_sandbox == "workspace-write" and settings.sandbox_mode != "workspace-write":
                _eff_sandbox = settings.sandbox_mode
        except Exception:
            logger.debug("[engine] turn=%s 读取沙箱模式失败，用 workspace-write", turn_id, exc_info=True)
        # 完全访问模式（danger-full-access）：免审批、直接执行——
        # 不注入规划/审阅模式说明，工具全量放行（用户要求"完全访问"不再被规划流程限制）
        _full_access = _eff_sandbox == "danger-full-access"

        if mode == "plan" and not _full_access and settings.plan_history_inject_chars > 0:
            _plan_history = await _collect_plan_history(db, session, workspace)
            if _plan_history:
                logger.info("[engine] turn=%s 注入 Plan History %d 字符", turn_id, len(_plan_history))
        bundle = await build_main_context(
            db, agent=main_agent, session=session, project=project, turn=turn,
            user_message=user_text, attachments=attachments,
            multimodal=_is_multimodal,
            enable_subagents=_allow_subagents,
            plan_history=_plan_history,
            # plan-671: 目标激活时 Current Goal 段为持久目标，本轮消息降级为 Current Task
            goal={"text": session.goal_text, "turns_used": session.goal_turns_used or 0}
            if (session.goal_status or "none") == "active" and session.goal_text else None,
            available_tools=_available_tools,
            effective_model_id=effective_model_id,
        )

        # 命令模式：注入模式指令（/chat 只读、/plan 先规划后执行）；完全访问下跳过。
        # plan-230-1144 M2: 自定义模式的提示词从配置读取（resolve_hint），
        # 内置模式沿用 _MODE_HINTS。
        _mode_hint = ""
        if not _full_access:
            if mode in _MODE_HINTS:
                _mode_hint = _MODE_HINTS[mode]
            else:
                try:
                    from app.services import permission_profile_service as _pps
                    _mode_hint = _pps.resolve_hint(mode or "default")
                except Exception:
                    logger.debug("[engine] turn=%s 自定义提示词读取失败", turn_id, exc_info=True)
        if _mode_hint:
            if mode == "plan":
                # plan-95: 提示词含 {session_id}/{turn_id} 占位符——文档按 turn 唯一命名，
                # 同一会话多次规划不再互相覆盖（此前固定会话名导致解析到上一任务旧文档）
                _mode_hint = _mode_hint.format(session_id=session_id, turn_id=turn_id)
            bundle.instruction = (_mode_hint + "\n\n" + bundle.instruction).strip() if bundle.instruction else _mode_hint

        # 2. 工具 schemas（按模式过滤 + 子代理工具；完全访问 = 全量工具）
        # plan-230-1144 M1.3: 先按模式取内置工具集，再统一走一次 MCP 注入。
        # plan-230-1144 M2: 白名单统一经 resolve_tools（内置 4 模式 + 自定义模式）；
        # 只读类判定经 is_readonly_like（MCP 只放行只读工具的依据）。
        try:
            from app.services import permission_profile_service as _pps
            _schema_whitelist = _pps.resolve_tools(
                mode or "default", {t.name for t in tool_registry.all()},
            )
            _mode_readonly = _pps.is_readonly_like(mode or "default") and not _full_access
            # 设置页白名单勾选对 MCP 工具同样生效（None = 不限制，见服务层口径说明）
            _mcp_allowed = _pps.mcp_allowed_tools(mode or "default")
        except Exception:
            logger.debug("[engine] turn=%s 权限配置解析失败，回退硬编码", turn_id, exc_info=True)
            _schema_whitelist = (
                _READONLY_TOOLS if mode == "readonly"
                else (_PLAN_TOOLS if mode == "plan" else None)
            )
            _mode_readonly = mode in ("readonly", "plan")
            _mcp_allowed = None
        if _full_access:
            tool_schemas = tool_registry.all_schemas()
        else:
            tool_schemas = tool_registry.all_schemas(_schema_whitelist)
        await _inject_mcp_tools(
            db, main_agent, tool_schemas, turn_id,
            readonly_only=_mode_readonly,
            allowed=_mcp_allowed,
        )
        # v38 (plan-482): 系统不再预拆分子任务，是否分步由主代理 todo_write 自主决定。
        # v20: 把 spawn_subagent/collect_results 暴露给主代理——
        # 需要并行调研时主代理自行 spawn 探索任务并拿回结论，再串行实现。
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
                # plan-248-1258 M6: 子代理继承的关键上下文（沙箱/权限模式）
                "sandbox_mode": _eff_sandbox,
                "permission_mode": mode,
                "context_summary": _plan_history or "",
            },
            # plan-547: 每次 LLM 调用前 drain 运行中注入的用户消息
            injected_inputs_provider=lambda: drain_injected_inputs(turn_id),
        )

        # /plan：方案文档完成后自动生成拆分提案，确认前不执行方案中的业务修改。
        # v2.2 (plan-88): 仅当方案文档真实存在且 AI 正常返回（kind=message）时才生成
        # 提案；未生成文档 / AI 异常结束时 turn 置 failed 并提示，不再广播 task.proposed
        # ——前端计划卡（pendingSplit）由 task.proposed 驱动，因此不会出现"无文档也弹卡"。
        if mode == "plan" and settings.plan_mode_auto_split:
            async def _fail_plan_turn(reason: str, user_hint: str) -> dict:
                """plan 阶段失败收尾：turn 置 failed + 任务失败 + 落一条用户可读错误。"""
                await turn_service.update_turn_status(
                    db, turn_id, "failed", summary=f"{reason}：{user_hint[:120]}", completed=True,
                )
                try:
                    await task_service.update_task_status(db, main_task.id, "failed",
                                                          note=f"{reason}: {user_hint[:120]}")
                    await broadcast(session_id, {"event": "task.updated",
                                                 "payload": {"task_id": main_task.id, "status": "failed"}})
                except Exception:
                    logger.debug("[engine] plan 失败任务状态更新失败(非阻塞)", exc_info=True)
                try:
                    await message_service.create_message(
                        db, session_id=session_id, turn_id=turn_id,
                        sender_type=SenderType.AGENT.value, sender_id=main_agent.id,
                        msg_type=MsgType.ERROR.value,
                        content={"text": f"{reason}，任务已终止。{user_hint} 请重试或检查执行过程后再次发送。",
                                 "agent_name": main_agent.name},
                        buffered=True,
                    )
                except Exception:
                    logger.debug("[engine] plan 失败提示消息写入失败(非阻塞)", exc_info=True)
                await _flush_turn_buffer(session_id, turn_id)
                await db.commit()
                await broadcast_turn_updated(session_id, turn_id, "failed")
                return {"ok": True, "failed": True, "reason": reason}

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
                return await _fail_plan_turn("计划文档未生成", _plan_reason)

            # v38 (plan-482): 不再由系统拆分步骤——方案文档只交付用户确认。
            # 步骤在用户确认后由主代理读文档、用 todo_write 自主重建（见
            # execute_confirmed_plan）。此处仅标记待确认并记录实际文档路径。
            # plan-644: 计划字段持久化（turns.plan_doc_path/plan_status）--
            # 前端卡片恢复与多轮 Plan History 需求全集注入的数据源；
            # 同会话旧 proposed 方案随之标记 superseded（被本轮取代）。
            _plan_rel = str(_plan_path.relative_to(Path(workspace).resolve()).as_posix())
            await _patch_turn(turn_id, plan_doc_path=_plan_rel, plan_status="proposed")
            _stamp_plan_doc(workspace, _plan_rel, "proposed")
            await _supersede_stale_proposed(db, session_id, turn_id, workspace)
            await _patch_task(main_task.id, status="awaiting_confirmation")

            # v0.3.1 (plan-190-898): 规划阶段完成第一时间落库 PLAN 消息并提交（用户要求：
            # 计划卡预览在规划阶段完成后显示在底部，在此刻完成写库，确认后再继续在后面刷消息/写库）。
            # 作为规划阶段的收尾项落库，保证物理时间线上排在规划总结之后、执行阶段之前。
            try:
                await message_service.create_message(
                    db, session_id=session_id, turn_id=turn_id,
                    sender_type=SenderType.SYSTEM.value, msg_type=MsgType.PLAN.value,
                    content={"plan_doc_path": _plan_rel, "plan_status": "proposed",
                             "text": "方案已生成，请确认后执行", "agent_name": main_agent.name},
                    buffered=True,  # 立即广播 message.created，前端流式底部即刻渲染计划卡
                )
            except Exception:
                logger.warning("[engine] PLAN 预览消息落库失败", exc_info=True)

            await turn_service.update_turn_status(
                db, turn_id, "awaiting_confirmation",
                summary="方案文档已生成，等待用户确认", completed=True,
            )
            await _flush_turn_buffer(session_id, turn_id)
            await db.commit()

            # v26: 广播实际命中的计划文档路径与待确认状态
            await broadcast(session_id, {
                "event": "task.proposed",
                "payload": {
                    "turn_id": turn_id,
                    "request_task_id": main_task.id,
                    "group_task_id": 0,
                    "reasons": [],
                    "plan_doc_path": _plan_rel,
                    "steps": [],
                },
            })
            await broadcast_turn_updated(session_id, turn_id, "awaiting_confirmation")
            return {"ok": True, "awaiting_confirmation": True, "task_id": main_task.id}

        # 4. turn 完成：取消是用户中断，不应伪装成失败；先落库再广播。
        summary = out.text or ""
        _is_cancel = cancel_event.is_set() or out.kind in ("cancelled", "interrupted")
        final_status = (
            "interrupted" if _is_cancel
            else "completed" if out.kind == "message" else "failed"
        )
        # v45: 失败原因必须对用户可见——agent_loop 的 out.error / 已有摘要择一，兜底通用文案。
        _fail_reason = ((out.error or "").strip() or summary.strip()
                        or "任务执行失败，未返回具体原因")[:500]
        await turn_service.update_turn_status(
            db, turn_id, final_status,
            summary=(summary[:500] or _fail_reason) if final_status != "interrupted" else (summary[:500] or "用户中断"),
            completed=True,
        )
        await _flush_turn_buffer(session_id, turn_id)
        await db.commit()
        await broadcast_turn_updated(session_id, turn_id, final_status)
        # v45: 失败单独事件 turn.failed（此前与中断共用 turn.interrupted，前端按"用户停止"
        # 静默处理，用户看不到任何报错——"无报错就终止任务"的根因）。
        await broadcast(session_id, {
            "event": ("turn.failed" if final_status == "failed"
                      else "turn.completed" if final_status == "completed"
                      else "turn.interrupted"),
            "payload": {"turn_id": turn_id, "status": final_status, "summary": summary,
                        "error": _fail_reason if final_status == "failed" else None,
                        "artifact_ids": out.artifact_ids,
                        "session_id": session_id},
        })
        # v45: 失败兜底——确认消息流内已有可见的错误消息，否则补一条，杜绝静默终止。
        if final_status == "failed":
            try:
                from sqlalchemy import select as _select
                from app.persistence.models.message import Message as _Message
                _err_rows = (await db.execute(
                    _select(_Message.id).where(
                        _Message.turn_id == turn_id,
                        _Message.msg_type == MsgType.ERROR.value,
                    ).limit(1)
                )).first()
                if _err_rows is None:
                    await message_service.create_message(
                        db, session_id=session_id, turn_id=turn_id, thread_id=None,
                        sender_type=SenderType.AGENT.value, sender_id=main_agent.id,
                        msg_type=MsgType.ERROR.value,
                        content={"text": _fail_reason, "agent_name": main_agent.name},
                        buffered=True,
                    )
                    await _flush_turn_buffer(session_id, turn_id)
                    await db.commit()
                    await broadcast_turn_updated(session_id, turn_id, final_status)
            except Exception:
                logger.debug("[engine] turn 失败兜底提示写入失败(非阻塞)", exc_info=True)
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

        # 目标模式（plan-671）：turn 完成而目标未完成 → 服务端自动续跑。
        # goal_complete 工具与 engine 共用同一 db 连接且已 commit，此处重取会话
        # 拿到最新 goal_status；耗尽判定仅对本 turn 即续跑 turn 生效（手动消息
        # 不触发耗尽提示，避免重复刷屏）。
        _refreshed = await session_service.get_session(db, session_id)
        if _refreshed is not None:
            session = _refreshed
        _is_goal_turn = bool(user_msg_content.get("goal_continuation"))
        if _should_continue_goal(session, final_status, cancel_event):
            _turn_tasks[turn_id] = asyncio.get_event_loop().create_task(
                _continue_goal_turn(session_id, turn_id)
            )
        elif (_is_goal_turn and _goal_active(session)
              and (session.goal_turns_used or 0) >= settings.goal_max_continuation_turns):
            await _emit_goal_exhausted(db, session_id, turn_id)

        # 5. 记忆提取（异步，不阻塞；受设置中心「AI 主动生成记忆」开关控制）
        if out.kind == "message" and (summary or user_text):
            await _spawn_memory_extract(
                db,
                session_id=session_id,
                turn_id=turn_id,
                prompt=user_text or "",
                summary=summary or "",
                model_id=getattr(session, "model_id", None),
            )

        # plan_restore_after_turn 粘性机制已移除：切换后的权限模式保持到手动切换

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
        discard_injected_inputs(turn_id)  # plan-547: 丢弃残留注入项
        cleanup(session_id)
        # v1.1: 无条件广播 session.completed，驱动前端摘除左侧转圈（无论成败）
        # v37: 携带最新活动时间，侧栏排序同步上移（此前仅整表刷新才更新）
        try:
            await broadcast_session_completed(session_id, db)
        except Exception:
            pass
        # v2.2 (plan-88): 低频 checkpoint GC
        try:
            await _maybe_run_checkpoint_gc(db, locals().get("workspace"))
        except Exception:
            pass


async def cancel_turn(turn_id: int) -> bool:
    """立即发出取消信号，数据库终态由唯一后台 finalizer 完成。"""
    ev = _cancel_events.get(turn_id)
    manager = _turn_managers.get(turn_id)
    main_task = _turn_tasks.get(turn_id)
    if ev is None and manager is None and main_task is None:
        # 非运行态也返回真实存在性，避免前端把不存在的 turn 当成已取消。
        from app.persistence.database import async_session_factory
        async with async_session_factory() as db:
            return await turn_service.get_turn(db, turn_id) is not None

    if ev is not None:
        ev.set()
    if manager is not None:
        # cancel_all 会取消子任务，但不在 HTTP 请求中等待其数据库收尾。
        pending = [h.task for h in manager._handles.values()
                   if h.task is not None and not h.task.done()]
        for task in pending:
            task.cancel()
    if main_task is not None and main_task is not asyncio.current_task() and not main_task.done():
        main_task.cancel()

    existing = _cancel_finalizers.get(turn_id)
    if existing is None or existing.done():
        finalizer = asyncio.create_task(_finalize_cancelled_turn(turn_id))
        _cancel_finalizers[turn_id] = finalizer
    return True


async def _finalize_cancelled_turn(turn_id: int) -> None:
    """取消后台收尾：短事务（写引擎单写线程）、条件更新、有限重试，不阻塞 Stop 请求。"""
    from datetime import datetime, timezone
    from app.persistence.database import run_write_locked
    from app.persistence.models.agent import Agent

    async def _persist_cancel(db):
        status_before = None  # 兼容旧签名；读取与写入统一在写引擎事务内完成

        def patch(s):
            from sqlalchemy import update as _update
            from app.persistence.models.turn import Turn
            t = s.get(Turn, turn_id)
            if t is None:
                return None
            session_id = int(t.session_id)
            terminal = ("completed", "failed", "cancelled", "interrupted", "rolled_back")
            if t.status in terminal:
                return {"session_id": session_id, "active": False, "summary": t.summary or ""}
            t.status = "interrupted"
            t.summary = t.summary or "用户中断"
            t.completed_at = t.completed_at or datetime.now(timezone.utc).isoformat()
            s.execute(
                _update(Agent).where(Agent.turn_id == turn_id, Agent.status == "running")
                .values(status="terminated")
            )
            s.commit()
            return {"session_id": session_id, "active": True, "summary": t.summary}

        result = await run_write_locked(patch, label=f"turn.cancel.{turn_id}")
        if result and result.get("active"):
            await task_service.cancel_turn_tasks(db, int(result["session_id"]), turn_id)
        return result

    try:
        result = await _persist_cancel(None)
        if result and result.get("active"):
            session_id = int(result["session_id"])
            await broadcast_turn_updated(session_id, turn_id, "interrupted")
            await broadcast(session_id, {
                "event": "turn.interrupted",
                "payload": {"turn_id": turn_id, "status": "interrupted",
                            "summary": result.get("summary") or "用户中断",
                            "session_id": session_id},
            })
            await broadcast_session_completed(session_id)
    except asyncio.CancelledError:
        logger.info("[cancel] finalizer cancelled turn=%s", turn_id)
    except Exception:
        logger.exception("[cancel] finalizer failed turn=%s", turn_id)
        # finalizer 失败不能把 turn 留在 running；下一次健康检查/重启自愈仍可处理。
    finally:
        _cancel_finalizers.pop(turn_id, None)


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


def _stamp_plan_doc(workspace: str, rel_path: str | None, status: str,
                    extra: str = "") -> None:
    """plan-644: 往方案文档头部插入状态元数据行（模型与人可读的冗余标注）。

    状态真值以 turns.plan_status 为准；本函数任何失败仅记日志不抛出。
    仅接受工作区内 ai/ 目录下 chatcoder-plan*.md，防数据库异常值指向任意文件。
    同一状态重复标注幂等（头部已有同状态行则跳过）。
    """
    if not workspace or not rel_path:
        return
    root = Path(workspace).resolve()
    try:
        target = (root / rel_path).resolve()
        if not (target.is_file() and root in target.parents
                and target.parent.name == "ai"
                and target.name.startswith("chatcoder-plan")
                and target.suffix.lower() == ".md"):
            return
        text = target.read_text(encoding="utf-8", errors="replace")
        if text.startswith(f"<!-- plan-status: {status} "):
            return
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        line = f"<!-- plan-status: {status} @ {ts}{extra} -->"
        target.write_text(line + "\n" + text, encoding="utf-8")
    except OSError:
        logger.debug("[engine] 方案文档状态标注失败(非阻塞) path=%s", rel_path, exc_info=True)


def _read_plan_document_exact(workspace: str, rel_path: str | None) -> str:
    """plan-644: 按 turn 持久化的精确路径读取方案文档（上限 16000 字符）。

    用于确认执行阶段：用户确认哪份文档就执行哪份，不再受 mtime 扫描竞态
    （"确认旧方案却执行了更新的新文档"）影响。路径限制在工作区内且命中
    chatcoder-plan*.md 约定；不满足或读取失败返回空串（调用方回退旧逻辑）。
    """
    if not workspace or not rel_path:
        return ""
    root = Path(workspace).resolve()
    try:
        target = (root / rel_path).resolve()
        if not (target.is_file() and root in target.parents
                and target.name.startswith("chatcoder-plan")
                and target.suffix.lower() == ".md"):
            return ""
        return target.read_text(encoding="utf-8", errors="replace")[:16000]
    except OSError:
        return ""


async def _supersede_stale_proposed(db: AsyncSession, session_id: int,
                                    new_turn_id: int, workspace: str) -> None:
    """plan-644: 新一轮方案文档解析成功 -> 同会话其余 proposed 计划标记为已被取代。

    旧待确认方案在用户发起下一轮规划时失效（配合 start_turn 开头 v26 的
    turn 级作废逻辑：turn.status 已置 cancelled，此处补 plan_status 语义）。
    数据库为状态真值源；文档头元数据行写失败不阻塞。
    """
    from sqlalchemy import select as _select

    from app.persistence.models.turn import Turn as _Turn
    res = await db.execute(_select(_Turn).where(
        _Turn.session_id == session_id,
        _Turn.id != new_turn_id,
        _Turn.plan_status == "proposed",
    ))
    stale = list(res.scalars().all())
    if stale:
        for t in stale:
            _stamp_plan_doc(workspace, t.plan_doc_path, "superseded", f" by turn {new_turn_id}")
        await _patch_turn_bulk([t.id for t in stale], plan_status="superseded")
        logger.info("[engine] turn=%s 取代旧待确认方案 turns=%s",
                    new_turn_id, [t.id for t in stale])


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


async def execute_confirmed_plan(db: AsyncSession, *, turn_id: int) -> dict:
    """v38 (plan-482): 用户确认方案文档后执行——清理分析阶段清单，按文档重建执行清单。

        docstring 注明）。核心改动：
   1. 不再读取任何 group/steps 记录——系统已不生成它们；
   2. 确认瞬间广播空 todo.updated，清掉规划阶段遗留的调研清单（目标流程第 4 步）；
   3. 把方案文档正文注入 instruction，要求主代理自行重建执行清单后逐步执行
      （目标流程第 5 步），每完成一步用 todo_write 标记。
   plan-1075: 注入 Plan History 历史未完结轮摘要（状态行+用户需求行，不含正文）
   ——多轮迭代中确认执行的方案应已由 AI 合并历史未完成项，执行阶段让模型
   仍能看到全集，发现遗漏时显式指出差异而非静默丢弃。
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
        _session_model_id = getattr(session, "model_id", None) if session else None
        project = await project_service.get_project(db, session.project_id) if session and session.project_id else None
        if session is None or project is None:
            return {"ok": False, "error": "session/project not found"}
        request_task = (await db.execute(
            select(Task).where(Task.turn_id == turn_id, Task.kind == "request").order_by(Task.id.asc()).limit(1)
        )).scalars().first()
        main_agent = (await db.execute(select(Agent).where(Agent.kind == "main").limit(1))).scalars().first()
        if main_agent is None:
            return {"ok": False, "error": "主代理未初始化"}
        workspace = _resolve_workspace(session, project)
        manager = get_subagent_manager(_session_id)
        _turn_managers[turn_id] = manager
        # plan-166-767: confirm 执行路径与 start_turn 对齐——按有效模型解析多模态/窗口。
        effective_model_id = _session_model_id or main_agent.model_id
        _model_source = "session" if _session_model_id else "agent"
        logger.info(
            "[engine:confirm] turn=%s effective_model_id=%s source=%s",
            turn_id, effective_model_id, _model_source,
        )
        from app.persistence.models.model_reg import Model as _Model
        _sel_model = await db.get(_Model, effective_model_id) if effective_model_id else None
        _is_multimodal = bool(getattr(_sel_model, "is_multimodal", False)) if _sel_model else False
        await broadcast(_session_id, {"event": "turn.started", "payload": {"turn_id": turn_id, "session_id": _session_id}})
        if request_task:
            await _patch_task(request_task.id, status="running")

        # 目标流程第 4 步：确认执行即清空分析阶段的调研清单，
        # 主代理随后按方案文档重建执行清单（避免旧清单与新执行清单混杂）。
        await broadcast(session.id, {"event": "todo.updated", "payload": {
            "turn_id": turn_id, "todos": [], "persisted": False,
        }})

        # 用户原始请求（主代理上下文继承）——优先主请求任务，缺则最近 user 消息
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
                        Message.deleted == False,  # noqa: E712 问题14: 排除已回滚软删
                    ).order_by(Message.id.desc()).limit(1)
                )
                _um = _ures.scalars().first()
                if _um and isinstance(_um.content, dict):
                    original_request = str(_um.content.get("text") or "")
            except Exception:
                original_request = ""

        # —— 主代理按文档自主执行 ——
        # plan-644: 优先按 turn 持久化路径精确读取（用户确认哪份就执行哪份，
        # 消除"确认旧方案却执行了 mtime 更新的新文档"竞态）；旧数据无路径时
        # 回退 mtime 最新逻辑。上限 12000 -> 16000（与 MAX_TOOL_OUTPUT_CHARS 对齐）。
        plan_doc = _read_plan_document_exact(workspace, getattr(turn, "plan_doc_path", None))
        if not plan_doc:
            plan_doc = _read_plan_document(workspace, session.id)[:16000]
        # plan-1075: 历史未完结轮摘要（状态行+用户需求行，不含正文；排除本 turn
        # ——其文档已全文注入）。失败非阻塞：拿不到就不注入，行为同旧版。
        _open_summary = ""
        try:
            _open_summary = await _collect_plan_history(
                db, session, workspace, summary_only=True, exclude_turn_id=turn_id,
            )
        except Exception:
            logger.debug("[engine:confirm] Plan History 摘要注入失败(非阻塞)", exc_info=True)
        _open_section = (
            "\n\n【历史未完结计划轮（Plan History 摘要）】\n" + _open_summary
            + "\n若发现上方方案文档未覆盖其中的历史未完成需求，在总结中明确指出差异，不要静默丢弃。"
            if _open_summary.strip() else ""
        )
        bundle = await build_main_context(
            db, agent=main_agent, session=session, project=project, turn=turn,
            user_message=original_request,
            multimodal=_is_multimodal,
            effective_model_id=effective_model_id,
        )
        bundle.instruction = (
            "用户已确认以下方案文档，现在按它执行：\n\n"
            f"【方案文档】\n{plan_doc or '（文档读取失败，请依据用户原始请求执行）'}\n\n"
            "【执行要求】\n"
            "1. 先用 todo_write 按文档重建执行清单——分几步、每步粒度由你决定，"
            "以可独立验证的交付物为界；之前的调研清单已清空。\n"
            "2. 再串行推进：自行阅读、编辑、验证。\n"
            "3. 每完成一步立即用 todo_write 把该项标记为 completed、下一项标记为"
            " in_progress，不要事后批量补标。\n"
            "4. 执行中发现划分不合理时，先更新清单再继续。\n"
            "5. 完成后用一段话总结改动与验证结果。"
            + _open_section
        )
        main_tools = tool_registry.all_schemas()
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
            logger.warning("[engine] 确认执行路径 MCP 工具加载失败(非阻塞)", exc_info=True)
        _type_states = await load_subagent_type_states(db)
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
            multimodal=_is_multimodal,
            subagent_context={
                "manager": mgr, "session": session, "project": project,
                "cancel_event": cancel_event,
                "main_task_id": request_task.id if request_task else None,
                "model_id": effective_model_id,
            },
        )

        # —— 收尾：状态与产物 ——
        summary = out.text or ""
        _is_cancel2 = cancel_event.is_set() or out.kind in ("cancelled", "interrupted")
        final_status = (
            "interrupted" if _is_cancel2
            else "completed" if out.kind == "message" else "failed"
        )
        # v45: 失败原因必须可见（与主路径同口径）
        _fail_reason2 = ((out.error or "").strip() or summary.strip()
                         or "任务执行失败，未返回具体原因")[:500]
        await turn_service.update_turn_status(
            db, turn_id, final_status,
            summary=(summary[:500] or _fail_reason2) if final_status != "interrupted" else (summary[:500] or "用户中断"),
            completed=True,
        )
        # plan-644: 执行成功 -> 计划生命周期收口 done；失败/中断保持 confirmed
        # （已确认但未完成，其中未完成需求由后续轮次的 Plan History 继承）。
        if out.kind == "message":
            await _patch_turn(turn_id, plan_status="done")
            _stamp_plan_doc(workspace, getattr(turn, "plan_doc_path", None), "done")
        if request_task is not None:
            await _patch_task(request_task.id, status=(
                "cancelled" if final_status == "interrupted" else (
                    "done" if out.kind == "message" else "failed")))
            if out.kind == "message" and out.artifact_ids:
                await task_service.attach_artifacts(db, request_task.id, out.artifact_ids)
        await _flush_turn_buffer(_session_id, turn_id)
        await db.commit()
        # v0.3.1: 使用提前缓存的标量 _session_id/_session_model_id，禁止在 commit 之后点 session.id
        await broadcast_turn_updated(_session_id, turn_id, final_status)
        # v45: 失败单独事件 turn.failed 并携带原因（此前与中断共用 turn.interrupted，
        # 前端按"用户停止"静默处理 → 用户看不到报错）。
        await broadcast(_session_id, {
            "event": ("turn.failed" if final_status == "failed"
                      else "turn.completed" if final_status == "completed"
                      else "turn.interrupted"),
            "payload": {"turn_id": turn_id, "status": final_status, "summary": summary,
                        "error": _fail_reason2 if final_status == "failed" else None,
                        "artifact_ids": out.artifact_ids, "session_id": _session_id},
        })
        if request_task is not None:
            await broadcast(_session_id, {"event": "task.updated", "payload": {
                "task_id": request_task.id, "status": request_task.status}})
        if out.kind == "message" and (summary or (request_task.title if request_task else "")):
            await _spawn_memory_extract(
                db,
                session_id=_session_id,
                turn_id=turn_id,
                prompt=(request_task.title if request_task else "") or "",
                summary=summary or "",
                model_id=_session_model_id,
            )
        # plan_restore_after_turn 粘性机制已移除：确认执行后的权限模式保持到手动切换
        return {"ok": True, "kind": out.kind, "summary": summary}
    except Exception as exc:
        logger.exception("确认后执行失败 turn=%s", turn_id)
        try:
            await db.rollback()
            await _flush_turn_buffer(_session_id, turn_id)
            await turn_service.update_turn_status(db, turn_id, "failed", summary=str(exc)[:500], completed=True)
            # v45: 异常路径同样要可见——补错误消息 + turn.failed 事件，杜绝静默终止。
            try:
                await message_service.create_message(
                    db, session_id=_session_id or 0, turn_id=turn_id, thread_id=None,
                    sender_type=SenderType.AGENT.value, sender_id=main_agent.id,
                    msg_type=MsgType.ERROR.value,
                    content={"text": f"执行异常: {str(exc)[:200]}", "agent_name": main_agent.name},
                    buffered=True,
                )
                await _flush_turn_buffer(_session_id, turn_id)
            except Exception:
                logger.debug("[engine] 确认执行异常提示写入失败(非阻塞)", exc_info=True)
            await db.commit()
            await broadcast_turn_updated(_session_id or 0, turn_id, "failed")
            await broadcast(_session_id or 0, {
                "event": "turn.failed",
                "payload": {"turn_id": turn_id, "status": "failed", "summary": "",
                            "error": f"执行异常: {str(exc)[:200]}", "artifact_ids": [],
                            "session_id": _session_id or 0},
            })
        except Exception:
            pass
        return {"ok": False, "error": str(exc)}
    finally:
        _running_turns.discard(turn_id)
        _cancel_events.pop(turn_id, None)
        _turn_managers.pop(turn_id, None)
        discard_injected_inputs(turn_id)  # plan-547: 丢弃残留注入项
        cleanup(_session_id or 0)
        if _session_id:
            try:
                await broadcast_session_completed(_session_id, db)
            except Exception:
                pass
        try:
            await _maybe_run_checkpoint_gc(db, locals().get("workspace"))
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
    # 子代理创建 + step 状态（写引擎单写线程，一次事务返回创建后的 Agent id）
    from app.persistence.database import run_write_locked

    def _persist_agent(s):
        from app.persistence.models.agent import Agent as _Agent
        from app.persistence.models.task import Task as _Task
        sa = _Agent(
            kind="sub", name=step.title[:40], model_id=effective_model_id,
            session_id=session.id, turn_id=turn_id, parent_agent_id=main_agent.id,
        )
        s.add(sa)
        s.flush()
        aid = sa.id
        st = s.get(_Task, task_id)
        if st is not None:
            st.agent_id = aid
            st.status = "running"
            st.note = None
        s.commit()
        return aid

    agent_id = await run_write_locked(_persist_agent, label="subagent.retry.create")
    step = await task_service.get_task(db, task_id)  # async 只读对象（保持后续 step 属性读取）
    sub_agent.id = agent_id
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
                Message.deleted == False,  # noqa: E712 问题14: 排除已回滚软删
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
        await _patch_task(step.id, status="done",
                          note=(handle.summary or "")[:300] or None)
    else:
        await _patch_task(step.id, status="failed",
                          note=(handle.error if handle else "子代理未返回结果")[:500])
    step = await task_service.get_task(db, task_id)
    await broadcast(session.id, {"event": "task.updated", "payload": {
        "task_id": step.id, "status": step.status, "note": step.note or ""}})
    return {"ok": ok, "status": step.status}


async def _spawn_memory_extract(
    db,
    *,
    session_id: int,
    turn_id: int,
    prompt: str = "",
    summary: str = "",
    model_id: int | None = None,
    auto_memory_enabled: bool | None = None,
) -> None:
    """异步提取记忆（D8）。不阻塞 turn 返回。

    受设置中心「AI 主动生成记忆」开关控制：关闭时（settings.auto_memory_enabled=False）
    跳过提取。
    修复：
    1. 优先使用当前会话绑定模型构造 Provider；若无则回退系统已配置活跃模型，最后回退全局默认配置。
    2. 聚合「用户意图」与「执行结果/技术解答」，传给提炼模型。
    3. 传入 Provider 对应的真实 model 名称，避免 model="" 导致 400 报错。
    """
    if auto_memory_enabled is None:
        auto_memory_enabled = settings.auto_memory_enabled
    if not auto_memory_enabled:
        return

    context_parts: list[str] = []
    if prompt and prompt.strip():
        context_parts.append(f"【用户需求/任务】:\n{prompt.strip()[:1500]}")
    if summary and summary.strip():
        context_parts.append(f"【执行结果/结论】:\n{summary.strip()[:3000]}")
    if not context_parts:
        return
    text = "\n\n".join(context_parts)

    async def _extract():
        try:
            from app.models.registry import get_model_registry
            from app.models.schemas import ChatMessage, ChatRequest
            from app.persistence.database import async_session_factory
            from app.persistence.models.model_reg import Model

            provider = None
            model_name = ""
            registry = get_model_registry()

            async with async_session_factory() as s:
                # plan-270-1358: ta3 x-ws-id 需要会话工作目录指纹（其它 Provider 忽略）
                from app.persistence.models.message import Session as _SessionRow
                _sess_row = await s.get(_SessionRow, session_id)
                _ws_dir = (resolve_workspace_root(getattr(_sess_row, "workspace_root", None))
                           if _sess_row is not None else None)

                # 1. 尝试从 session 的 model_id 解析
                if model_id:
                    m = await s.get(Model, model_id)
                    if m:
                        p, _ = await registry.get_provider_for_model(s, m)
                        if p:
                            provider = p
                            model_name = m.name

                # 2. 尝试从系统活跃模型中解析
                if provider is None:
                    from sqlalchemy import select
                    res = await s.execute(select(Model).where(Model.is_active == True).limit(5))
                    active_models = list(res.scalars().all())
                    for am in active_models:
                        p, _ = await registry.get_provider_for_model(s, am)
                        if p:
                            provider = p
                            model_name = am.name
                            break

            # 3. 兜底尝试全局默认 Provider
            if provider is None:
                provider = registry.get_default_provider()
                model_name = getattr(provider, "model", "") or getattr(settings, "default_llm_model", "")

            if provider is None:
                logger.debug("[memory] 无可用 ModelProvider，跳过记忆提取")
                return

            req = ChatRequest(
                messages=[
                    ChatMessage(
                        role="system",
                        content=(
                            "You are a project memory extraction assistant. Analyze the user request and execution outcome below to extract 1-3 durable, useful facts about this specific project (such as architectural patterns, directory structure conventions, library choices, pitfalls, or domain rules). "
                            'Format as a strict JSON array like [{"kind": "fact", "text": "...", "importance": 0.8}]. '
                            "'kind' must be one of: 'fact', 'convention', 'pitfall', 'decision'. "
                            "'importance' must be a number between 0.65 and 1.0. "
                            "Return [] if the content is trivial, transient, or lacks general project knowledge."
                        ),
                    ),
                    ChatMessage(role="user", content=text[:4500]),
                ],
                model=model_name or getattr(provider, "model", "") or "default",
                workspace_dir=_ws_dir,  # plan-270-1358: ta3 x-ws-id
            )
            resp = await provider.chat(req)
            import json as _json
            import re as _re
            raw = resp.content or ""
            match = _re.search(r"\[.*\]", raw, _re.DOTALL)
            if not match:
                return
            data = _json.loads(match.group(0))
            if not isinstance(data, list) or not data:
                return
            from app.services.memory_service import save_memories
            async with async_session_factory() as s:
                count = await save_memories(s, session_id=session_id, turn_id=turn_id, memories=data)
                await s.commit()
                if count > 0:
                    logger.info("[memory] Turn %s 自动生成并保存了 %d 条记忆", turn_id, count)
        except Exception:
            logger.warning("[memory] 记忆提取异常(非阻塞)", exc_info=True)

    try:
        asyncio.create_task(_extract())
    except Exception:
        try:
            asyncio.get_event_loop().create_task(_extract())
        except Exception:
            pass
