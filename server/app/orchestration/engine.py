"""Turn 引擎（v2 总控）：一次用户消息 = 一个 turn。

流程：创建快照 → 构建主上下文 → 主代理 loop（可 spawn 子代理）→
收集结果 → turn 完成摘要 → 记忆提取（异步）→ 广播。
"""
import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import MsgType, SenderType
from app.orchestration.agent_events import broadcast, broadcast_turn_updated
from app.orchestration.agent_loop import run_agent_loop
from app.orchestration.context_manager import build_main_context, build_subagent_context
from app.orchestration.subagent import cleanup, get_subagent_manager
from app.orchestration.tools.registry import tool_registry
from app.services import audit_service, message_service, project_service, rollback_service, session_service, task_service, turn_service

logger = logging.getLogger(__name__)

# 会话级运行锁（SQLite 串行写）
_running_turns: set[int] = set()
_cancel_events: dict[int, asyncio.Event] = {}


async def start_turn(db: AsyncSession, *, turn_id: int,
                     attachments: list[dict] | None = None,
                     reasoning_effort: str | None = None) -> dict:
    """执行一个 turn。turn 与用户消息已由路由创建。"""
    turn = await turn_service.get_turn(db, turn_id)
    if turn is None:
        return {"ok": False, "error": "turn not found"}
    session_id = turn.session_id

    if turn_id in _running_turns:
        return {"ok": False, "error": "turn already running"}
    _running_turns.add(turn_id)
    cancel_event = asyncio.Event()
    _cancel_events[turn_id] = cancel_event

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

        # 用户消息原文
        user_msg = await message_service.get_message(db, turn.user_message_id) if turn.user_message_id else None
        user_text = str((user_msg.content or {}).get("text", "")) if user_msg else "(空消息)"

        # 0. 回滚快照（§4.10）
        workspace = _resolve_workspace(session, project)
        try:
            await rollback_service.create_turn_snapshot(
                db, session_id=session_id, turn_id=turn_id,
                workspace=workspace, user_message_id=turn.user_message_id,
            )
        except Exception:
            logger.debug("创建回滚快照失败(非阻塞)", exc_info=True)

        await broadcast(session_id, {"event": "turn.started", "payload": {"turn_id": turn_id}})

        # 1. 主上下文
        bundle = await build_main_context(
            db, agent=main_agent, session=session, project=project, turn=turn,
            user_message=user_text, attachments=attachments,
        )

        # 2. 工具 schemas（全量 + 子代理工具）
        tool_schemas = tool_registry.all_schemas()
        tool_schemas.append({
            "type": "function",
            "function": {
                "name": "spawn_subagent",
                "description": "Spawn a subagent to work on a separable subtask in an isolated context.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task_title": {"type": "string"},
                        "task_description": {"type": "string"},
                        "acceptance_criteria": {"type": "string"},
                    },
                    "required": ["task_title", "task_description"],
                },
            },
        })
        tool_schemas.append({
            "type": "function",
            "function": {
                "name": "collect_results",
                "description": "Collect the results (summaries) of all finished subagents.",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        })

        # 3. 运行主代理
        from app.orchestration.subagent import get_subagent_manager
        mgr = get_subagent_manager(session_id)
        out = await run_agent_loop(
            db, session_id=session_id, turn_id=turn_id,
            agent=main_agent, context_messages=bundle.to_messages(),
            tool_schemas=tool_schemas, workspace=workspace,
            cancel_event=cancel_event,
            reasoning_effort=reasoning_effort,
            subagent_context={
                "manager": mgr, "session": session, "project": project,
                "cancel_event": cancel_event,
            },
        )

        # 4. turn 完成
        summary = out.text or ""
        await turn_service.update_turn_status(
            db, turn_id,
            "completed" if out.kind == "message" else "failed",
            summary=summary[:500], completed=True,
        )
        await broadcast_turn_updated(session_id, turn_id, "completed" if out.kind == "message" else "failed")
        await broadcast(session_id, {
            "event": "turn.completed",
            "payload": {"turn_id": turn_id, "summary": summary, "artifact_ids": out.artifact_ids},
        })
        await audit_service.log(db, action="turn", session_id=session_id, turn_id=turn_id,
                                detail={"kind": out.kind, "tokens": 0})

        # 5. 记忆提取（异步，不阻塞）
        if out.kind == "message" and summary:
            _spawn_memory_extract(db, session_id=session_id, turn_id=turn_id, text=summary)

        # 6. 会话自动命名（首条消息）
        if not session.title and turn.user_message_id:
            await _auto_title(db, session, user_text)

        return {"ok": True, "kind": out.kind, "summary": summary}
    finally:
        _running_turns.discard(turn_id)
        _cancel_events.pop(turn_id, None)
        cleanup(session_id)


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


def _spawn_memory_extract(db, *, session_id: int, turn_id: int, text: str) -> None:
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
