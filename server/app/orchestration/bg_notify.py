"""后台命令完成回报（plan-474-2105）。

旧实现里 `terminal_exec(waitForCompletion=false)` 的进程退出后只在日志里留一行，
会话完全不知道——只能靠模型自己反复轮询 `terminal_bg_status`，或者干脆漏掉结果。

这里把「进程退出」变成一条**主动投递**：
  1. `BgProcessRegistry.register(on_exit=on_bg_exit)` 在进程退出时回调一次；
  2. 短窗口内同一会话的多个完成事件合并成一条摘要（避免批跑命令刷屏）；
  3. 通过会话邮箱投递：会话忙 → 注入当前轮；空闲 → 自动拉起一轮把结果读进上下文。

plan-20261020-bgtask：
  · 模型已经从工具结果里看到终态的（terminal_bg_status 返回 running=false、terminal_bg_kill）不再回报——
    此前同一份结果会在消息流里出现两遍，空闲时还会白白叫醒一轮；
  · 用户在界面上手动停止的单独成类（BG_COMMAND_STOPPED）：只让会话知道，不叫醒空闲会话；
  · 落库消息带结构化条目 `bg_items`，前端据此渲染，不必从摘要文本里反向解析；
  · 进程启动 / 退出事件（bg.process.started / exited）由注册表即时广播，这里不再重复广播。

plan-20261025-bgturn：通知落库时必须带上命令所属的 turn（`mailbox.publish(turn_id=...)`）。
此前它恒为 None，而撤回只软删 `turn_id >= 目标轮` 的消息——用户「先停后台命令、再撤回该轮」之后，
那条「[后台命令已被用户停止]」永远留在消息流里（已比对数据库确认）。现在两个口子都堵上：
  · 落库带 turn_id，撤回时跟着一起软删；
  · 合并窗口内该轮已被撤回的，直接不投递。
"""
from __future__ import annotations

import asyncio
import logging

from app.orchestration.tools.bg_process import bg_process_registry, fmt_elapsed

logger = logging.getLogger(__name__)

# 同一会话的完成事件合并窗口（秒）
MERGE_WINDOW_S = 3.0
# 摘要里最多列出的条数（其余只报数量）
MAX_ITEMS = 5
# 摘要里单条命令的日志尾部长度
TAIL_CHARS = 600

_pending: dict[int, list[dict]] = {}
_timers: dict[int, asyncio.Task] = {}


def _format(items: list[dict]) -> str:
    """把一批完成事件写成主代理可读的摘要。"""
    head = (
        f"[后台命令完成] 共 {len(items)} 个后台命令已结束。"
        "请先看结果，再决定是否需要继续处理（日志可用 terminal_bg_status 按 shell_id 续读）。"
    )
    lines = [head]
    for it in items[:MAX_ITEMS]:
        rc = it.get("returncode")
        state = "退出码 0" if rc == 0 else f"退出码 {rc}"
        lines.append(
            f"\n- `{str(it.get('command') or '')[:160]}`（{it.get('shell_id')}）："
            f"{state}，耗时 {it.get('elapsed')}s"
        )
        tail = (it.get("tail") or "").strip()
        if tail:
            lines.append(f"  日志尾部：\n{tail[-TAIL_CHARS:]}")
    if len(items) > MAX_ITEMS:
        lines.append(f"\n（另有 {len(items) - MAX_ITEMS} 个已完成命令未列出，可用 terminal_bg_status 查询）")
    return "\n".join(lines)


def _format_stopped(items: list[dict]) -> str:
    """用户手动停止的后台命令：只是让主代理知道它们已不在运行，不需要处理。"""
    lines = [
        f"[后台命令已被用户停止] 用户在界面上停止了 {len(items)} 个后台命令。"
        "它们已不在运行，无需处理；之后如果还需要，请重新启动。"
    ]
    for it in items[:MAX_ITEMS]:
        lines.append(
            f"\n- `{str(it.get('command') or '')[:160]}`（{it.get('shell_id')}）："
            f"已运行 {fmt_elapsed(it.get('elapsed'))}"
        )
    if len(items) > MAX_ITEMS:
        lines.append(f"\n（另有 {len(items) - MAX_ITEMS} 个未列出）")
    return "\n".join(lines)


def _bg_items(items: list[dict]) -> list[dict]:
    """落库的结构化条目（消息流里的「后台命令已结束」行据此渲染）。"""
    return [
        {
            "shell_id": it.get("shell_id"),
            "command": str(it.get("command") or "")[:500],
            "returncode": it.get("returncode"),
            "elapsed": it.get("elapsed"),
            "tail": str(it.get("tail") or "")[-TAIL_CHARS:],
            "stopped_by": it.get("stopped_by"),
        }
        for it in items[:MAX_ITEMS]
    ]


