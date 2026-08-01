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
                out = await run_agent_loop(
                    db, session_id=self.session_id, turn_id=turn_id,
                    agent=agent, context_messages=context_bundle.to_messages(),
                    tool_schemas=tool_schemas, workspace=workspace,
                    cancel_event=cancel_event, token_budget=token_budget,
                )
                handle.status = "done" if out.kind == "message" else "failed"
                handle.summary = out.text or ""
                handle.error = out.error or ""
            except Exception as e:
                handle.status = "failed"
                handle.error = str(e)
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
