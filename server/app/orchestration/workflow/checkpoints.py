"""工作流节点的增量检查点日志（设计 §6.2）。

子代理每进入一个执行步骤，就把新增的消息追加到节点尝试的日志里：
- 尝试开始时先记录运行参数（spec），再记录一份完整快照（snapshot）；
- 之后每一步只追加增量（delta）；消息列表被压缩重建（变短）时追加新的快照。

进程被强制结束后，续跑先从日志还原检查点（最近快照 + 其后的增量），再交给既有的续跑逻辑。
这样最多丢失一次尚未返回的模型调用，而不是整个节点已完成的上下文。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

from sqlalchemy import func, select

from app.orchestration.workflow import store
from app.orchestration.workflow.effects import VERDICT_LABEL, effect_verdict
from app.persistence.database import async_session_factory, run_write_locked
from app.persistence.models.workflow import WorkflowAttempt, WorkflowMessage

logger = logging.getLogger(__name__)


async def _append(attempt_id: int, kind: str, payload: list[Any]) -> None:
    def _op(s):
        last = (
            s.query(func.max(WorkflowMessage.seq))
            .filter(WorkflowMessage.attempt_id == attempt_id)
            .scalar()
        ) or 0
        s.add(WorkflowMessage(attempt_id=attempt_id, seq=last + 1, kind=kind, payload_json=payload))
        s.commit()

    await run_write_locked(_op, label="workflow.checkpoint.append")


class StepRecorder:
    """单个尝试的日志写入器。

    增量在调用线程里即时计算（只看消息条数，开销很小）；写入按提交顺序串行执行，
    保证日志的序号与步骤顺序一致。
    """

    def __init__(self, attempt_id: int) -> None:
        self.attempt_id = attempt_id
        self._persisted = 0
        self._started = False
        self._last: asyncio.Task | None = None

    def record(self, dumped: list[dict[str, Any]], spec: dict[str, Any]) -> None:
        """记录一个执行步骤结束时的完整消息列表（已序列化）。"""
        if not self._started:
            self._started = True
            self._schedule("spec", [spec])
            self._schedule("snapshot", dumped)
        elif len(dumped) < self._persisted:
            self._schedule("snapshot", dumped)  # 压缩重建：列表变短，写完整快照
        elif len(dumped) > self._persisted:
            self._schedule("delta", dumped[self._persisted :])
        else:
            return
        self._persisted = len(dumped)

    def _schedule(self, kind: str, payload: list[Any]) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # 无事件循环（关停路径）：不记录，不影响运行
        previous = self._last

        async def _run() -> None:
            if previous is not None:
                with contextlib.suppress(Exception):
                    await previous
            try:
                await _append(self.attempt_id, kind, payload)
            except Exception:
                logger.warning(
                    "[workflow] 增量检查点写入失败 attempt=%s kind=%s",
                    self.attempt_id,
                    kind,
                    exc_info=True,
                )

        self._last = loop.create_task(_run())


async def materialize(
    node_id: int, *, exclude_attempt_id: int | None = None
) -> dict[str, Any] | None:
    """还原节点最近一次有日志的尝试的检查点（与 resume_subagent 使用的结构一致）。

    没有日志返回 None。
    """
    async with async_session_factory() as s:
        attempt_ids = (
            (
                await s.execute(
                    select(WorkflowAttempt.id)
                    .where(WorkflowAttempt.node_id == node_id)
                    .order_by(WorkflowAttempt.id.desc())
                )
            )
            .scalars()
            .all()
        )
        for attempt_id in attempt_ids:
            if attempt_id == exclude_attempt_id:
                continue
            rows = (
                (
                    await s.execute(
                        select(WorkflowMessage)
                        .where(WorkflowMessage.attempt_id == attempt_id)
                        .order_by(WorkflowMessage.seq)
                    )
                )
                .scalars()
                .all()
            )
            if not rows:
                continue
            spec: dict[str, Any] | None = None
            messages: list[Any] = []
            for row in rows:
                payload = list(row.payload_json or [])
                if row.kind == "spec":
                    spec = dict(payload[0]) if payload else None
                elif row.kind == "snapshot":
                    messages = payload
                elif row.kind == "delta":
                    messages.extend(payload)
            if spec is None or not messages:
                return None
            return {
                "messages": messages,
                "spec": spec,
                "reason": "增量日志还原",
                "saved_at": store.now_iso(),
            }
    return None


def reconcile_unknown_results(messages: list[Any]) -> tuple[list[Any], list[str]]:
    """续跑前的结果对账：找出“已发出调用、检查点里却没有结果”的工具调用。

    这类调用在中断时可能已经执行、也可能没有执行，不能假定结果。这里为每个这样的调用补上一条
    如实的结果说明（代替配对修复默认写入的“因上下文压缩丢失”占位），并返回逐条描述，
    供续跑说明提示模型先核实这些操作的实际状态。
    """
    answered = {
        m.get("tool_call_id") for m in messages if isinstance(m, dict) and m.get("role") == "tool"
    }
    out: list[Any] = []
    unknown: list[str] = []
    for m in messages:
        out.append(m)
        if not isinstance(m, dict) or m.get("role") != "assistant" or not m.get("tool_calls"):
            continue
        for call in m["tool_calls"]:
            call_id = call.get("id")
            if not call_id or call_id in answered:
                continue
            name = str(call.get("name") or "unknown")
            unknown.append(_describe_call(name, call.get("arguments")))
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": name,
                    "content": (
                        f"[系统提示: 工具 {name} 在中断时已发出调用，结果未知"
                        "（可能已执行，也可能未执行）。请先核实其实际影响，再依赖它做决策]"
                    ),
                }
            )
    return out, unknown


def _describe_call(name: str, arguments: Any) -> str:
    if isinstance(arguments, dict):
        text = json.dumps(arguments, ensure_ascii=False)
    else:
        text = str(arguments or "")
    return f"{name} {text[:120]}".rstrip()


async def ledger_notes(node_id: int, messages: list[Any]) -> tuple[list[str], list[str]]:
    """对账工具台账：返回 (结果未知的操作, 已执行但结果未进入对话记录的操作)。

    对话记录里已经出现过的调用（无论结果是否已记录）不重复提示，它们由悬空调用的对账处理。
    """
    recorded_calls: set[str] = set()
    answered: set[str] = set()
    for m in messages:
        if not isinstance(m, dict):
            continue
        if m.get("role") == "assistant":
            recorded_calls.update(
                str(tc.get("id")) for tc in m.get("tool_calls") or [] if tc.get("id")
            )
        elif m.get("role") == "tool" and m.get("tool_call_id"):
            answered.add(str(m["tool_call_id"]))
    unknown: list[str] = []
    unrecorded: list[str] = []
    reconciled: list[int] = []
    for call in await store.node_tool_calls(node_id):
        if call["status"] == "reconciled":
            continue
        key = call["llm_call_id"]
        if key and (key in answered or key in recorded_calls):
            continue
        text = f"{call['tool']} {call['args_preview']}".rstrip()
        if call["status"] == "started":
            verdict = await effect_verdict(call.get("effect_json"))
            if verdict is not None:
                text = f"{text}（{VERDICT_LABEL[verdict]}）"
            unknown.append(text)
        elif call["status"] == "done":
            unrecorded.append(text)
        elif call["status"] == "failed":
            unrecorded.append(f"{text}（失败）")
        reconciled.append(call["id"])
    await store.mark_tool_calls_reconciled(reconciled)
    return unknown, unrecorded


def resume_note(unknown: list[str], unrecorded: list[str], feedback: str | None) -> str | None:
    """续跑时附加在检查点末尾的补充说明：结果未知的操作、已执行但结果未记录的操作与用户 / 返工反馈。

    没有内容时返回 None。
    """
    parts: list[str] = []
    if unknown:
        parts.append(
            "以下操作在中断时已发出，结果未知："
            + "；".join(unknown)
            + "。标注“已确认未执行”的可以重做，标注“已确认已执行”的不要重复执行，"
            "其余请先核实实际状态再继续。"
        )
    if unrecorded:
        parts.append(
            "以下操作已经执行，但结果没有保存到对话记录中："
            + "；".join(unrecorded)
            + "。请以当前文件与状态为准，不要重复执行。"
        )
    if feedback:
        parts.append(feedback)
    return "\n".join(parts) or None


async def install(agent_id: int, checkpoint: dict[str, Any]) -> bool:
    """把还原出的检查点写入子代理行（仅当它比现有检查点更完整时）；返回是否写入。"""
    from app.persistence.models.agent import Agent as AgentRow

    def _op(s):
        row = s.get(AgentRow, agent_id)
        if row is None:
            return False
        current = row.checkpoint_json or {}
        have = len(current.get("messages") or [])
        if have >= len(checkpoint["messages"]):
            return False
        row.checkpoint_json = checkpoint
        s.commit()
        return True

    return await run_write_locked(_op, label="workflow.checkpoint.install")