async def _turn_revoked(turn_ids: set[int]) -> set[int]:
    """这些 turn 里已被撤回（rolled_back）的集合（plan-20261025-bgturn）。

    合并窗口（`MERGE_WINDOW_S`）内用户可能已经把那一轮撤回了：此时进程退出事件早晚会走到
    这里，若照旧投递，就会在消息流里重新冒出一条属于已撤销轮的「后台命令已被用户停止」。
    查库而不是只看内存标记——回滚可能发生在另一个请求里，也可能在窗口开启前。
    查询失败时返回空集（宁可多一条通知，也不能让正常通知消失）。
    """
    if not turn_ids:
        return set()
    try:
        from sqlalchemy import select

        from app.persistence.database import async_session_factory
        from app.persistence.models.turn import Turn

        async with async_session_factory() as db:
            res = await db.execute(
                select(Turn.id).where(
                    Turn.id.in_(list(turn_ids)),
                    Turn.status == "rolled_back",
                )
            )
            return {int(r) for r in res.scalars().all()}
    except Exception:  # noqa: BLE001 — 查不到就当作未撤回
        logger.warning("[bg.notify] 查询 turn 回滚状态失败 turns=%s", turn_ids, exc_info=True)
        return set()


def _batch_turn_id(items: list[dict]) -> int | None:
    """一批条目同属一轮时返回该 turn，否则 None（plan-20261025-bgturn）。

    落库消息只能带一个 turn_id，而合并窗口内可能混入不同轮的命令。同轮（绝大多数情况）
    落上归属，撤回那一轮时能一起软删；跨轮时宁可保持 None，不能把后一轮的通知误挂到前一轮。
    """
    ids = {int(it["turn_id"]) for it in items if it.get("turn_id") is not None}
    if len(ids) == 1 and all(it.get("turn_id") is not None for it in items):
        return ids.pop()
    return None


async def _flush(session_id: int) -> None:
    try:
        await asyncio.sleep(MERGE_WINDOW_S)
    except asyncio.CancelledError:
        return
    finally:
        _timers.pop(session_id, None)
    items = _pending.pop(session_id, [])
    # 合并窗口内模型已经读到终态的（status 返回 running=false / 自己 kill 的），不再回报
    items = [it for it in items if not bg_process_registry.is_observed(str(it.get("shell_id") or ""))]
    if not items:
        return
    # plan-20261025-bgturn：本轮已被撤回的命令不再提示（进程停止的额外提示已由撤回流程终止）
    revoked = await _turn_revoked({int(it["turn_id"]) for it in items if it.get("turn_id") is not None})
    if revoked:
        items = [it for it in items
                 if it.get("turn_id") is None or int(it["turn_id"]) not in revoked]
        if not items:
            return
    stopped = [it for it in items if it.get("stopped_by") == "user"]
    finished = [it for it in items if it.get("stopped_by") != "user"]

    try:
        from app.orchestration import mailbox

        if finished:
            await mailbox.publish(
                mailbox.MailKind.BG_COMMAND_DONE,
                session_id=session_id,
                text=_format(finished),
                extra={"bg_items": _bg_items(finished)},
                turn_id=_batch_turn_id(finished),
            )
        if stopped:
            await mailbox.publish(
                mailbox.MailKind.BG_COMMAND_STOPPED,
                session_id=session_id,
                text=_format_stopped(stopped),
                extra={"bg_items": _bg_items(stopped)},
                turn_id=_batch_turn_id(stopped),
            )
    except Exception:  # noqa: BLE001 — 投递失败只记日志，进程记录本身仍然可查
        logger.warning("[bg.notify] 投递失败 session=%s", session_id, exc_info=True)


async def on_bg_exit(payload: dict) -> None:
    """`BgProcessRegistry` 的完成回调：入队并在短窗口后合并投递。"""
    try:
        session_id = int(payload.get("session_id") or 0)
    except (TypeError, ValueError):
        return
    if session_id <= 0:
        return
    _pending.setdefault(session_id, []).append(payload)
    task = _timers.get(session_id)
    if task is not None and not task.done():
        return  # 已有合并窗口在途，本条随它一起送出
    try:
        _timers[session_id] = asyncio.get_running_loop().create_task(_flush(session_id))
    except RuntimeError:
        # 无事件循环（关停路径）：直接丢弃，进程记录仍可从 status 查到
        _pending.pop(session_id, None)
        logger.debug("[bg.notify] 无事件循环，跳过完成回报 session=%s", session_id)


def reset_for_tests() -> None:
    _pending.clear()
    for task in _timers.values():
        if not task.done():
            task.cancel()
    _timers.clear()
