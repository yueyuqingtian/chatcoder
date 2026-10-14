"""工作流节点的真实执行器（设计 §5.5）：以子代理执行一个节点。

- 派发：复用 SubagentManager.spawn，与普通子代理同一套线程、事件与面板；
  上下文由 build_subagent_context 构建；
- 续跑（checkpoint）：resume_subagent 从检查点继续，已完成步骤不重做；
- 返工 / 格式修复（rework / repair）：continue_subagent 在同一子代理上下文中追加说明续做；
- 上下文已不可用时回落为新建子代理，并把反馈写进任务说明；
- 工作流节点子代理标记 internal：完成后不推送主代理、不触发唤醒，结果由运行时收取。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

from app.orchestration.workflow import checkpoints, store
from app.orchestration.workflow.executor import NodeRequest, NodeResult

logger = logging.getLogger(__name__)

READONLY_KINDS = frozenset({"explore", "review"})
UPSTREAM_BUDGET_CHARS = 6000
# 停滞判定（§5.5）：轮询间隔、停滞阈值与心跳落库节流（秒）。
# 进展信号 = 工具步骤数、子代理状态与等待重试；超过阈值无进展即停止子代理，按瞬时故障重试。
POLL_SEC = 2.0
STALL_TIMEOUT_SEC = 600.0
HEARTBEAT_TOUCH_SEC = 5.0


def _attach_recorder(manager: Any, agent_id: int, req: NodeRequest) -> None:
    """把本次尝试的增量检查点写入器挂到子代理上：每个执行步骤记录一次（设计 §6.2）。"""
    from app.orchestration.subagent import _dump_message

    handle = manager.get(agent_id)
    if handle is None or req.attempt_id is None:
        return
    handle.workflow_attempt_id = req.attempt_id
    recorder = checkpoints.StepRecorder(req.attempt_id)

    def _listen(msgs: list) -> None:
        dumped = [d for d in (_dump_message(m) for m in msgs) if d]
        recorder.record(dumped, dict(handle.run_spec or {}))

    handle.on_step = _listen


def _tokens_of(manager: Any, agent_id: int | None) -> int:
    handle = manager.get(agent_id) if agent_id is not None else None
    return int(handle.tokens_used) if handle is not None else 0


async def _cancel_and_wait(handle: Any) -> None:
    """停止子代理并等待其保存检查点（驱动循环在取消时写入续跑所需的状态）。"""
    handle.task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await handle.task


def _title(req: NodeRequest) -> str:
    base = f"{req.flow_key}·{req.node_key}" if req.flow_key else req.node_key
    return base[:40]


def _description(req: NodeRequest) -> str:
    lines = [req.task.strip() or f"完成『{req.node_key}』节点的工作"]
    if req.acceptance:
        lines.append("验收标准：")
        lines.extend(f"- {item}" for item in req.acceptance)
    if req.write_scope and req.kind in ("execute", "merge"):
        lines.append("只能修改以下路径（越界写入会被拒绝）：" + "、".join(req.write_scope))
    if req.kind == "review":
        lines.append(
            "请以一个 JSON 对象作为最终结论：decision（pass / rework / escalate）、summary、"
            "criteria（[{id, result: met|unmet|unverified, evidence}]）、"
            "issues（[{severity: blocker|major|minor, file, line, problem, fix_hint}]）。"
        )
    if req.feedback and req.mode == "fresh":
        lines.append(f"上一次执行的反馈：{req.feedback}")
    return "\n".join(lines)


def _handoff(req: NodeRequest) -> str:
    parts = [
        f"工作流程序目标：{req.goal}",
        f"所属子工作流：{req.flow_key}",
        f"节点：{req.node_key}（{req.kind}）",
    ]
    for key, item in req.upstream.items():
        body = json.dumps(item.get("output") or {}, ensure_ascii=False)
        parts.append(f"【上游『{key}』产出】{body}")
    return "\n".join(parts)[:UPSTREAM_BUDGET_CHARS]


def _to_result(handle: Any, agent_id: int) -> NodeResult:
    from app.orchestration.agent_loop import _is_transient_failure_text

    text = handle.summary or handle.findings or ""
    if handle.status == "done":
        output = {
            "summary": text[:2000],
            "findings": (handle.findings or "")[:4000],
            "files_touched": list(handle.files_touched or []),
            "risks": list(handle.risks or []),
            "verification": list(handle.verification or []),
            "acceptance": list(handle.acceptance or []),
        }
        return NodeResult(status="done", text=text, output=output, agent_id=agent_id)
    if handle.status == "cancelled":
        return NodeResult(status="interrupted", error=handle.error or "已取消", agent_id=agent_id)
    if handle.status == "paused":
        error = handle.error or handle.pause_reason or "已暂停"
        return NodeResult(
            status="interrupted",
            error=error,
            transient=_is_transient_failure_text(error),
            agent_id=agent_id,
        )
    error = handle.error or "子代理执行失败"
    return NodeResult(
        status="failed",
        error=error,
        transient=_is_transient_failure_text(error),
        agent_id=agent_id,
    )


async def _mark_attempt(req: NodeRequest, label: str) -> None:
    """子代理线程里标出新一次尝试的分隔线（设计 §5.5）：系统消息，界面渲染为分割线。"""
    from app.core.enums import MsgType, SenderType
    from app.persistence.database import async_session_factory
    from app.services import message_service

    if req.agent_id is None:
        return
    try:
        async with async_session_factory() as db:
            await message_service.create_message(
                db,
                session_id=req.session_id,
                sender_type=SenderType.SYSTEM.value,
                msg_type=MsgType.SYSTEM.value,
                content={"text": f"第 {req.attempt_no} 次尝试 · {label}"},
                turn_id=req.turn_id,
                thread_id=req.agent_id,
            )
            await db.commit()
    except Exception:  # 分隔线只是辅助信息
        logger.warning("[workflow] 写入尝试分隔线失败", exc_info=True)


class SubagentNodeExecutor:
    """以子代理执行工作流节点。实现 executor.NodeExecutor 协议。"""

    async def run(self, req: NodeRequest) -> NodeResult:
        from app.orchestration.subagent import (
            continue_subagent,
            get_subagent_manager,
            resume_subagent,
        )

        manager = get_subagent_manager(req.session_id)
        # 本次尝试的 token 用量 = 子代理累计用量 - 尝试开始时的累计值（同一子代理跨尝试复用）
        baseline = _tokens_of(manager, req.agent_id)
        if req.agent_id is not None and req.mode == "checkpoint":
            # 进程被强制结束时子代理行可能没有最新检查点：先从增量日志还原，再续跑
            recovered = await checkpoints.materialize(
                req.node_id, exclude_attempt_id=req.attempt_id
            )
            unknown: list[str] = []
            unrecorded: list[str] = []
            if recovered is not None:
                # 续跑前对账：对话记录中悬空的调用补上如实的说明；
                # 台账里未进入对话记录的调用逐条列出
                messages, dangling = checkpoints.reconcile_unknown_results(
                    recovered.get("messages") or []
                )
                ledger_unknown, unrecorded = await checkpoints.ledger_notes(req.node_id, messages)
                unknown = [*dangling, *ledger_unknown]
                recovered = {**recovered, "messages": messages}
                await checkpoints.install(req.agent_id, recovered)
            state, detail = await resume_subagent(
                req.session_id,
                req.agent_id,
                turn_id=req.turn_id or 0,
                extra_note=checkpoints.resume_note(unknown, unrecorded, req.feedback),
            )
            if state in ("resumed", "already_running"):
                await _mark_attempt(req, "续跑")
                _attach_recorder(manager, req.agent_id, req)
                return await self._wait(manager, req.agent_id, req, baseline)
            logger.info("[workflow] 检查点续跑不可用（%s），回落为新建子代理", detail)
        elif req.agent_id is not None and req.mode in ("rework", "repair"):
            state, detail = await continue_subagent(
                req.session_id,
                req.agent_id,
                turn_id=req.turn_id or 0,
                note=req.feedback,
            )
            if state in ("resumed", "already_running"):
                label = "返工" if req.mode == "rework" else "修复格式"
                await _mark_attempt(req, label)
                _attach_recorder(manager, req.agent_id, req)
                return await self._wait(manager, req.agent_id, req, baseline)
            logger.info("[workflow] 同一子代理上下文不可用（%s），回落为新建子代理", detail)
        return await self._spawn(manager, req)

    async def _wait(
        self, manager: Any, agent_id: int, req: NodeRequest, baseline: int = 0
    ) -> NodeResult:
        handle = manager.get(agent_id)
        if handle is None or handle.task is None:
            return NodeResult(status="failed", error="子代理句柄丢失", agent_id=agent_id)
        loop = asyncio.get_running_loop()
        last_sig: tuple[int, str, bool] | None = None
        last_progress = loop.time()
        last_touch = float("-inf")
        try:
            while True:
                done, _pending = await asyncio.wait({handle.task}, timeout=POLL_SEC)
                if done:
                    break
                now = loop.time()
                sig = (len(handle.trajectory), handle.status, bool(handle.waiting))
                if sig != last_sig or handle.waiting:
                    # 有新的工具步骤、状态变化，或处于网络等待重试（属正常等待，不计停滞）
                    last_sig = sig
                    last_progress = now
                    if now - last_touch >= HEARTBEAT_TOUCH_SEC:
                        last_touch = now
                        await store.touch_heartbeat(req.node_id)
                elif now - last_progress >= STALL_TIMEOUT_SEC:
                    logger.warning(
                        "[workflow] 节点 %s 停滞超过 %.0f 秒，停止子代理后按瞬时故障重试",
                        req.node_id,
                        STALL_TIMEOUT_SEC,
                    )
                    handle.pause_reason = "节点长时间没有进展，已停止以便重试"
                    await _cancel_and_wait(handle)
                    return NodeResult(
                        status="failed",
                        error="节点长时间没有进展（停滞），已停止并将重试",
                        transient=True,
                        agent_id=agent_id,
                        tokens=max(0, handle.tokens_used - baseline),
                    )
        except asyncio.CancelledError:
            # 工作流被暂停：同时停止子代理，并等它保存检查点后再向上抛出取消
            handle.pause_reason = handle.pause_reason or "工作流已暂停"
            await _cancel_and_wait(handle)
            raise
        result = _to_result(handle, agent_id)
        result.tokens = max(0, handle.tokens_used - baseline)
        return result

    async def _spawn(self, manager: Any, req: NodeRequest) -> NodeResult:
        from app.core.enums import MsgType, SenderType
        from app.orchestration.context_manager import build_subagent_context
        from app.orchestration.subagent_tools import EXPLORE_TOOLS
        from app.orchestration.tools.registry import tool_registry
        from app.persistence.database import async_session_factory, run_write_locked
        from app.persistence.models.agent import Agent as AgentRow
        from app.persistence.models.message import Session as SessionRow
        from app.persistence.models.project import Project as ProjectRow
        from app.persistence.models.task import Task as TaskRow
        from app.services import message_service

        readonly = req.kind in READONLY_KINDS
        title = _title(req)
        description = _description(req)
        handoff = _handoff(req)

        def _create(s: Any) -> tuple[int, int]:
            session_row = s.get(SessionRow, req.session_id)
            agent = AgentRow(
                kind="sub",
                name=title,
                model_id=session_row.model_id if session_row else None,
                session_id=req.session_id,
                turn_id=req.turn_id,
                parent_agent_id=None,
                workflow_node_id=req.node_id,
                status="running",
            )
            s.add(agent)
            s.flush()
            task = TaskRow(
                session_id=req.session_id,
                turn_id=req.turn_id,
                title=title,
                description=description,
                acceptance_criteria="；".join(req.acceptance),
                agent_id=agent.id,
                parent_task_id=None,
                priority=1,
                kind="step",
                status="pending",
            )
            s.add(task)
            s.flush()
            s.commit()
            return agent.id, task.id

        agent_id, task_id = await run_write_locked(_create, label="workflow.node.spawn")
        schemas = (
            tool_registry.all_schemas(EXPLORE_TOOLS) if readonly else tool_registry.all_schemas()
        )
        if not readonly:
            # 可写节点可在执行中申请扩展写入范围（设计 §5A.1 第 4 点）
            from app.orchestration.workflow.guard import REQUEST_SCOPE_SCHEMA

            schemas = [*schemas, REQUEST_SCOPE_SCHEMA]

        # 派发时刻的锚点是线程首条消息的 id（见 agent_loop._agent_started_extra）：先把已入队的主线程消息
        # 全部落库（屏障），使派发前产生的消息 id 都小于它，前端据此把节点卡放在派发前最后一条消息之后。
        from app.persistence.write_behind import write_behind as _write_behind

        await _write_behind.flush_all()
        async with async_session_factory() as db:
            agent_row = await db.get(AgentRow, agent_id)
            task_row = await db.get(TaskRow, task_id)
            session_row = await db.get(SessionRow, req.session_id)
            project_row = None
            if session_row is not None and session_row.project_id:
                project_row = await db.get(ProjectRow, session_row.project_id)
            bundle = await build_subagent_context(
                db,
                agent=agent_row,
                session=session_row,
                project=project_row,
                task=task_row,
                handoff_summary=handoff,
                original_request=req.goal,
            )
            # 任务说明落库到子代理线程：子代理面板首条即“任务从哪来”，与普通子代理一致
            await message_service.create_message(
                db,
                session_id=req.session_id,
                turn_id=req.turn_id,
                thread_id=agent_id,
                sender_type=SenderType.USER.value,
                msg_type=MsgType.TEXT.value,
                content={"text": f"{title}\n\n{description}".strip()},
            )
            await db.commit()

        manager.spawn(
            None,
            agent=agent_row,
            turn_id=req.turn_id,
            task=task_row,
            handoff_summary=handoff,
            context_bundle=bundle,
            tool_schemas=schemas,
            workspace=req.workspace,
            subagent_mode="readonly" if readonly else "default",
            is_blocking=False,
            task_title=title,
            task_description=description,
        )
        handle = manager.get(agent_id)
        if handle is not None:
            handle.internal = True
            handle.workflow_node_id = req.node_id
        _attach_recorder(manager, agent_id, req)
        return await self._wait(manager, agent_id, req)
