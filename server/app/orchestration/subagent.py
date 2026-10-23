"""子代理管理（v2：主代理按需 spawn，独立上下文 + 结果收集）。

简化实现：spawn 启动后台 agent_loop 任务；结果按 subagent_id 缓存于内存，
主代理通过 collect_results 工具轮询获取。持久化子代理线程消息于 messages(thread_id=agent.id)。
"""
import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.schemas import ChatMessage
from app.orchestration.agent_loop import (
    AgentOutput,
    _retry_plan,
    _retry_wait_seconds,
    _wait_retry_interval,
    run_agent_loop,
)
from app.orchestration.tools.registry import tool_registry
from app.persistence.database import async_session_factory, commit_with_retry, run_write_locked

logger = logging.getLogger(__name__)


@dataclass
class SubagentHandle:
    agent_id: int
    task: object = None
    # v36: 归属 turn——每轮派发总量上限按此统计（含已完成/失败）
    turn_id: int | None = None
    # running / done / failed / paused（中断可续跑）/ cancelled（用户显式取消）
    status: str = "running"
    summary: str = ""
    error: str = ""
    artifact_ids: list[int] = field(default_factory=list)
    # v20: 探索子代理的"结论"文本（只读探索任务的最终输出，主代理据此整合，不落线程消息）
    findings: str = ""
    # plan-248-1258 M6: 主代理可检视子代理上下文与轨迹（subagent_inspect）
    task_title: str = ""
    task_description: str = ""
    handoff_summary: str = ""
    # 执行轨迹：工具调用序列（tool name + 参数摘要 + 输出摘要），由 _run 回填
    trajectory: list[dict] = field(default_factory=list)
    # 结构化汇报字段（由子代理最终输出解析）
    files_touched: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    # v36 (plan-321-1600 M1): 汇报闭环字段——验证动作/结果与验收标准逐条结论。
    # 此前主代理只能看到“做了什么”，看不到“验证过没有、验收标准满足没有”，
    # 无法判断子任务结果是否可信（需求：汇报要详细、信息要完整）。
    verification: list[str] = field(default_factory=list)
    acceptance: list[str] = field(default_factory=list)
    # 主代理可见的上下文快照（关键事实/约束，spawn 时传入）
    context_snapshot: dict = field(default_factory=dict)
    # plan-64-291: 派发时刻（单调时钟）——供运行期提醒展示「已运行多久」，
    # 让主代理知道子代理仍在正常推进、而不是卡住（避免因此自己动手重复劳动）。
    started_at: float | None = None
    # plan-330-1648 M2: 实际生效的模型/思考深度（供汇报与诊断——核对“设置是否生效”）
    model_id: int | None = None
    reasoning_effort: str | None = None
    # plan-91-394: 断点续跑——检查点与续跑参数
    # live_messages：run_agent_loop 的 messages 列表引用（每步登记），失败/暂停时即最新进度
    live_messages: list | None = None
    # run_spec：续跑所需的可落库运行参数（workspace / 工具名 / 模式 / 档位 …）
    run_spec: dict = field(default_factory=dict)
    # tool_schemas：进程内工具 schema（续跑优先复用，落库时按工具名重建）
    tool_schemas: list = field(default_factory=list)
    resumable: bool = False
    # waiting：瞬时故障后保活等待恢复中
    waiting: bool = False
    # cancel_requested：用户显式取消（cancel_subagent）→ 终态取消；否则中断一律视为暂停
    cancel_requested: bool = False
    # pause_reason：暂停原因文案（主代理中断 / 主代理已结束），写入检查点与卡片备注
    pause_reason: str = ""
    # 工作流节点子代理：结果由工作流运行时收取，不推送主代理、不计入每轮派发总量
    internal: bool = False
    workflow_node_id: int | None = None
    # 所属的工作流尝试（工具调用台账按尝试归属）
    workflow_attempt_id: int | None = None
    # 累计 token 用量（每个驱动段结束时累加）：工作流按节点差值计入程序预算
    tokens_used: int = 0
    # 每个执行步骤的监听回调（参数为当前消息列表）：工作流据此写入增量检查点日志
    on_step: object | None = None


