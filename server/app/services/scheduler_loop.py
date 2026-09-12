"""定时任务调度循环（plan-230-1144 M1.1）。

此前 `scheduled_tasks` 表只有 CRUD、没有任何消费者，用户创建的任务永不执行。
本模块提供 tick 循环：到点领取 → 以用户身份注入指令 → 创建并后台执行 turn →
广播 `scheduled.triggered`（事件类型在 gateway/schemas.py 中早已声明但无发送方）。

设计要点：
- **原子领取**：`claim_due` 在单写事务内读-改-写并推进 next_run_at，两个 tick
  或多个实例不会重复触发同一行；
- **错过策略**：触发点过期超过 5 分钟视为错过，`missed_policy=skip` 只推进不执行，
  `run_once` 补跑一次（重启后不雪崩式补历史）；
- **失败不阻塞**：单个任务执行异常只记录 last_status=failed，循环继续；
- **会话失效自愈**：绑定的会话已删除时自动禁用任务，避免每 tick 空转。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timezone

from app.core.config import settings

logger = logging.getLogger(__name__)

# tick 周期（秒）。cron 最小粒度是分钟，30s 足够且不空转。
TICK_INTERVAL = 30

_task: asyncio.Task | None = None
_stop: asyncio.Event | None = None


async def _fire_task(snapshot: dict) -> None:
    """执行一个到点的定时任务：建用户消息 + 建 turn + 后台跑 engine.start_turn。"""
    from app.core.enums import MsgType, SenderType
    from app.persistence.database import async_session_factory
    from app.services import (
        message_service,
        scheduled_service,
        session_service,
        turn_service,
    )

    task_id = snapshot["id"]
    session_id = int(snapshot["session_id"])
    prompt = str(snapshot.get("prompt") or "").strip()
    name = str(snapshot.get("name") or f"定时任务 #{task_id}")

    if not prompt:
        await scheduled_service.mark_run(
            task_id, status="failed", next_run_at=snapshot.get("next_run_at"),
            error="指令内容为空",
        )
        logger.warning("[scheduler] 任务 %s 无指令内容，跳过", task_id)
        return

    async with async_session_factory() as db:
        session = await session_service.get_session(db, session_id)
        if session is None:
            # 会话已被删除：自动禁用任务，避免每 tick 空转
            await scheduled_service.update_scheduled(db, task_id, enabled=False, last_status="orphaned")
            logger.info("[scheduler] 任务 %s 的会话 %s 不存在，已自动禁用", task_id, session_id)
            return

        content = {"text": prompt, "scheduled_task_id": task_id, "scheduled_task_name": name}
        user_msg = await message_service.create_message(
            db, session_id=session_id,
            sender_type=SenderType.USER.value,
            msg_type=MsgType.TEXT.value,
            content=content,
            broadcast=False,
        )
        turn_id = await turn_service.create_turn(
            db, session_id=session_id, user_message_id=user_msg.id,
        )
        await message_service.patch_message(user_msg.id, turn_id=turn_id)

        # 广播：用户消息 + turn 创建 + 定时触发事件（前端据此弹提示并刷新会话）
        try:
            from app.gateway.ws import manager as ws_manager
            from app.services.message_service import _to_out
            await ws_manager.broadcast(session_id, {
                "event": "message.created",
                "payload": {"msg": _to_out(user_msg).model_dump()},
            })
            await ws_manager.broadcast(session_id, {
                "event": "scheduled.triggered",
                "payload": {
                    "task_id": task_id, "session_id": session_id,
                    "turn_id": turn_id, "name": name,
                    "next_run_at": snapshot.get("next_run_at"),
                },
            })
        except Exception:
            logger.debug("[scheduler] 触发广播失败(非阻塞) task=%s", task_id, exc_info=True)

        logger.info("[scheduler] 任务 %s(%s) 已触发 turn=%s", task_id, name, turn_id)

    # 后台执行 turn（与 turns.py 的 create_turn 同一范式：独立会话 + 异常关 turn）
    async def _run():
        from app.orchestration import engine
        from app.persistence.database import async_session_factory
        from app.services import turn_service

        async with async_session_factory() as s:
            try:
                await engine.start_turn(s, turn_id=turn_id)
                await s.commit()
                await scheduled_service.mark_run(
                    task_id, status="ok", next_run_at=snapshot.get("next_run_at"),
                )
            except asyncio.CancelledError:
                await s.rollback()
                await scheduled_service.mark_run(
                    task_id, status="cancelled", next_run_at=snapshot.get("next_run_at"),
                )
                raise
            except Exception as exc:
                await s.rollback()
                logger.exception("[scheduler] 任务 %s turn=%s 执行失败", task_id, turn_id)
                try:
                    await turn_service.update_turn_status(
                        s, turn_id, "failed", summary="定时任务执行异常", completed=True,
                    )
                    await s.commit()
                except Exception:
                    logger.debug("[scheduler] 失败态落库异常", exc_info=True)
                await scheduled_service.mark_run(
                    task_id, status="failed", next_run_at=snapshot.get("next_run_at"),
                    error=str(exc)[:300],
                )
            finally:
                from app.orchestration.engine import _turn_tasks
                _turn_tasks.pop(turn_id, None)

    task = asyncio.create_task(_run())
    from app.orchestration.engine import _turn_tasks
    _turn_tasks[turn_id] = task
    return task


async def _tick() -> int:
    """一次扫描。返回触发的任务数。"""
    from app.persistence.database import async_session_factory
    from app.services import scheduled_service

    async with async_session_factory() as db:
        claimed = await scheduled_service.claim_due(db)

    fired = 0
    for snapshot in claimed:
        if snapshot.get("missed") and snapshot.get("missed_policy", "skip") == "skip":
            logger.info(
                "[scheduler] 任务 %s 错过触发点（应用未运行），按 skip 策略跳过",
                snapshot["id"],
            )
            continue
        try:
            await _fire_task(snapshot)
            fired += 1
        except Exception:
            logger.exception("[scheduler] 任务 %s 触发失败", snapshot.get("id"))
    return fired


async def _loop() -> None:
    assert _stop is not None
    logger.info("[scheduler] 定时任务调度循环启动（间隔 %ss）", TICK_INTERVAL)
    while not _stop.is_set():
        try:
            await _tick()
        except Exception:
            # 单次 tick 异常不能终止循环（DB 抖动、迁移未完成等）
            logger.exception("[scheduler] tick 异常")
        # 等待间隔或停止信号（停止信号先到时立即退出）
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(_stop.wait(), timeout=TICK_INTERVAL)
    logger.info("[scheduler] 定时任务调度循环已停止")


async def start() -> None:
    """启动调度循环（幂等）。由 main.py 的 lifespan 调用。"""
    global _task, _stop
    if _task is not None and not _task.done():
        return
    if not getattr(settings, "scheduler_enabled", True):
        logger.info("[scheduler] 调度循环已被配置禁用")
        return
    _stop = asyncio.Event()
    _task = asyncio.create_task(_loop())


async def stop() -> None:
    """停止调度循环。"""
    global _task, _stop
    if _stop is not None:
        _stop.set()
    if _task is not None:
        try:
            await asyncio.wait_for(_task, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            _task.cancel()
        except Exception:
            logger.debug("[scheduler] 停止时异常", exc_info=True)
    _task = None
    _stop = None


async def run_task_now(task_id: int) -> dict:
    """手动立即触发一次（不影响 next_run_at 排程），供"试跑"按钮使用。"""
    from app.persistence.database import async_session_factory
    from app.services import scheduled_service

    async with async_session_factory() as db:
        st = await scheduled_service.get_scheduled(db, task_id)
        if st is None:
            raise KeyError(f"定时任务 {task_id} 不存在")
        snapshot = {
            "id": st.id, "session_id": st.session_id, "name": st.name,
            "cron": st.cron, "prompt": st.prompt, "missed": False,
            "missed_policy": st.missed_policy or "skip",
            "next_run_at": st.next_run_at,
        }
    await _fire_task(snapshot)
    return {"ok": True, "task_id": task_id, "triggered_at": datetime.now(timezone.utc).isoformat()}
