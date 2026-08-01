"""v0.3: 并行层 DAG 调度器。

- SessionScheduler.run_ready:取入度0+pending+plan_confirmed 任务,
  asyncio.gather 并行执行(每任务一个 run_agent_loop,各自独立 db session)。
- 每任务完成 → 标 done/in_review/blocked → 释放下游入度 → 下游 ready 则继续调度。
- 通过 _running 集合避免同任务并发重复执行。

注意:run_agent_loop 内部会用 async_session_factory() 开自己的事务,
调度器本身不持有跨任务共享 db。
"""
import asyncio
import logging
from typing import TYPE_CHECKING

from app.core.config import settings
from app.gateway.ws import manager as ws_manager
from app.persistence.database import async_session_factory
from app.services import session_service, task_service

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# SQLite 单写者模型:并发写易锁冲突,验证模式串行执行;PostgreSQL 生产保持并行
_SQLITE_SERIAL = settings.database_url.startswith("sqlite")


class SessionScheduler:
    """每个 session 一个调度器实例。"""

    def __init__(self, session_id: int) -> None:
        self.session_id = session_id
        self._running: set[int] = set()  # 正在执行的 task_id
        self._lock = asyncio.Lock()
        self._scheduled = False
        # v0.9: task_id → cancel_event,用于中断运行中的任务
        self._cancel_events: dict[int, asyncio.Event] = {}

    async def run_ready(self) -> dict:
        """触发一轮调度:并行执行所有当前可调度任务,完成后再触发下游。

        返回 {"scheduled": [task_id...], "results": [...]}
        """
        async with self._lock:
            if self._scheduled:
                logger.debug("session %s 调度已在进行,跳过", self.session_id)
                return {"scheduled": [], "results": [], "note": "already_running"}
            self._scheduled = True

        try:
            return await self._run_loop()
        finally:
            async with self._lock:
                self._scheduled = False

    async def _run_loop(self) -> dict:
        scheduled_all: list[int] = []
        results: list[dict] = []

        # 检查 plan_confirmed
        async with async_session_factory() as db:
            from app.persistence.models.message import Session
            session = await db.get(Session, self.session_id)
            if session is None:
                return {"scheduled": [], "results": [], "error": "session not found"}
            if not session.plan_confirmed:
                return {
                    "scheduled": [], "results": [],
                    "error": "plan_not_confirmed",
                    "note": "请先确认 Leader 的任务拆解",
                }
            ready = await task_service.list_ready_tasks(db, self.session_id)

        # 即使没有 ready 任务,也可能所有任务已 done → 检查并发完成总结
        if not ready:
            await self._maybe_announce_completion()
            return {"scheduled": [], "results": [], "note": "no_ready_tasks"}

        # SQLite 验证模式串行执行,避免并发写锁;PostgreSQL 生产可并行
        if _SQLITE_SERIAL:
            for t in ready:
                scheduled_all.append(t.id)
                r = await self._run_one(t.id)
                results.append({"task_id": t.id, **r})
        else:
            tasks = [asyncio.create_task(self._run_one(t.id)) for t in ready]
            for t in ready:
                scheduled_all.append(t.id)
            done_results = await asyncio.gather(*tasks, return_exceptions=True)
            for tid, r in zip(ready, done_results):
                if isinstance(r, Exception):
                    results.append({"task_id": tid.id, "ok": False, "error": str(r)})
                else:
                    results.append({"task_id": tid.id, **r})

        # 递归触发下游(完成任务的下游可能 ready)
        await self._schedule_dependents([t.id for t in ready])

        # 全部任务完成后,Leader 在群里发"交付完成"总结
        await self._maybe_announce_completion()

        return {"scheduled": scheduled_all, "results": results}

    async def _maybe_announce_completion(self) -> None:
        """若会话所有任务都已 done,Leader 在群里发总结消息(含成品路径)。"""
        try:
            async with async_session_factory() as db:
                tasks = await task_service.list_tasks(db, self.session_id)
                if not tasks:
                    return
                # 还有未完成的 → 不发
                unfinished = [t for t in tasks if t.status not in ("done", "rejected", "blocked")]
                if unfinished:
                    return
                # 本轮已发过总结 → 不重复发(用 session.shared_context 记录标记)
                from app.persistence.models.message import Session
                session = await db.get(Session, self.session_id)
                if session is None:
                    return
                ctx = session.shared_context or {}
                if ctx.get("completion_announced"):
                    return

                # v1.0: 收集所有 artifact_ids 后一次性 IN 查询（修复 N+1）
                from app.persistence.models.task import Artifact
                from sqlalchemy import select
                all_artifact_ids = []
                for t in tasks:
                    if t.artifact_ids:
                        all_artifact_ids.extend(t.artifact_ids)
                all_artifacts = []
                if all_artifact_ids:
                    res = await db.execute(
                        select(Artifact).where(Artifact.id.in_(all_artifact_ids))
                    )
                    all_artifacts = list(res.scalars().all())

                from app.orchestration.chat_handler import _generate_completion_summary
                summary_text = await _generate_completion_summary(
                    db, tasks=tasks, artifacts=all_artifacts,
                )

                from app.core.enums import MsgType, SenderType
                # v3：团队概念已移除，不再有 Leader；用默认名
                leader_id = None
                leader_name = "Assistant"

                await session_service.create_message(
                    db,
                    session_id=self.session_id,
                    sender_type=SenderType.AGENT,
                    sender_id=leader_id,
                    msg_type=MsgType.TEXT,
                    content={"text": summary_text, "agent_name": leader_name},
                    thread_id=None,
                )
                # 标记已发,避免重复
                ctx["completion_announced"] = True
                session.shared_context = ctx
                await db.commit()
                logger.info("会话 %s 全部完成,已发交付总结", self.session_id)

                # v2: 广播会话完成事件,通知前端 isRunning 归零
                try:
                    from app.gateway.ws import manager as ws_manager
                    await ws_manager.broadcast(self.session_id, {
                        "event": "session.completed",
                        "payload": {"session_id": self.session_id},
                    })
                except Exception:
                    logger.debug("session.completed 广播失败(可能无连接)", exc_info=True)
        except Exception:
            logger.exception("发送完成总结失败 session=%s", self.session_id)

    async def _run_one(self, task_id: int) -> dict:
        """v1.0: 执行单个任务(独立 db session)，支持瞬态错误自动重试。"""
        async with self._lock:
            if task_id in self._running:
                return {"ok": False, "error": "already_running"}
            self._running.add(task_id)
            # v0.9: 为本任务创建 cancel event
            cancel_event = asyncio.Event()
            self._cancel_events[task_id] = cancel_event

        # v1.0: 自动重试配置
        _MAX_RETRIES = 3
        _RETRYABLE_KEYWORDS = ("timeout", "timed out", "429", "rate limit", "connection", "ECONNRESET", "503", "502")

        try:
            last_error = ""
            for attempt in range(_MAX_RETRIES):
                try:
                    # 重试前重置任务状态为 pending，避免 run_agent_loop 读到 blocked
                    if attempt > 0:
                        async with async_session_factory() as db:
                            await task_service.update_task_status(db, task_id, "pending")
                            await db.commit()
                        logger.info("[调度] task=%s 重试前重置状态为 pending (第%d次)", task_id, attempt + 1)
                    async with async_session_factory() as db:
                        result = await self._dispatch_task(db, task_id, cancel_event)
                        await db.commit()
                    return result
                except Exception as e:
                    last_error = str(e)
                    # 判断是否为瞬态错误（网络超时、模型限流等）
                    is_transient = any(kw in last_error.lower() for kw in _RETRYABLE_KEYWORDS)
                    if not is_transient or attempt >= _MAX_RETRIES - 1:
                        raise
                    wait_sec = 2 ** (attempt + 1)  # 指数退避: 2, 4, 8
                    logger.warning(
                        "[调度] task=%s 瞬态错误(第%d次)，%ds 后重试: %s",
                        task_id, attempt + 1, wait_sec, last_error[:150],
                    )
                    await asyncio.sleep(wait_sec)
            return {"ok": False, "error": last_error}
        except Exception as e:
            logger.exception("调度执行任务 %s 异常", task_id)
            err_text = str(e)[:200]
            async with async_session_factory() as db:
                await task_service.update_task_status(db, task_id, "blocked", note=f"调度异常:{err_text}")
                await db.commit()
            # v4.6: 失败上屏——把 blocked 原因推给前端(任务卡 + 主群消息 + agent.status)
            try:
                from app.orchestration.agent_runtime import (
                    _broadcast_agent_status,
                    _broadcast_task_updated,
                    _emit_main_card,
                )
                from app.persistence.models.agent import Agent

                agent_id = None
                agent_name = "Agent"
                task_title = f"任务#{task_id}"
                async with async_session_factory() as db:
                    t = await task_service.get_task(db, task_id)
                    if t:
                        task_title = t.title or task_title
                        agent_id = t.assigned_agent_id
                        if agent_id:
                            ag = await db.get(Agent, agent_id)
                            if ag:
                                agent_name = ag.name
                await _broadcast_task_updated(self.session_id, task_id, "blocked", note=f"调度异常:{err_text}")
                if agent_id:
                    async with async_session_factory() as db:
                        await _emit_main_card(
                            db, self.session_id, agent_id, agent_name,
                            task_id, task_title, status="blocked",
                            note=f"执行失败:{err_text}",
                        )
                    await _broadcast_agent_status(
                        self.session_id, agent_id, agent_name,
                        status="failed", task_id=task_id,
                    )
            except Exception:
                logger.debug("blocked 通知广播失败(非阻塞)", exc_info=True)
            return {"ok": False, "error": str(e)}
        finally:
            async with self._lock:
                self._running.discard(task_id)
                self._cancel_events.pop(task_id, None)

    async def cancel_task(self, task_id: int) -> bool:
        """v0.9: 中断指定任务。返回是否找到了运行中的任务。"""
        async with self._lock:
            ev = self._cancel_events.get(task_id)
        if ev is None:
            return False
        ev.set()
        logger.warning("[调度] session=%s task=%s 已请求中断", self.session_id, task_id)
        return True

    async def cancel_all(self) -> int:
        """v0.9: 中断当前 session 所有运行中任务。返回中断数量。"""
        async with self._lock:
            events = list(self._cancel_events.values())
        for ev in events:
            ev.set()
        if events:
            logger.warning("[调度] session=%s 已请求中断全部 %d 个任务", self.session_id, len(events))
        return len(events)

    async def _dispatch_task(self, db, task_id: int, cancel_event: asyncio.Event | None = None) -> dict:
        """读取任务+agent+模板,调用 run_agent_loop。"""
        from app.orchestration.agent_runtime import run_agent_loop
        from app.persistence.models.agent import Agent

        logger.info("[调度] _dispatch_task 开始 task=%s", task_id)
        task = await task_service.get_task(db, task_id)
        if task is None:
            return {"ok": False, "error": "task not found"}
        if not task.assigned_agent_id:
            await task_service.update_task_status(db, task_id, "blocked", note="未分配执行人")
            try:
                from app.orchestration.agent_runtime import _broadcast_task_updated
                await _broadcast_task_updated(self.session_id, task_id, "blocked", note="未分配执行人")
            except Exception:
                logger.debug("no_assignee 广播失败(非阻塞)", exc_info=True)
            return {"ok": False, "error": "no_assignee"}
        agent = await db.get(Agent, task.assigned_agent_id)
        if agent is None:
            return {"ok": False, "error": "agent not found"}
        logger.info("[调度] task=%s agent=%s(%s) 准备执行", task_id, agent.name, agent.id)

        # v0.9: 执行前创建回滚快照(记录 git HEAD 基线)
        try:
            from app.orchestration.rollback import create_snapshot
            from app.persistence.models.message import Session
            session = await db.get(Session, self.session_id)
            from app.core.config import resolve_workspace_root
            ws = resolve_workspace_root(getattr(session, "workspace_root", None) if session else None)
            await create_snapshot(db, session_id=self.session_id, task_id=task_id, workspace=ws)
        except Exception:
            logger.debug("创建回滚快照失败(非阻塞)", exc_info=True)

        # v3：团队/模板概念已移除，系统提示词由 prompts 分层统一构建
        system_prompt = ""
        agent_role = ""
        whitelist: list[str] | None = None

        logger.info("[调度] task=%s 调用 run_agent_loop...", task_id)
        out = await run_agent_loop(
            db,
            session_id=self.session_id,
            task_id=task_id,
            agent_id=agent.id,
            agent_name=agent.name,
            agent_role=agent_role,
            system_prompt=system_prompt,
            template_whitelist=whitelist,
            cancel_event=cancel_event,
        )
        logger.info("[调度] task=%s 执行完成 kind=%s", task_id, out.kind)
        return {
            "ok": out.kind in ("message", "done"),
            "kind": out.kind,
            "artifacts": out.artifact_ids,
            "error": out.error,
        }

    async def _schedule_dependents(self, completed_ids: list[int]) -> None:
        """v1.0: 完成 completed_ids 后,迭代调度新 ready 的下游任务。

        改进: 批量处理 + while 循环替代递归，避免深度无限制。
        """
        pending_ids = list(completed_ids)
        max_depth = 50  # 安全阀: 防止无限循环
        depth = 0

        while pending_ids and depth < max_depth:
            depth += 1
            # v1.0: 批量处理所有 completed_ids，共用一个 session
            newly_all = []
            async with async_session_factory() as db:
                for cid in pending_ids:
                    newly = await task_service.decrement_indegree_and_pick_ready(
                        db, self.session_id, cid
                    )
                    newly_all.extend(newly)
                await db.commit()

            if not newly_all:
                break

            if _SQLITE_SERIAL:
                results = [await self._run_one(t.id) for t in newly_all]
            else:
                tasks_coro = [asyncio.create_task(self._run_one(t.id)) for t in newly_all]
                results = await asyncio.gather(*tasks_coro, return_exceptions=True)

            # 收集成功的任务 ID 作为下一轮的 completed_ids
            pending_ids = [
                t.id for t, r in zip(newly_all, results)
                if not isinstance(r, Exception) and r.get("ok", False)
            ]

        if depth >= max_depth:
            logger.warning("会话 %s _schedule_dependents 达到深度限制 %d", self.session_id, max_depth)


# ───────────────────────── 调度器注册表 ─────────────────────────

_schedulers: dict[int, SessionScheduler] = {}


def get_scheduler(session_id: int) -> SessionScheduler:
    if session_id not in _schedulers:
        _schedulers[session_id] = SessionScheduler(session_id=session_id)
    return _schedulers[session_id]