class SubagentManager:
    """会话级子代理管理器。"""

    def __init__(self, session_id: int) -> None:
        self.session_id = session_id
        self._handles: dict[int, SubagentHandle] = {}
        # v36 (plan-321-1600 M3): 子代理终态“完成通知”队列 + 已读集合。
        # 对齐 ZCode（background-task-notifications）的 claim 语义：终态入队一次；
        # 主代理若已通过 collect_results 读到该结果，则不再重复推送，
        # 避免“同时收到工具结果与重复通知”。
        self._completion_queue: list[dict] = []
        self._notified: set[int] = set()
        # plan-330-1648 M4: 双向通信——主代理 → 子代理的待注入指令 inbox
        # （agent_id → 待投递指令列表）与“提问等待答复”事件（wait=true 的子代理挂起）。
        self._inboxes: dict[int, list[str]] = {}
        self._reply_waiters: dict[int, asyncio.Event] = {}
        # 子代理 → 主代理的上报回调（engine 启动 turn 时注册：写入主代理注入队列）
        self._leader_notify = None
        # 后台子代理终态回调（engine 启动 turn 时注册：turn 已结束时据此自动唤醒主代理）
        self._finish_notify = None
        # plan-91-394: 主代理 turn 是否存活。仅存活期间允许瞬时故障子代理保活等待恢复；
        # turn 收尾后不再保活（改为暂停保存检查点），避免后台无限重试。
        self.owner_active = False

    def spawn(self, db, *, agent, turn_id: int, task, handoff_summary: str,
              context_bundle, tool_schemas: list[dict], workspace: str,
              cancel_event: asyncio.Event | None = None, token_budget: int | None = None,
              task_title: str = "", task_description: str = "",
              context_snapshot: dict | None = None,
              reasoning_effort: str | None = None,
              subagent_mode: str | None = None) -> int:
        """后台启动子代理 agent_loop，返回 subagent id（派发即返回，不阻塞调用方）。

        plan-541-2264：同步派发能力已彻底删除，这里是唯一的派发入口。
        """
        handle = SubagentHandle(
            agent_id=agent.id,
            turn_id=turn_id,
            task_title=task_title or getattr(task, "title", "") or "",
            task_description=task_description,
            handoff_summary=handoff_summary,
            context_snapshot=context_snapshot or {},
            started_at=time.monotonic(),
            # plan-330-1648 M2: 记录生效的模型/深度（设置覆盖 → 会话 → 主代理已在上游解析完）
            model_id=getattr(agent, "model_id", None),
            reasoning_effort=reasoning_effort,
        )
        self._handles[agent.id] = handle

        # plan-91-394: 运行参数与消息检查点随 handle 保存——中断后可从断点续跑（resume_subagent）
        handle.tool_schemas = list(tool_schemas or [])
        handle.run_spec = _build_run_spec(
            agent=agent, task_id=getattr(task, "id", None), turn_id=turn_id,
            task_title=handle.task_title, workspace=workspace, tool_schemas=tool_schemas,
            subagent_mode=subagent_mode, reasoning_effort=reasoning_effort, token_budget=token_budget,
            reply_language=getattr(context_bundle, "reply_language", "auto"),
            reply_language_source=getattr(context_bundle, "reply_language_source", "user"),
        )
        handle.live_messages = list(context_bundle.to_messages())
        handle.task = asyncio.create_task(self._drive(
            handle, agent_id=agent.id, task_id=getattr(task, "id", None), turn_id=turn_id,
            messages=handle.live_messages, cancel_event=cancel_event,
        ))
        return agent.id

    # ── plan-91-394: 驱动循环 / 检查点 / 暂停 ──────────

    async def _drive(self, handle: "SubagentHandle", *, agent_id: int, task_id: int | None,
                     turn_id: int, messages: list, cancel_event: asyncio.Event | None) -> None:
        """子代理驱动循环：执行 → 瞬时故障保活重试 → 终态或暂停。

        - 成功：status=done，清空检查点；
        - 瞬时故障（网关/超时/空响应/流丢帧）且设置中的重试次数>0：按最后一个重试间隔等待，
          从检查点原地续跑（已完成步骤不重做），直到恢复或主代理中断；
        - 主代理中断（取消信号 / CancelledError）：status=paused，保存检查点（可续跑）；
        - 用户显式取消（cancel_subagent）：status=cancelled，清空检查点；
        - 其余失败：status=failed，保留检查点（可由“重试”或 resume_subagent 续跑）。
        """
        sid = self.session_id
        spec = dict(handle.run_spec or {})
        handle.live_messages = messages
        attempt_messages = messages
        try:
            while True:
                if task_id is not None:
                    await _sync_task(sid, task_id, "in_progress", None)
                try:
                    from app.persistence.models.agent import Agent as _AgentRow
                    async with async_session_factory() as s:
                        agent_row = await s.get(_AgentRow, agent_id)
                        out = await run_agent_loop(
                            s, session_id=sid, turn_id=turn_id,
                            agent=agent_row, context_messages=attempt_messages,
                            tool_schemas=handle.tool_schemas, workspace=spec.get("workspace") or "",
                            cancel_event=cancel_event, token_budget=spec.get("token_budget"),
                            task_id=task_id, model_id=spec.get("model_id"),
                            reasoning_effort=spec.get("reasoning_effort"),
                            subagent_mode=spec.get("subagent_mode"),
                            injected_inputs_provider=(lambda _aid=agent_id: self.drain_agent_inputs(_aid)),
                            # plan-541-2264：不再携带 sync 标记——所有子代理都是后台执行，
                            # report_to_leader(wait=true) 对所有子代理都合法，无需死锁防护。
                            subagent_comm={"manager": self, "agent_id": agent_id},
                            reply_language=spec.get("reply_language", "auto"),
                            reply_language_source=spec.get("reply_language_source", "user"),
                            checkpoint_sink=lambda msgs, _h=handle: _on_step(_h, msgs),
                        )
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    logger.exception("[subagent] %s 异常", agent_id)
                    out = AgentOutput(kind="error", error=f"执行异常: {str(e)[:300]}")

                handle.tokens_used += int(getattr(out, "tokens", 0) or 0)
                if out.kind == "message":
                    handle.status = "done"
                    handle.summary = out.text or ""
                    handle.error = out.error or ""
                    handle.artifact_ids = list(out.artifact_ids or [])
                    # v20: 探索子代理最终输出即结论文本（供主代理 wait 后直接整合）
                    handle.findings = out.text or ""
                    # plan-248-1258 M6: 解析结构化汇报（结果/变更文件/关键发现/风险）
                    _parse_structured_report(handle, out.text or "")
                    handle.resumable = False
                    handle.waiting = False
                    # plan-workflow-collab：工作流节点子代理完成后仍保留上下文——返工 / 复审要在同一子代理里
                    # 继续（进程重启后从这里还原），普通子代理完成即清空
                    await _persist_agent_state(
                        agent_id, "done",
                        _checkpoint_payload(handle, spec, "done") if handle.internal else None,
                    )
                    await _sync_task(sid, task_id, "done", (out.text or "")[:300] or None,
                                     agent_id=agent_id, token_usage=getattr(out, "tokens", 0) or 0)
                    await self._after_terminal(handle, notify=True)
                    logger.info("[subagent] %s 完成 status=done", agent_id)
                    return

                if handle.cancel_requested:
                    await self._finish_cancelled(handle, agent_id=agent_id, task_id=task_id)
                    return
                interrupted = out.kind == "cancelled" or bool(cancel_event and cancel_event.is_set())
                if interrupted:
                    await self._finish_paused(handle, agent_id=agent_id, task_id=task_id, spec=spec,
                                              reason=handle.pause_reason or "主代理已中断")
                    return
                if out.transient:
                    waits = _retry_plan()
                    if waits and self.owner_active:
                        # 保活：重试耗尽仍是瞬时故障——不判死，按最后一个间隔继续尝试恢复
                        handle.waiting = True
                        await _sync_task(sid, task_id, "in_progress",
                                         "网络或网关异常，等待恢复后自动续跑（进度已保留）")
                        logger.warning("[subagent] %s 瞬时故障保活等待恢复: %s", agent_id, (out.error or "")[:200])
                        alive = await _wait_retry_interval(cancel_event, _retry_wait_seconds(len(waits)))
                        handle.waiting = False
                        if not alive:
                            await self._finish_paused(handle, agent_id=agent_id, task_id=task_id, spec=spec,
                                                      reason=handle.pause_reason or "主代理已中断")
                            return
                        attempt_messages = _sanitize_messages(list(handle.live_messages or attempt_messages))
                        continue
                    # 未配置重试：瞬时故障也不判死，保存检查点等待主代理/用户续跑
                    await self._finish_paused(handle, agent_id=agent_id, task_id=task_id, spec=spec,
                                              reason=out.error or "网络或网关故障")
                    return
                await self._finish_failed(handle, agent_id=agent_id, task_id=task_id, spec=spec,
                                          error=out.error or "未知原因")
                return
        except asyncio.CancelledError:
            # 任务被取消：主代理中断/结束（暂停）或用户显式取消（终态）。收尾后正常结束任务。
            handle.waiting = False
            if handle.cancel_requested:
                await self._finish_cancelled(handle, agent_id=agent_id, task_id=task_id)
            else:
                await self._finish_paused(handle, agent_id=agent_id, task_id=task_id, spec=spec,
                                          reason=handle.pause_reason or "主代理已中断")

    async def _finish_paused(self, handle: "SubagentHandle", *, agent_id: int, task_id: int | None,
                             spec: dict, reason: str) -> None:
        """暂停：保存检查点（可续跑）。不通知唤醒——由主代理在用户“继续”时决定续跑。"""
        handle.status = "paused"
        handle.waiting = False
        handle.resumable = True
        handle.error = f"已暂停（{reason}），进度已保存，可续跑"
        await _persist_agent_state(agent_id, "paused", _checkpoint_payload(handle, spec, reason))
        await _sync_task(self.session_id, task_id, "failed", handle.error[:300], agent_id=agent_id)
        await _broadcast_subagent_failed(self.session_id, agent_id, handle.error, "failed")
        await self._after_terminal(handle, notify=False)
        logger.warning("[subagent] %s 已暂停（%s），检查点已保存", agent_id, reason)

    async def _finish_failed(self, handle: "SubagentHandle", *, agent_id: int, task_id: int | None,
                             spec: dict, error: str) -> None:
        """非瞬时失败（鉴权/模型不可用/确定性截断等）：终态失败，保留检查点供重试续跑。"""
        handle.status = "failed"
        handle.waiting = False
        handle.error = error
        handle.resumable = True
        await _persist_agent_state(agent_id, "failed", _checkpoint_payload(handle, spec, error))
        await _sync_task(self.session_id, task_id, "failed", f"{error[:200]}（进度已保存，可续跑）",
                         agent_id=agent_id)
        await _broadcast_subagent_failed(self.session_id, agent_id, error, "failed")
        await self._after_terminal(handle, notify=True)
        logger.info("[subagent] %s 完成 status=failed", agent_id)

    async def _finish_cancelled(self, handle: "SubagentHandle", *, agent_id: int, task_id: int | None) -> None:
        """用户显式取消：终态，清空检查点（不可续跑）。"""
        handle.status = "cancelled"
        handle.waiting = False
        handle.resumable = False
        handle.error = handle.error or "用户中断"
        await _persist_agent_state(agent_id, "cancelled", None)
        await _sync_task(self.session_id, task_id, "cancelled", "用户中断", agent_id=agent_id)
        await _broadcast_subagent_failed(self.session_id, agent_id, "用户中断", "cancelled")
        await self._after_terminal(handle, notify=False)

    async def _after_terminal(self, handle: "SubagentHandle", *, notify: bool) -> None:
        """子代理收尾：flush 本 turn 消息缓冲；notify=True 时入完成通知并唤醒主代理。"""
        try:
            from app.persistence.write_behind import write_behind
            await write_behind.get(self.session_id, handle.turn_id).flush()
        except Exception:
            logger.debug("[subagent] flush turn buffer failed agent=%s", handle.agent_id, exc_info=True)
        if notify and not handle.internal:
            self._enqueue_completion(handle)
            self.notify_finished(handle)

    def pause_for_turn_end(self, *, interrupted: bool) -> int:
        """plan-91-394: 主代理 turn 收尾时决定子代理去留（子代理只在主代理停止/中断时自动停止）。

        - interrupted=True（主代理被中断或出错结束）：运行中的子代理全部暂停（保存检查点）；
        - interrupted=False（主代理正常结束）：仅暂停处于保活等待中的子代理（避免无限后台重试），
          正常运行中的子代理照常完成并唤醒主代理。
        返回被暂停的子代理数量。
        """
        self.owner_active = False
        paused = 0
        for h in self._handles.values():
            if h.status != "running" or h.task is None or h.task.done():
                continue
            if interrupted or h.waiting:
                h.pause_reason = "主代理已中断" if interrupted else "主代理已结束"
                h.task.cancel()
                paused += 1
        if paused:
            logger.warning("[subagent] 主代理 turn 结束(interrupted=%s)，暂停子代理 %d 个", interrupted, paused)
        return paused

    # ── plan-330-1648 M4: 双向通信 ──────────────────────

    def push_agent_input(self, agent_id: int, message: str) -> bool:
        """主代理向运行中的子代理追加指令（send_to_subagent）。

        返回 False 表示子代理不存在或已结束——指令无法投递（调用方转述给模型）。
        """
        h = self._handles.get(agent_id)
        if h is None or h.status != "running" or h.task is None or h.task.done():
            return False
        self._inboxes.setdefault(agent_id, []).append(message)
        # 若该子代理正挂着等答复（report_to_leader wait=true），唤醒它
        ev = self._reply_waiters.get(agent_id)
        if ev is not None:
            ev.set()
        return True

    def drain_agent_inputs(self, agent_id: int) -> list[dict]:
        """取走该子代理的待注入指令（子代理 loop 每步 LLM 调用前调用）。"""
        items = self._inboxes.pop(agent_id, [])
        return [{"content": f"[Instruction from main agent]\n{m}"} for m in items]

    def set_leader_notify(self, callback) -> None:
        """注册子代理 → 主代理的上报回调（engine 在启动 turn 时注册）。"""
        self._leader_notify = callback

    def set_finish_notify(self, callback) -> None:
        """注册后台子代理终态回调（engine 在启动 turn 时注册）。

        后台子代理完成时主代理 turn 可能已结束——engine 据此决定是否自动创建
        续跑 turn 把完成通知送达主代理（对齐 zcode background-task-notifications，
        不再依赖用户下一条消息触发送达）。
        """
        self._finish_notify = callback

    def notify_finished(self, handle: SubagentHandle) -> None:
        """子代理进入终态 → 通知 engine 判断是否需要唤醒主代理（失败仅记日志）。"""
        cb = self._finish_notify
        if cb is None:
            return
        try:
            cb(handle)
        except Exception:
            logger.debug("[subagent] 终态回调失败 agent=%s", handle.agent_id, exc_info=True)

    def notify_leader(self, agent_id: int, message: str, kind: str) -> None:
        """子代理上报/提问 → 主代理注入队列（失败仅记日志，不影响子代理）。"""
        cb = self._leader_notify
        if cb is None:
            return
        try:
            cb(agent_id, message, kind)
        except Exception:
            logger.debug("[subagent] 上报主代理失败 agent=%s", agent_id, exc_info=True)

    async def wait_for_reply(self, agent_id: int, timeout_s: float = 300.0) -> str | None:
        """子代理提问后等待主代理答复（report_to_leader wait=true）。

        返回答复文本；超时返回 None（调用方提示“按最佳判断继续”）。
        注意：只有后台子代理可等待——同步（主代理阻塞等待）场景会死锁，工具层已拒绝。
        """
        ev = self._reply_waiters.get(agent_id)
        if ev is None:
            ev = asyncio.Event()
            self._reply_waiters[agent_id] = ev
        try:
            await asyncio.wait_for(ev.wait(), timeout=max(1.0, float(timeout_s)))
        except asyncio.TimeoutError:
            return None
        finally:
            self._reply_waiters.pop(agent_id, None)
        items = self._inboxes.pop(agent_id, [])
        return "\n".join(items) if items else "(main agent replied without content)"

    def get(self, agent_id: int) -> SubagentHandle | None:
        return self._handles.get(agent_id)

    def results(self) -> list[dict]:
        """已完成的子代理结构化结果列表（plan-248-1258 M6 加强汇报）。"""
        out = []
        for aid, h in self._handles.items():
            if h.status in ("done", "failed", "paused"):
                out.append({
                    "agent_id": aid, "status": h.status,
                    "title": h.task_title,
                    "summary": h.summary, "error": h.error,
                    "findings": h.findings,
                    "files_touched": list(h.files_touched),
                    "risks": list(h.risks),
                    # v36 (plan-321-1600 M1): 主代理侧可获得验证/验收结论
                    "verification": list(h.verification),
                    "acceptance": list(h.acceptance),
                })
        return out

    def duplicate_of(self, task_title: str) -> SubagentHandle | None:
        """同标题子任务已存在时返回其 handle（v36 plan-321-1600 M1 防无用功用）。"""
        if not task_title:
            return None
        for h in self._handles.values():
            if h.task_title == task_title:
                return h
        return None

    def spawned_count(self, turn_id: int | None) -> int:
        """本 turn 已派发的子代理总数（含已完成/失败）。

        v36 优化：每轮总量上限必须统计“已派发”而非“运行中”——旧实现用
        pending_count 兼职判总量，快速完成的子代理不占额度，一轮内实际可派发
        数量远超上限（语义错位）。并发闸门仍用 pending_count。
        """
        if turn_id is None:
            return 0
        return sum(1 for h in self._handles.values() if h.turn_id == turn_id and not h.internal)

    def queue_leader_note(self, text: str) -> None:
        """plan-330-1648 M4: turn 已结束（后台子代理续跑）时，把子代理上报/提问挂到
        完成通知队列，随下一轮 turn 启动的 drain_completions() 一并送达主代理。"""
        self._completion_queue.append({"agent_id": 0, "text": text})

    def _enqueue_completion(self, handle: SubagentHandle) -> None:
        """子代理进入终态时入队一条完成通知（v36 plan-321-1600 M3）。"""
        if handle.agent_id in self._notified:
            return
        self._completion_queue.append({
            "agent_id": handle.agent_id,
            "text": _format_completion_note(handle),
        })

    def claim_notification(self, agent_id: int) -> None:
        """标记某子代理结果已被主代理读取——不再推送它的完成通知。

        v36：对齐 ZCode 的 claim 语义，避免“主代理既拿到 collect_results 结果，
        又收到一条重复的完成推送”。
        """
        self._notified.add(agent_id)
        self._completion_queue = [n for n in self._completion_queue if n["agent_id"] != agent_id]

    def drain_completions(self) -> list[str]:
        """取出待推送的完成通知文本（取出即视为已读）。"""
        if not self._completion_queue:
            return []
        notes = [str(n["text"]) for n in self._completion_queue]
        for n in self._completion_queue:
            # plan-330-1648 M4: agent_id=0 是“非归属某子代理”的上报转发（见 queue_leader_note）
            _aid = int(n.get("agent_id") or 0)
            if _aid:
                self._notified.add(_aid)
        self._completion_queue = []
        return notes

    def has_unclaimed_notes(self) -> bool:
        """plan-330-1648 M7: 是否存在尚未送达主代理的完成通知/上报。

        turn 收尾时据此决定是否保活 manager——有未送达通知就保留，
        下一轮 turn 启动时 drain_completions() 会把它注入主代理上下文。
        """
        return bool(self._completion_queue)

    def pending_count(self) -> int:
        # 任务已结束而状态仍停在 running（驱动循环收尾时抛异常）的 handle 已经没有人在跑：
        # 不能再算运行中，否则主代理的「等待子代理结束」会空等到总超时，并发名额也被永久占着
        return sum(
            1 for h in self._handles.values()
            if h.status == "running" and (h.task is None or not h.task.done())
        )

    def running_snapshot(self) -> list[dict]:
        """plan-64-291: 运行中子代理清单（agent_id / 标题 / 已运行秒数）。

        运行期提醒的唯一数据源——主循环据此告诉主代理「还有谁在跑、跑了多久」，
        避免它在等待期重复劳动、或因误判卡死而自己动手。已完成的子代理不在内。
        """
        now = time.monotonic()
        snap: list[dict] = []
        for h in self._handles.values():
            if h.status != "running":
                continue
            elapsed = max(0.0, now - h.started_at) if h.started_at is not None else None
            snap.append({
                "agent_id": h.agent_id,
                "title": h.task_title or "",
                "elapsed_s": elapsed,
            })
        return snap

    async def wait_all(self) -> None:
        """等待所有仍在运行的子代理结束（collect_results(wait=true) 用）。"""
        pending = [h.task for h in self._handles.values() if h.task is not None and not h.task.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def wait_one(self, timeout: float | None = None) -> bool:
        """等待任意一个运行中的子代理结束（v36 plan-321-1600 M3 并发排队用）。

        返回是否等到（False = 超时）。无运行中子代理时视为已就绪（True）。
        """
        pending = [h.task for h in self._handles.values() if h.task is not None and not h.task.done()]
        if not pending:
            # 没有可等的任务也要让出事件循环：调用方常在 while pending_count() > 0 里循环调用，
            # 若某个 handle 状态停在 running 而任务早已结束（收尾异常），这里不 await 就会把循环空转卡死
            await asyncio.sleep(min(0.5, timeout) if timeout else 0.05)
            return True
        done, _ = await asyncio.wait(pending, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        return bool(done)

    def inspect(self, agent_id: int, section: str = "all") -> dict | None:
        """返回某子代理的上下文/轨迹/结果（subagent_inspect 用）。"""
        h = self._handles.get(agent_id)
        if h is None:
            return None
        data: dict = {"agent_id": agent_id, "status": h.status}
        if section in ("all", "context"):
            data["context"] = {
                "title": h.task_title,
                "task_description": h.task_description,
                "handoff_summary": h.handoff_summary,
                "inherited_context": h.context_snapshot,
            }
        if section in ("all", "transcript"):
            data["transcript"] = h.trajectory
        if section in ("all", "result"):
            data["result"] = {
                "summary": h.summary,
                "findings": h.findings,
                "files_touched": h.files_touched,
                "risks": h.risks,
                # v36: 闭环字段同样可经 subagent_inspect 查看
                "verification": h.verification,
                "acceptance": h.acceptance,
                "error": h.error,
            }
        return data

    def agent_ids(self) -> list[int]:
        return list(self._handles.keys())

    async def cancel(self, agent_id: int) -> str:
        """取消单个仍在运行的子代理（v36 plan-321-1600 M3，对齐 ZCode 的 TaskCancel）。

        返回结果状态字符串（cancelled / not_running / not_found），供工具层转述给模型。
        """
        h = self._handles.get(agent_id)
        if h is None:
            return "not_found"
        if h.status != "running" or h.task is None or h.task.done():
            return "not_running"
        # plan-91-394: 显式取消为终态（区别于主代理中断的暂停）
        h.cancel_requested = True
        h.task.cancel()
        await asyncio.gather(h.task, return_exceptions=True)
        h.status = "cancelled"
        h.error = h.error or "已取消"
        return "cancelled"

    async def cancel_all(self) -> None:
        """取消并等待该会话所有尚未结束的子代理任务。"""
        pending = [h.task for h in self._handles.values() if h.task is not None and not h.task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for handle in self._handles.values():
            if handle.status == "running":
                handle.status = "cancelled"



# ── plan-91-394: 断点续跑（检查点 / 续跑 / 列出可续跑子代理） ──

# 续跑时追加到检查点末尾的说明：告知模型这是断点续跑，已完成的步骤不要重做
_RESUME_NOTE = (
    "[系统·断点续跑] 你上一次运行因网络/网关故障或主代理中断而暂停，现已从断点恢复。"
    "以上消息即已完成的全部进度：不要重复已完成的步骤；若最后一步的结果不确定"
    "（例如刚执行的写入或命令），先用只读工具核实当前状态，再继续完成原任务，并按原汇报格式输出最终结果。"
)


def _sanitize_messages(messages: list) -> list:
    """修复检查点中的工具调用配对（悬空 tool_calls / 孤立 tool 消息），保证续跑请求合法。"""
    from app.orchestration.compaction import (
        ensure_tool_pairing,
        normalize_tool_sequence,
        repair_tool_call_ids,
    )
    out = repair_tool_call_ids(list(messages))
    out = ensure_tool_pairing(out)
    return normalize_tool_sequence(out)


def _build_run_spec(*, agent, task_id, turn_id, task_title, workspace, tool_schemas, subagent_mode,
                    reasoning_effort, token_budget, reply_language, reply_language_source) -> dict:
    """运行参数（可落库的 JSON 子集）。工具 schema 按名保存，续跑时重建。"""
    names = [s.get("function", {}).get("name") for s in (tool_schemas or [])]
    return {
        "task_id": task_id,
        "turn_id": turn_id,
        "task_title": task_title or "",
        "workspace": workspace or "",
        "tool_names": [n for n in names if n],
        "subagent_mode": subagent_mode,
        "reasoning_effort": reasoning_effort,
        "token_budget": token_budget,
        "reply_language": reply_language,
        "reply_language_source": reply_language_source,
        "model_id": getattr(agent, "model_id", None),
    }


def _dump_message(m) -> dict | None:
    if isinstance(m, ChatMessage):
        return m.model_dump(exclude_none=True)
    if isinstance(m, dict):
        return {k: v for k, v in m.items() if v is not None}
    return None


def _on_step(handle: "SubagentHandle", msgs: list) -> None:
    """每个执行步骤开始时的回调：登记最新消息引用，并通知可选的监听者（工作流增量检查点）。"""
    handle.live_messages = msgs
    listener = handle.on_step
    if listener is not None:
        try:
            listener(msgs)
        except Exception:  # 监听者失败不影响子代理运行
            logger.debug("[subagent] 步骤监听回调失败(非阻塞)", exc_info=True)


def _checkpoint_payload(handle: "SubagentHandle", spec: dict, reason: str) -> dict:
    """检查点：完整 messages（含系统/上下文/已完成的工具往返）+ 运行参数。"""
    msgs = [d for d in (_dump_message(m) for m in (handle.live_messages or [])) if d]
    return {
        "messages": msgs,
        "spec": dict(spec or {}),
        "reason": (reason or "")[:300],
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }


def _build_tool_schemas(names: list[str] | None) -> list[dict]:
    """按工具名重建子代理工具 schema（与 spawn 时口径一致：注册表工具 + 子代理自身通信工具）。"""
    from app.orchestration.subagent_tools import SUBAGENT_OWN_TOOLS, strip_for_subagent

    wanted = set(names) if names else None
    out = [
        s for s in tool_registry.all_schemas()
        if wanted is None or s.get("function", {}).get("name") in wanted
    ]
    have = {s.get("function", {}).get("name") for s in out}
    for own in SUBAGENT_OWN_TOOLS:
        name = own["function"]["name"]
        if (wanted is None or name in wanted) and name not in have:
            out.append(own)
    # plan-474-2105：续跑沿用旧检查点里的工具名，可能含已禁用的「问用户 / 派下级」工具——
    # 与 spawn 路径同口径剥离，否则续跑等于绕过能力边界。
    return strip_for_subagent(out)


async def _persist_agent_state(agent_id: int, status: str, checkpoint: dict | None) -> None:
    """落库子代理状态与检查点（写引擎单写线程）。失败仅记日志，不影响收尾。"""
    from app.persistence.models.agent import Agent as _AgentRow

    def _patch(s):
        row = s.get(_AgentRow, agent_id)
        if row is None:
            return
        row.status = status
        row.checkpoint_json = checkpoint
        s.commit()

    try:
        await run_write_locked(_patch, label=f"subagent.state.{agent_id}")
    except Exception:
        logger.warning("[subagent] 子代理状态落库失败 agent=%s status=%s", agent_id, status, exc_info=True)


async def _sync_task(session_id: int, task_id: int | None, status: str, note: str | None,
                     agent_id: int | None = None, token_usage: int | None = None) -> None:
    """独立会话同步子任务状态（子代理各自独立会话，避免与主代理交错提交）。"""
    if task_id is None:
        return
    async with async_session_factory() as s:
        await _sync_task_status(s, session_id, task_id, status, note,
                                agent_id=agent_id, token_usage=token_usage)


async def resume_subagent(session_id: int, agent_id: int, *, turn_id: int,
                          cancel_event: asyncio.Event | None = None,
                          readonly_only: bool = False,
                          extra_note: str | None = None) -> tuple[str, str]:
    """续跑一个中断/暂停的子代理：从检查点继续，已完成步骤与上下文全部保留。

    readonly_only：主代理处于只读/计划模式时为 True——只允许续跑只读探索子代理，
    不得借续跑绕过模式约束去恢复可写子代理。
    返回 (state, detail)：state ∈ resumed / already_running / not_found / not_resumable。
    """
    from app.persistence.models.agent import Agent as _AgentRow

    manager = get_subagent_manager(session_id)
    live = manager.get(agent_id)
    if live is not None and live.status == "running" and live.task is not None and not live.task.done():
        return "already_running", ""

    async with async_session_factory() as s:
        row = await s.get(_AgentRow, agent_id)
        if row is None or row.session_id != session_id or row.kind != "sub":
            return "not_found", ""
        ck = dict(row.checkpoint_json or {}) if row.checkpoint_json else {}
        db_status = row.status
        row_name = row.name

    if not ck.get("messages"):
        return "not_resumable", "没有可用的检查点（任务已完成或从未运行）"
    if db_status == "done":
        return "not_resumable", "任务已完成，无需续跑"
    if readonly_only and (ck.get("spec") or {}).get("subagent_mode") != "readonly":
        return "not_resumable", "当前为只读/计划模式，只能续跑只读探索子代理"
    try:
        messages = _sanitize_messages([ChatMessage(**m) for m in ck["messages"]])
    except Exception:
        logger.warning("[subagent] 检查点解析失败 agent=%s", agent_id, exc_info=True)
        return "not_resumable", "检查点已损坏，无法续跑"
    messages.append(ChatMessage(role="user", content=_RESUME_NOTE))
    if extra_note:
        messages.append(ChatMessage(role="user", content=extra_note))

    spec = dict(ck.get("spec") or {})
    steps = sum(1 for m in ck["messages"] if m.get("role") == "assistant" and m.get("tool_calls"))
    handle = live or SubagentHandle(agent_id=agent_id)
    handle.turn_id = turn_id
    handle.task_title = handle.task_title or spec.get("task_title", "") or ""
    handle.status = "running"
    handle.error = ""
    handle.resumable = False
    handle.cancel_requested = False
    handle.waiting = False
    handle.pause_reason = ""
    handle.started_at = time.monotonic()
    handle.run_spec = spec
    handle.tool_schemas = _build_tool_schemas(spec.get("tool_names"))
    handle.live_messages = messages
    manager._handles[agent_id] = handle
    # 续跑是新一轮交付：清除该子代理的旧“已读/旧完成通知”，保证续跑结果会正常送达主代理
    manager._notified.discard(agent_id)
    manager._completion_queue = [
        n for n in manager._completion_queue if int(n.get("agent_id") or 0) != agent_id
    ]
    await _persist_agent_state(agent_id, "running", ck)
    handle.task = asyncio.create_task(manager._drive(
        handle, agent_id=agent_id, task_id=spec.get("task_id"), turn_id=turn_id,
        messages=messages, cancel_event=cancel_event or asyncio.Event(),
    ))
    # 与派发路径一致：前端卡片重新进入运行态、待完成计数同步（失败不影响续跑本身）
    try:
        from app.orchestration.agent_events import broadcast
        await broadcast(session_id, {
            "event": "agent.started",
            "payload": {"agent_id": agent_id, "kind": "sub", "name": row_name,
                        "turn_id": turn_id, "task_id": spec.get("task_id")},
        })
        await broadcast(session_id, {
            "event": "subagent.pending",
            "payload": {"session_id": session_id, "pending": int(manager.pending_count())},
        })
    except Exception:
        logger.debug("[subagent] 续跑事件广播失败(非阻塞)", exc_info=True)
    logger.info("[subagent] 续跑 agent=%s steps=%d", agent_id, steps)
    return "resumed", f"已保留 {steps} 步已完成的进度"


async def continue_subagent(session_id: int, agent_id: int, *, turn_id: int, note: str) -> tuple[str, str]:
    """同一子代理线程续做（工作流的返工 / 格式修复）：沿用进程内上下文，追加一条说明后重新驱动。

    优先沿用进程内上下文；进程内没有时从落库的检查点还原（plan-workflow-collab：工作流子代理完成时
    保留检查点，重启后返工仍回到同一子代理）。两者都没有返回 not_resumable，由调用方改为新建子代理。
    返回 (state, detail)：state ∈ resumed / already_running / not_resumable。
    """
    from app.persistence.models.agent import Agent as _AgentRow

    manager = get_subagent_manager(session_id)
    live = manager.get(agent_id)
    if live is not None and live.task is not None and not live.task.done():
        return "already_running", ""
    async with async_session_factory() as s:
        row = await s.get(_AgentRow, agent_id)
        row_name = row.name if row is not None else ""
        ck = dict(row.checkpoint_json or {}) if row is not None and row.session_id == session_id else {}
    if live is not None and live.live_messages:
        messages = _sanitize_messages(list(live.live_messages))
        spec = dict(live.run_spec or {})
    elif ck.get("messages"):
        try:
            messages = _sanitize_messages([ChatMessage(**m) for m in ck["messages"]])
        except Exception:
            logger.warning("[subagent] 检查点解析失败 agent=%s", agent_id, exc_info=True)
            return "not_resumable", "检查点已损坏"
        spec = dict(ck.get("spec") or {})
        live = live or SubagentHandle(agent_id=agent_id)
        live.task_title = live.task_title or spec.get("task_title", "") or row_name or ""
        live.run_spec = spec
        live.tool_schemas = _build_tool_schemas(spec.get("tool_names"))
        manager._handles[agent_id] = live
    else:
        return "not_resumable", "没有该子代理的上下文"
    messages.append(ChatMessage(role="user", content=note))
    live.turn_id = turn_id
    live.status = "running"
    live.error = ""
    live.resumable = False
    live.cancel_requested = False
    live.waiting = False
    live.pause_reason = ""
    live.started_at = time.monotonic()
    live.live_messages = messages
    manager._notified.discard(agent_id)
    manager._completion_queue = [
        n for n in manager._completion_queue if int(n.get("agent_id") or 0) != agent_id
    ]
    await _persist_agent_state(agent_id, "running", _checkpoint_payload(live, spec, "continue"))
    live.task = asyncio.create_task(manager._drive(
        live, agent_id=agent_id, task_id=spec.get("task_id"), turn_id=turn_id,
        messages=messages, cancel_event=asyncio.Event(),
    ))
    # 与派发 / 续跑一致：子代理面板与卡片重新进入运行态（否则节点在转圈、点开却显示已完成）
    try:
        from app.orchestration.agent_events import broadcast
        await broadcast(session_id, {
            "event": "agent.started",
            "payload": {"agent_id": agent_id, "kind": "sub", "name": row_name or live.task_title,
                        "turn_id": turn_id, "task_id": spec.get("task_id")},
        })
    except Exception:
        logger.debug("[subagent] 续做事件广播失败(非阻塞)", exc_info=True)
    return "resumed", "已在原子代理上下文中继续"


async def list_resumable_subagents(session_id: int, limit: int = 8) -> list[dict]:
    """列出本会话中断后可续跑（status=paused 且有检查点）的子代理，供新 turn 注入上下文。"""
    from sqlalchemy import select
    from app.persistence.models.agent import Agent as _AgentRow

    try:
        async with async_session_factory() as s:
            rows = (await s.execute(
                select(_AgentRow).where(
                    _AgentRow.session_id == session_id,
                    _AgentRow.kind == "sub",
                    _AgentRow.status == "paused",
                    _AgentRow.workflow_node_id.is_(None),
                ).order_by(_AgentRow.id.desc()).limit(limit)
            )).scalars().all()
    except Exception:
        logger.warning("[subagent] 查询可续跑子代理失败 session=%s", session_id, exc_info=True)
        return []
    items: list[dict] = []
    for r in rows:
        ck = r.checkpoint_json or {}
        if not ck.get("messages"):
            continue
        spec = ck.get("spec") or {}
        steps = sum(1 for m in ck["messages"] if m.get("role") == "assistant" and m.get("tool_calls"))
        items.append({
            "agent_id": int(r.id),
            "title": spec.get("task_title") or r.name or "",
            "steps": steps,
            "reason": ck.get("reason") or "",
            "saved_at": ck.get("saved_at") or "",
        })
    return items


# 会话级 manager 注册表
_managers: dict[int, SubagentManager] = {}


def get_subagent_manager(session_id: int) -> SubagentManager:
    if session_id not in _managers:
        _managers[session_id] = SubagentManager(session_id)
    return _managers[session_id]


def peek_subagent_manager(session_id: int) -> SubagentManager | None:
    """只读获取（不创建）会话子代理管理器——REST 展示用（避免读接口凭空建条目）。"""
    return _managers.get(session_id)


def cleanup(session_id: int) -> None:
    _managers.pop(session_id, None)


async def _broadcast_subagent_failed(session_id: int, agent_id: int, error: str, status: str) -> None:
    """plan-330-1648 M7: 子代理失败/取消事件广播。

    此前失败只写内存 handle 与任务表 note，主消息流卡片与右面板只剩一个红叉、看不到原因
    （用户反馈“子代理异常中断”）。现在显式广播，前端写入 subagentMeta[].error 并展示。
    """
    try:
        from app.orchestration.agent_events import broadcast
        await broadcast(session_id, {
            "event": "subagent.failed",
            "payload": {"agent_id": agent_id, "status": status, "error": (error or "")[:500]},
        })
    except Exception:
        logger.debug("[subagent] 失败事件广播失败(非阻塞)", exc_info=True)


# ── plan-248-1258 M6: 结构化汇报解析 ──

# 标题行识别：去 # 后为短行（<=72 字符）且命中关键词即视为分节标题。
# 用宽松匹配（而非整行精确）以兼容 "### Risks / Open Questions"、"## 变更文件：" 等变体。
_FILES_HEAD = re.compile(r"(变更文件|改动文件|files?\s*(?:touched|changed|modified)|files\s*[:：])", re.IGNORECASE)
_RISK_HEAD = re.compile(r"(风险|未决|待确认|risks?|blockers?|open\s+questions?)", re.IGNORECASE)
_OTHER_HEADS = re.compile(
    r"(result|summary|results|结论|关键发现|findings|key\s+findings|后续建议|next\s+steps)",
    re.IGNORECASE,
)
# v36 (plan-321-1600 M1): 汇报闭环分节识别（验证动作/结果、验收标准逐条结论）。
# 与上面几组同口径：宽容匹配 "### Verification" / "## 验证：" 等变体。
_VERIFY_HEAD = re.compile(r"(验证|校验|verification|verified|how\s+verified|tests?\s+run)", re.IGNORECASE)
_ACCEPT_HEAD = re.compile(r"(验收|acceptance|definition\s+of\s+done)", re.IGNORECASE)
# v36 修复：交替项按长度递减（tsx 先于 ts、json 先于 js），并加词边界断言 (?!\w)，
# 避免「.json 被截成 .js」「.tsx 被截成 .ts」（此前短项优先，主代理拿到的变更文件
# 路径扩展名错误）；字符类补 ":" 以保留 Windows 盘符（此前 D:\...\file.json 丢盘符）。
_FILE_RE = re.compile(
    r"[\w./\\:-]+\.(?:tsx|ts|json|jsx|js|py|go|rs|java|cs|cpp|c|h|rb|php|sql|md|yaml|yml|toml"
    r"|svelte|scss|less|css|html|htm|vue|xml|txt|ini|cfg|sh|bat|ps1)(?![\w])"
)


def _parse_structured_report(handle: "SubagentHandle", text: str) -> None:
    """从子代理最终输出解析结构化字段（变更文件 / 风险 / 验证 / 验收）。

    子代理被引导输出分节汇报（结果/变更文件/关键发现/验证/验收/风险）；此处做宽容解析：
    识别到「变更文件」节时收集其中的文件路径，其余分节按条目收集。
    v36 (plan-321-1600 M1) 新增 verification / acceptance 两节，补齐验收闭环。
    解析失败不影响主流程（summary/findings 已保留全文）。
    """
    if not text:
        return
    try:
        section = ""
        # v36 (plan-321-1600 M1): 验证/验收节里的文件是**命令参数**（如 pytest xxx.py），
        # 不是变更产物——兜底提取时需排除，避免污染 Files Touched。
        _skip_lines: set[str] = set()
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            # 标题行：以 # 开头，或短行且以冒号结尾
            _is_head = line.startswith("#") or (len(line) <= 72 and line.endswith((":", "：")))
            _head_text = line.lstrip("#").strip()
            if _is_head and len(_head_text) <= 72:
                if _FILES_HEAD.search(_head_text):
                    section = "files"
                    continue
                if _RISK_HEAD.search(_head_text):
                    section = "risks"
                    continue
                if _VERIFY_HEAD.search(_head_text):
                    section = "verification"
                    continue
                if _ACCEPT_HEAD.search(_head_text):
                    section = "acceptance"
                    continue
                if _OTHER_HEADS.search(_head_text):
                    section = "other"
                    continue
            if section in ("verification", "acceptance"):
                _skip_lines.add(line)
            if section == "files":
                for f in _FILE_RE.findall(line):
                    if f not in handle.files_touched:
                        handle.files_touched.append(f)
            elif section == "risks":
                item = line.lstrip("-*·0123456789.) ").strip()
                if item and len(item) > 3 and item != "None":
                    handle.risks.append(item[:200])
            elif section in ("verification", "acceptance"):
                # v36: 同上宽容化——去项目符号后收集条目（None / n/a 视为空）
                item = line.lstrip("-*·0123456789.) ").strip()
                if item and len(item) > 3 and item.lower() not in ("none", "n/a"):
                    target = handle.verification if section == "verification" else handle.acceptance
                    target.append(item[:300])
        # 未识别到「变更文件」节时，兜底从全文提取文件路径（限量，避免噪声）。
        # v36: 排除验证/验收节的行（其中的文件多为命令参数，非变更产物）。
        if not handle.files_touched:
            _fallback = "\n".join(
                ln for ln in text.splitlines() if ln.strip() not in _skip_lines
            )
            for f in _FILE_RE.findall(_fallback)[:20]:
                if f not in handle.files_touched:
                    handle.files_touched.append(f)
    except Exception:  # noqa: BLE001
        logger.debug("[subagent] 结构化汇报解析失败(非阻塞)", exc_info=True)


def _first_meaningful_paragraph(text: str, limit: int = 300) -> str:
    """取汇报里第一段正文（跳过 ### 分节标题），用于完成通知的一句话摘要。"""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        return line[:limit]
    return ""


def _format_completion_note(handle: "SubagentHandle") -> str:
    """子代理完成通知文案（v36 plan-321-1600 M3）。

    形如：[子代理 #140 已完成] 任务标题：结论首段（files: 2，risks: 0）。
    该文本经 injected_inputs_provider 注入主代理上下文，使其不必轮询 collect_results。
    """
    title = handle.task_title or "(未命名子任务)"
    if handle.status == "done":
        note = f"[子代理 #{handle.agent_id} 已完成] {title}"
        body = _first_meaningful_paragraph(handle.findings or handle.summary or "")
        if body:
            note += f"：{body}"
        note += f"（files: {len(handle.files_touched)}，risks: {len(handle.risks)}）"
        note += "。结果已就绪，可直接整合；需要完整结构化汇报时用 collect_results。"
        return note
    if handle.status == "cancelled":
        return f"[子代理 #{handle.agent_id} 已取消] {title}。"
    _resume_hint = "进度已保存，可用 resume_subagent 从中断处续跑。" if handle.resumable else ""
    return (
        f"[子代理 #{handle.agent_id} 失败] {title}：{(handle.error or '未知原因')[:200]}。"
        f"{_resume_hint}如需细节可用 subagent_inspect 查看其上下文与轨迹。"
    )


async def _sync_task_status(db, session_id: int, task_id: int, status: str, note: str | None,
                            agent_id: int | None = None,
                            token_usage: int | None = None) -> None:
    """更新子任务状态并广播，前端任务面板据此实时刷新步骤与执行情况。

    先提交（子任务与主代理共享 session，创建任务的 flush 需随 commit 落库，
    否则前端 refreshTasks 通过 HTTP 查询时看不到新任务），再广播 task.updated。
    agent_id 传入时在终态（done/failed）附带 agent.completed 事件，
    前端消息流子代理卡片据此摘除转圈（此前仅靠 task.updated，探索期任务被置 running
    导致已完成卡片被 REST 回退成转圈）。
    """
    try:
        from app.orchestration.agent_events import broadcast
        from app.services import task_service
        # update_task_status 已经 WriteEngine 单写线程提交（无锁单写者），
        # 此处不再需要 async commit_with_retry（db 无未决写）。
        await task_service.update_task_status(db, task_id, status, note=note,
                                              token_usage=token_usage)
        await broadcast(session_id, {
            "event": "task.updated",
            "payload": {"task_id": task_id, "status": status, "note": note or ""},
        })
        if agent_id is not None and status in ("done", "failed", "cancelled"):
            await broadcast(session_id, {
                "event": "agent.completed",
                # plan-20261019-subagent-ghost: 显式标注这是子代理（主代理完成事件同样走
                # agent.completed），并带上会话 id 供前端做串会话防护。
                "payload": {"agent_id": agent_id, "kind": "sub",
                            "session_id": session_id, "status": status},
            })
    except Exception:
        logger.debug("[subagent] 子任务状态同步失败(非阻塞)", exc_info=True)
