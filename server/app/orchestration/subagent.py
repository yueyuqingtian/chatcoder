"""子代理管理（v2：主代理按需 spawn，独立上下文 + 结果收集）。

简化实现：spawn 启动后台 agent_loop 任务；结果按 subagent_id 缓存于内存，
主代理通过 collect_results 工具轮询获取。持久化子代理线程消息于 messages(thread_id=agent.id)。
"""
import asyncio
import logging
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from app.orchestration.agent_loop import run_agent_loop
from app.orchestration.tools.registry import tool_registry

logger = logging.getLogger(__name__)


@dataclass
class SubagentHandle:
    agent_id: int
    task: object = None
    status: str = "running"  # running / done / failed
    summary: str = ""
    error: str = ""


class SubagentManager:
    """会话级子代理管理器。"""

    def __init__(self, session_id: int) -> None:
        self.session_id = session_id
        self._handles: dict[int, SubagentHandle] = {}

    def spawn(self, db, *, agent, turn_id: int, task, handoff_summary: str,
              context_bundle, tool_schemas: list[dict], workspace: str,
              cancel_event: asyncio.Event | None = None, token_budget: int | None = None) -> int:
        """异步启动子代理 agent_loop，返回 subagent id。"""
        handle = SubagentHandle(agent_id=agent.id)
        self._handles[agent.id] = handle

        async def _run():
            try:
                # v9: 子任务启动即广播 in_progress（提交任务创建 + 状态），
                # 前端任务面板/右上角卡片实时展示新任务的拆分步骤与执行情况。
                # 此前子任务创建后从不更新状态，前端看不到子任务步骤执行进度。
                await _sync_task_status(db, self.session_id, task.id, "in_progress", None)
                out = await run_agent_loop(
                    db, session_id=self.session_id, turn_id=turn_id,
                    agent=agent, context_messages=context_bundle.to_messages(),
                    tool_schemas=tool_schemas, workspace=workspace,
                    cancel_event=cancel_event, token_budget=token_budget,
                )
                handle.status = "done" if out.kind == "message" else "failed"
                handle.summary = out.text or ""
                handle.error = out.error or ""
                await _sync_task_status(
                    db, self.session_id, task.id,
                    "done" if handle.status == "done" else "failed",
                    (out.text or "")[:300] or None,
                )
            except Exception as e:
                handle.status = "failed"
                handle.error = str(e)
                await _sync_task_status(db, self.session_id, task.id, "failed", f"执行异常: {str(e)[:200]}")
                logger.exception("[subagent] %s 异常", agent.id)
            logger.info("[subagent] %s 完成 status=%s", agent.id, handle.status)

        handle.task = asyncio.create_task(_run())
        return agent.id

    def get(self, agent_id: int) -> SubagentHandle | None:
        return self._handles.get(agent_id)

    def results(self) -> list[dict]:
        """已完成的子代理结果列表。"""
        out = []
        for aid, h in self._handles.items():
            if h.status in ("done", "failed"):
                out.append({
                    "agent_id": aid, "status": h.status,
                    "summary": h.summary, "error": h.error,
                })
        return out

    def pending_count(self) -> int:
        return sum(1 for h in self._handles.values() if h.status == "running")


# 会话级 manager 注册表
_managers: dict[int, SubagentManager] = {}


def get_subagent_manager(session_id: int) -> SubagentManager:
    if session_id not in _managers:
        _managers[session_id] = SubagentManager(session_id)
    return _managers[session_id]


def cleanup(session_id: int) -> None:
    _managers.pop(session_id, None)


async def _sync_task_status(db, session_id: int, task_id: int, status: str, note: str | None) -> None:
    """更新子任务状态并广播，前端任务面板据此实时刷新步骤与执行情况。

    先提交（子任务与主代理共享 session，创建任务的 flush 需随 commit 落库，
    否则前端 refreshTasks 通过 HTTP 查询时看不到新任务），再广播 task.updated。
    """
    try:
        from app.orchestration.agent_events import broadcast
        from app.services import task_service
        await task_service.update_task_status(db, task_id, status, note=note)
        await db.commit()
        await broadcast(session_id, {
            "event": "task.updated",
            "payload": {"task_id": task_id, "status": status, "note": note or ""},
        })
    except Exception:
        logger.debug("[subagent] 子任务状态同步失败(非阻塞)", exc_info=True)
