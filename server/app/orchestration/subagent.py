"""子代理管理（v2：主代理按需 spawn，独立上下文 + 结果收集）。

简化实现：spawn 启动后台 agent_loop 任务；结果按 subagent_id 缓存于内存，
主代理通过 collect_results 工具轮询获取。持久化子代理线程消息于 messages(thread_id=agent.id)。
"""
import asyncio
import logging
import re
import time
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from app.orchestration.agent_loop import run_agent_loop
from app.orchestration.tools.registry import tool_registry
from app.persistence.database import async_session_factory, commit_with_retry

logger = logging.getLogger(__name__)


@dataclass
class SubagentHandle:
    agent_id: int
    task: object = None
    # v36: 归属 turn——每轮派发总量上限按此统计（含已完成/失败）
    turn_id: int | None = None
    status: str = "running"  # running / done / failed
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

    def spawn(self, db, *, agent, turn_id: int, task, handoff_summary: str,
              context_bundle, tool_schemas: list[dict], workspace: str,
              cancel_event: asyncio.Event | None = None, token_budget: int | None = None,
              task_title: str = "", task_description: str = "",
              context_snapshot: dict | None = None,
              reasoning_effort: str | None = None,
              subagent_mode: str | None = None,
              is_blocking: bool = False) -> int:
        """异步启动子代理 agent_loop，返回 subagent id。

        is_blocking（plan-330-1648 M4）：主代理是否正阻塞等待本子代理（spawn_and_wait）
        —— 用于禁止 report_to_leader(wait=true) 造成死锁。
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

        async def _run():
            try:
                # 问题3: 子代理用独立 AsyncSession —— 不再与主代理共享 session，
                # 避免多任务交错 flush/commit 踩踏产生 SQLite database is locked / PendingRollbackError。
                async with async_session_factory() as s:
                    # v9: 子任务启动即广播 in_progress（提交任务创建 + 状态），
                    # 前端任务面板/右上角卡片实时展示新任务的拆分步骤与执行情况。
                    # 此前子任务创建后从不更新状态，前端看不到子任务步骤执行进度。
                    await _sync_task_status(s, self.session_id, task.id, "in_progress", None)
                    out = await run_agent_loop(
                        s, session_id=self.session_id, turn_id=turn_id,
                        agent=agent, context_messages=context_bundle.to_messages(),
                        tool_schemas=tool_schemas, workspace=workspace,
                        cancel_event=cancel_event, token_budget=token_budget,
                        task_id=getattr(task, "id", None), model_id=getattr(agent, "model_id", None),
                        # plan-330-1648 M2: 思考深度随子代理类型设置透传（None = 全局默认）
                        reasoning_effort=reasoning_effort,
                        # plan-330-1648 M1: 运行模式按类型锁定（readonly/default），不继承会话模式
                        subagent_mode=subagent_mode,
                        # plan-330-1648 M4: 双向通信——①运行中可接收主代理追加指令（inbox 注入）；
                        # ②通过 subagent_comm 支持 report_to_leader 上报/提问。
                        injected_inputs_provider=(lambda _aid=agent.id: self.drain_agent_inputs(_aid)),
                        subagent_comm={
                            "manager": self,
                            "agent_id": agent.id,
                            "sync": bool(is_blocking),
                        },
                        # plan-19-82: 子代理压缩摘要同样跟随用户语言
                        reply_language=getattr(context_bundle, "reply_language", "auto"),
                        reply_language_source=getattr(context_bundle, "reply_language_source", "user"),
                    )
                    handle.status = "done" if out.kind == "message" else "failed"
                    handle.summary = out.text or ""
                    handle.error = out.error or ""
                    handle.artifact_ids = list(out.artifact_ids or [])
                    # v20: 探索子代理最终输出即结论文本（供主代理 wait 后直接整合）
                    handle.findings = out.text or ""
                    # plan-248-1258 M6: 解析结构化汇报（结果/变更文件/关键发现/风险）
                    _parse_structured_report(handle, out.text or "")
                    await _sync_task_status(
                        s, self.session_id, task.id,
                        "done" if handle.status == "done" else "failed",
                        (out.text or "")[:300] or None,
                        agent_id=agent.id,
                        # v36 (plan-321-1600 M1): 累加子代理 token 用量（此前恒为 0）
                        token_usage=getattr(out, "tokens", 0) or 0,
                    )
            except asyncio.CancelledError:
                handle.status = "cancelled"
                handle.error = "用户中断"
                async with async_session_factory() as s:
                    await _sync_task_status(s, self.session_id, task.id, "cancelled", "用户中断", agent_id=agent.id)
                # plan-330-1648 M7: 失败/取消对外可见（卡片与右面板显示原因，不再只留红叉）
                await _broadcast_subagent_failed(self.session_id, agent.id, "用户中断", "cancelled")
                raise
            except Exception as e:
                handle.status = "failed"
                handle.error = str(e)
                async with async_session_factory() as s:
                    await _sync_task_status(s, self.session_id, task.id, "failed", f"执行异常: {str(e)[:200]}", agent_id=agent.id)
                logger.exception("[subagent] %s 异常", agent.id)
                await _broadcast_subagent_failed(self.session_id, agent.id, str(e), "failed")
            # v956：子代理 loop 结束前 flush 其消息（与主代理共用 turn buffer）
            try:
                from app.persistence.write_behind import write_behind
                await write_behind.get(self.session_id, turn_id).flush()
            except Exception:
                logger.debug("[subagent] flush turn buffer failed agent=%s", agent.id, exc_info=True)
            # v36 (plan-321-1600 M3): 终态入队“完成通知”——主代理下次 LLM 调用前自动收到，
            # 无需轮询 collect_results（已读集合命中则跳过）。
            self._enqueue_completion(handle)
            # 修复：主代理 turn 已结束时仅入队通知没有任何动作送达（要等用户下一条消息）
            # ——表现为“子代理跑完但主代理没被唤醒”。终态回调让 engine 在会话空闲时
            # 自动创建续跑 turn，完成报告随新 turn 的注入通道送达主代理。
            self.notify_finished(handle)
            logger.info("[subagent] %s 完成 status=%s", agent.id, handle.status)

        handle.task = asyncio.create_task(_run())
        return agent.id

    async def spawn_and_wait(self, db, *, agent, turn_id: int, task, handoff_summary: str,
                             context_bundle, tool_schemas: list[dict], workspace: str,
                             cancel_event: asyncio.Event | None = None,
                             token_budget: int | None = None,
                             task_title: str = "", task_description: str = "",
                             context_snapshot: dict | None = None,
                             reasoning_effort: str | None = None,
                             subagent_mode: str | None = None) -> SubagentHandle:
        """同步启动并等待子代理完成，返回已填充结果的 handle。

        v20: 探索子代理（只读调研）用——主代理调用 spawn_subagent(explore=true) 后
        直接拿到结论文本（handle.findings），不必再轮询 collect_results。
        """
        handle_id = self.spawn(
            db, agent=agent, turn_id=turn_id, task=task,
            handoff_summary=handoff_summary, context_bundle=context_bundle,
            tool_schemas=tool_schemas, workspace=workspace,
            cancel_event=cancel_event, token_budget=token_budget,
            task_title=task_title, task_description=task_description,
            context_snapshot=context_snapshot,
            reasoning_effort=reasoning_effort,
            subagent_mode=subagent_mode,
            is_blocking=True,
        )
        handle = self._handles.get(handle_id)
        if handle is not None and handle.task is not None:
            await handle.task
        return handle

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
            if h.status in ("done", "failed"):
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
        return sum(1 for h in self._handles.values() if h.turn_id == turn_id)

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
        return sum(1 for h in self._handles.values() if h.status == "running")

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
    return (
        f"[子代理 #{handle.agent_id} 失败] {title}：{(handle.error or '未知原因')[:200]}。"
        "如需细节可用 subagent_inspect 查看其上下文与轨迹。"
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
                "payload": {"agent_id": agent_id, "status": status},
            })
    except Exception:
        logger.debug("[subagent] 子任务状态同步失败(非阻塞)", exc_info=True)
