"""工作流运行时的进程级服务：单例、后台调度循环与启动恢复（设计 §5.3、§6.8）。

- get_runtime()：进程内唯一的运行时，并发上限取自设置（全局 / 单会话 / 子工作流）；
- start_background()：启动恢复扫描，然后以固定节拍驱动调度（节点完成后的下一拍即可衔接下游）；
- stop_background()：应用关闭时停止调度循环（在途节点随进程结束，下次启动由恢复扫描转为可续跑）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from app.orchestration.workflow.runtime import WorkflowRuntime
from app.orchestration.workflow.scheduler import PoolLimits

logger = logging.getLogger(__name__)

TICK_INTERVAL_SEC = 1.0

_runtime: WorkflowRuntime | None = None
_loop_task: asyncio.Task[None] | None = None


def _limits_from_settings() -> PoolLimits:
    from app.core.config import settings

    return PoolLimits(
        global_limit=max(0, int(getattr(settings, "workflow_max_concurrent_global", 3))),
        per_session_limit=max(0, int(getattr(settings, "workflow_max_concurrent_per_session", 2))),
        per_flow_limit=max(1, int(getattr(settings, "workflow_max_parallel_nodes_per_flow", 2))),
    )


def get_runtime() -> WorkflowRuntime:
    global _runtime
    if _runtime is None:
        from app.orchestration.workflow.executor_subagent import SubagentNodeExecutor

        _runtime = WorkflowRuntime(
            SubagentNodeExecutor(),
            limits=_limits_from_settings(),
            notify=_notify_main_agent,
        )
    return _runtime


async def _notify_main_agent(session_id: int, text: str) -> None:
    """工作流关键状态变化 → 主代理（延迟导入引擎，避免循环依赖）。"""
    from app.orchestration.engine import notify_workflow

    await notify_workflow(session_id, text)


async def on_turn_stopped(session_id: int) -> int:
    """用户停止当前轮：会话内运行中的工作流暂停（保留进度，可继续）。返回暂停的程序数。"""
    return await get_runtime().pause_session_turn(session_id)


async def on_session_archived(session_id: int) -> int:
    """会话归档：运行中的工作流暂停（保留进度），恢复归档后可在工作流面板继续。"""
    return await get_runtime().pause_session_programs(
        session_id, kind="archived", reason="会话已归档，已保留进度，恢复会话后可继续"
    )


async def on_session_deleted(session_id: int) -> int:
    """会话被永久删除：未结束的工作流取消。"""
    return await get_runtime().cancel_session_programs(session_id)


def pool_snapshot() -> dict[str, object]:
    """并发池占用快照（界面展示用）。"""
    return get_runtime().pool_snapshot()


async def _loop(runtime: WorkflowRuntime) -> None:
    while True:
        try:
            # 并发上限每拍从设置读取：保存后下一拍即生效，调大时排队节点随本拍被调度
            runtime.pool.set_limits(_limits_from_settings())
            await runtime.tick()
        except asyncio.CancelledError:
            raise
        except Exception:  # 调度循环不能因单次异常停止
            logger.exception("[workflow] 调度循环异常，下一拍继续")
        await asyncio.sleep(TICK_INTERVAL_SEC)


async def start_background() -> None:
    global _loop_task
    runtime = get_runtime()
    try:
        recovered = await runtime.recover_after_restart()
        if recovered:
            logger.warning("[workflow] 启动恢复：%d 个运行被中断，已保留进度等待继续", recovered)
    except Exception:
        logger.exception("[workflow] 启动恢复扫描失败")
    if _loop_task is None or _loop_task.done():
        _loop_task = asyncio.create_task(_loop(runtime))


async def stop_background() -> None:
    global _loop_task
    if _loop_task is not None:
        _loop_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await _loop_task
        _loop_task = None


def describe_limits() -> dict[str, Any]:
    limits = _limits_from_settings()
    return {
        "global": limits.global_limit,
        "per_session": limits.per_session_limit,
        "per_flow": limits.per_flow_limit,
    }
