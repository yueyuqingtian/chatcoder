"""工作流运行时（设计 §5.3–§5.7）：调度、节点生命周期、评估返工、人工闸门、暂停与续跑。

运行时是会话级后台任务：一个调度入口（tick）统一为所有运行中的程序挑选可启动节点；
每个节点在独立协程中由执行器执行，完成后由 _settle 统一落库、释放名额与租约、推进状态。
状态以数据库为准，内存只保存任务句柄、准入元数据与重试计时器。
"""

from __future__ import annotations

import asyncio
import copy
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.orchestration.workflow import conflict_events, isolation, report, store
from app.orchestration.workflow.commands import CommandGate, gate_check_command, run_check_command
from app.orchestration.workflow.conflicts import LeaseOwner, WriteLeaseTable, get_lease_table
from app.orchestration.workflow.drift import (
    ABSENT,
    contents_of,
    diff_fingerprints,
    files_of,
    fingerprint_record,
    head_of_record,
    read_text_for_diff,
    snapshot_contents,
    snapshot_fingerprint,
    unified_diff,
)
from app.orchestration.workflow.executor import NodeExecutor, NodeRequest, NodeResult
from app.orchestration.workflow.graph import (
    DEFAULT_MAX_REWORK,
    INTEGRATION_FLOW_KEY,
    parse_plan,
    validate_plan,
)
from app.orchestration.workflow.guard import forget_agent, pop_gate
from app.orchestration.workflow.probe import probe_session_model
from app.orchestration.workflow.scheduler import (
    REASON_FLOW_FULL,
    REASON_GLOBAL_FULL,
    REASON_GRANTED,
    REASON_SESSION_FULL,
    PoolLimits,
    Slot,
    WorkflowPool,
)
from app.orchestration.workflow.state import (
    NodeView,
    derive_flow_status,
    derive_program_status,
    ready_node_keys,
)
from app.orchestration.workflow.verdict import (
    ReviewDecision,
    decide_review,
    issues_as_feedback,
    parse_verdict,
    rereview_note,
    validate_verdict,
)

REPAIR_HINT = (
    "上一条输出不符合评估契约。请只输出一个 JSON 对象"
    "（decision / summary / criteria / issues），不要附加其它文字。"
)

logger = logging.getLogger(__name__)

_POOL_REASON_TEXT = {
    # plan-workflow-collab：排队原因用通俗说法，不暴露“并发名额 / 上限”等调度术语
    REASON_GLOBAL_FULL: "排队中：同时进行的步骤较多，空出后自动开始",
    REASON_SESSION_FULL: "排队中：本会话同时进行的步骤较多，空出后自动开始",
    REASON_FLOW_FULL: "排队中：等这个子工作流里正在进行的步骤先完成",
}

_QUEUED_TEXT = "排队中，即将开始"
_TERMINAL_FLOW = frozenset({"succeeded", "failed", "cancelled"})
# 同一节点在一次运行中最多追加的合并步骤数（L3）；超过后直接转入用户决定（L4）
MAX_MERGE_STEPS = 2
# 网络中断后自动续跑前的等待（秒）：给网络恢复留出时间
AUTO_RESUME_DELAY_SEC = 60.0

Broadcast = Callable[[int, dict[str, Any]], Awaitable[None]]
CommandRunner = Callable[[str, str], Awaitable[dict[str, Any]]]
# 检查命令的权限门控：(命令, session_id=, workspace=, origin=) → 放行 / 不运行及原因
CommandGateFn = Callable[..., Awaitable[CommandGate]]
# 程序关键状态变化时通知主代理（会话、文本）：空闲则唤醒一轮，忙则注入当前轮
Notify = Callable[[int, str], Awaitable[None]]
# 续跑 / 重试前的模型可达性预检：(可达, 不可达原因)，参数为会话编号
ModelProbe = Callable[[int | None], Awaitable[tuple[bool, str]]]


async def _no_notify(session_id: int, text: str) -> None:
    return None


def _token_budget_exhausted(snap: dict[str, Any]) -> bool:
    budget = int(snap["run"]["token_budget"] or 0)
    return budget > 0 and int(snap["run"]["token_used"] or 0) >= budget


def _budget_reason(snap: dict[str, Any]) -> str:
    used = int(snap["run"]["token_used"] or 0)
    budget = int(snap["run"]["token_budget"] or 0)
    return f"token 预算已用完（已用 {used:,} / 预算 {budget:,}），已保留进度，调高预算后可继续"


def _budget_notice(snap: dict[str, Any]) -> str:
    return (
        f"[系统通知] 工作流『{snap['run']['title']}』的 token 预算已用完，已暂停并保留进度。"
        "请告知用户：可在设置 → 工作流中调高预算后继续，或放弃该工作流。"
    )


def _nodes_of(snap: dict[str, Any]) -> list[dict[str, Any]]:
    return [node for flow in snap["flows"] for node in flow["nodes"]]


def _succeeded_notice(snap: dict[str, Any]) -> str:
    nodes = _nodes_of(snap)
    done = sum(1 for n in nodes if n["status"] in ("done", "skipped"))
    remaining = report.remaining_issue_count(snap)
    tail = (
        f"评估留下 {remaining} 个未返工的问题（见报告中的“遗留问题”）：请先自行核实哪些是真问题，"
        "再向用户说明并询问是否需要整改；用户确认整改时可直接修改，或规划一个新的工作流。"
        if remaining
        else "询问用户是否需要后续调整。"
    )
    return (
        f"[系统通知] 工作流『{snap['run']['title']}』已全部完成（{done}/{len(nodes)} 步）。"
        "以下是过程报告。请向用户汇报：各子工作流的产出与关键改动、验证结果、"
        "是否返工及原因、每轮评估发现的问题与整改情况、剩余风险。"
        + tail
        + "\n\n"
        + report.program_report(snap)
    )


def _failed_notice(snap: dict[str, Any]) -> str:
    failed = next((n for n in _nodes_of(snap) if n["status"] == "failed"), None)
    detail = ""
    if failed is not None:
        message = (failed.get("error") or {}).get("message") or "执行失败"
        detail = f"（节点『{failed['key']}』：{message}）"
    return (
        f"[系统通知] 工作流『{snap['run']['title']}』执行失败{detail}。"
        "请向用户说明原因与已完成的进展，并询问是重试该节点、从断点继续，还是调整计划。"
        "\n\n" + report.program_report(snap)
    )


_GATE_GUIDE = (
    "请这样处理：1. 先核实——评估问题是否成立（读相关代码、必要时运行命令，"
    "或用 subagent_inspect 查看评估子代理的过程）；"
    "2. 向用户汇报：哪些是真问题、哪些可能是误判或次要问题，"
    "给出你的建议（返工 / 采纳 / 中止）；3. 等用户确认后，调用 decide_workflow 提交决定，"
    "用户的补充要求写进 feedback（用户附了图片或文件时，把要点与附件编号一并写进去）。"
    "不要替用户做决定；用户已在工作流卡片上直接决定时无需再调用。"
)


def _gate_notice(snap: dict[str, Any], node_id: int | None = None) -> str:
    gates = report.pending_gates(snap)
    if node_id is not None:
        keys = {
            (f["run"]["package_key"], n["key"])
            for f in snap["flows"]
            for n in f["nodes"]
            if n["id"] == node_id
        }
        gates = [g for g in gates if (g["flow_key"], g["node_key"]) in keys] or gates
    if not gates:
        return f"[系统通知] 工作流『{snap['run']['title']}』需要用户决定，请向用户说明情况。"
    blocks = "\n".join(report.gate_block(g) for g in gates)
    return (
        f"[系统通知] 工作流『{snap['run']['title']}』有步骤在等待确认，相关子工作流已暂停推进"
        "（其它子工作流照常运行）：\n" + blocks + "\n" + _GATE_GUIDE
    )


def _default_retry_plan() -> list[float]:
    from app.orchestration.agent_loop import _retry_plan  # 延迟导入：避免加载整条代理循环

    return _retry_plan()


class WorkflowLocalChangesError(ValueError):
    """开始独立工作树子工作流时，主工作区有未提交的改动：需用户确认（工作树基于最近一次提交）。"""

    def __init__(self, files: list[str]) -> None:
        self.files = files
        super().__init__("主工作区有未提交的改动，独立工作树看不到这些改动")


class WorkflowDriftError(ValueError):
    """暂停期间写入范围内的文件被外部改动：续跑前需用户确认（§6.6）。"""

    def __init__(self, files: list[str]) -> None:
        self.files = files
        shown = "、".join(files[:3])
        more = f" 等 {len(files)} 个文件" if len(files) > 3 else ""
        super().__init__(
            f"工作区在暂停期间有改动（{shown}{more}），继续可能覆盖这些改动，请确认后再继续"
        )


_TOO_LARGE_TO_DIFF = "文件较大或不是 UTF-8 文本，无法展示差异"


class WorkflowPlanInvalidError(ValueError):
    """替换模板后计划未通过校验（如成环、写入范围重叠）：附带诊断，界面逐条展示。"""

    def __init__(self, errors: list[dict[str, Any]]) -> None:
        self.errors = errors
        self.message = errors[0]["message"] if errors else "计划未通过校验"
        super().__init__(self.message)


class WorkflowModelUnavailableError(ValueError):
    """续跑或重试前模型不可达：保持暂停、不改动节点，也不消耗重试次数（§6.3 第 2 步）。"""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"模型暂时不可用（{reason}），已保留进度，稍后再试")


# 运行中修订计划的次数上限（设计 §4.5：最多 3 次修订，版本号从 1 开始）
MAX_PLAN_REVISIONS = 3
_NODE_SPEC_FIELDS = (
    "kind",
    "role",
    "task",
    "inputs",
    "acceptance",
    "write_scope",
    "commands",
    "rework_to",
    "max_rework",
    "question",
)


def _node_spec(node: dict[str, Any]) -> dict[str, Any]:
    """节点的规格部分：用于判断已开始的节点是否被修订改动。"""
    key = node.get("key") or node.get("node_key")
    spec: dict[str, Any] = {"title": node.get("title") or key}
    for name in _NODE_SPEC_FIELDS:
        value = node.get(name)
        spec[name] = list(value) if isinstance(value, list) else (value if value != "" else None)
    return spec


def _frozen_violation(snap: dict[str, Any], new_plan: dict[str, Any]) -> str | None:
    """已开始（非 pending）的子工作流与节点必须保持原样；返回首个违规说明，没有则 None。"""
    new_flows = {f["key"]: f for f in new_plan["flows"]}
    for flow in snap["flows"]:
        key = flow["run"]["package_key"]
        started = [n for n in flow["nodes"] if n["status"] != "pending"]
        if not started:
            continue
        if key not in new_flows:
            return f"子工作流『{key}』已有节点开始执行，不能删除"
        new_flow = new_flows[key]
        if sorted(new_flow.get("depends_on") or []) != sorted(flow["run"].get("depends_on") or []):
            return f"子工作流『{key}』已有节点开始执行，不能修改依赖"
        new_nodes = {n["key"]: n for n in new_flow["nodes"]}
        for node in started:
            new_node = new_nodes.get(node["key"])
            if new_node is None:
                return f"节点『{key}/{node['key']}』已开始执行，不能删除"
            if _node_spec(new_node) != _node_spec(node):
                return f"节点『{key}/{node['key']}』已开始执行，不能修改；只能调整尚未开始的步骤"
    return None


def _scope_patterns(snap: dict[str, Any]) -> list[str]:
    """程序的写入范围并集：只有 execute 节点会写文件，其余节点只读。"""
    return sorted(
        {
            pattern
            for flow in snap["flows"]
            for node in flow["nodes"]
            if node["kind"] == "execute"
            for pattern in (node["write_scope"] or [])
        }
    )


async def _default_broadcast(session_id: int, event: dict[str, Any]) -> None:
    from app.orchestration.agent_events import broadcast

    await broadcast(session_id, event)


class WorkflowRuntime:
    def __init__(
        self,
        executor: NodeExecutor,
        *,
        limits: PoolLimits | None = None,
        leases: WriteLeaseTable | None = None,
        command_runner: CommandRunner | None = None,
        command_gate: CommandGateFn | None = None,
        retry_plan: Callable[[], list[float]] | None = None,
        broadcast: Broadcast | None = None,
        notify: Notify | None = None,
        probe: ModelProbe | None = None,
    ) -> None:
        self._executor = executor
        self._probe: ModelProbe = probe or probe_session_model
        self._pool = WorkflowPool(limits or PoolLimits())
        self._leases = leases if leases is not None else get_lease_table()
        self._command_runner: CommandRunner = command_runner or run_check_command
        self._command_gate: CommandGateFn = command_gate or gate_check_command
        self._retry_plan = retry_plan or _default_retry_plan
        self._broadcast: Broadcast = broadcast or _default_broadcast
        self._notify: Notify = notify or _no_notify
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._timers: dict[int, asyncio.Task[None]] = {}
        # 节点准入元数据：程序 / 会话 / 子工作流 / 工作区 / 写入范围（内存缓存，可由数据库重建）
        self._meta: dict[int, dict[str, Any]] = {}
        self._owners: dict[int, LeaseOwner] = {}
        self._lease_block: dict[int, str] = {}
        self._reason_cache: dict[int, str] = {}
        self._retries: dict[int, int] = {}
        self._repaired: set[int] = set()
        self._started = 0  # 已启动的节点尝试次数（驱动 run_until_idle 判断是否还需调度）
        # 网络中断后已自动续跑过一次的程序（手动续跑后清除，保证自动续跑只尝试一次）
        self._auto_resume_tried: set[int] = set()

    @property
    def pool(self) -> WorkflowPool:
        return self._pool

    @property
    def busy(self) -> bool:
        """是否有节点正在执行（回收数据库空间前判断，避免长事务阻塞进行中的节点写入）。"""
        return bool(self._tasks)

    def pool_snapshot(self) -> dict[str, object]:
        """并发池占用快照（界面展示用）：全局 / 会话占用、排队数与上限。"""
        return self._pool.snapshot()

    def _in_flight(self, program_id: int) -> bool:
        return any(
            meta["program"] == program_id and node_id in self._tasks
            for node_id, meta in self._meta.items()
        )

    async def _notify_program(
        self, program_id: int, build: Callable[[dict[str, Any]], str]
    ) -> None:
        """把程序的关键状态变化交给主代理（经 notify 回调）；通知失败不影响运行时。"""
        snap = await store.load_program(program_id)
        if snap is None:
            return
        try:
            await self._notify(snap["run"]["session_id"], build(snap))
        except Exception:
            logger.debug("[workflow] 主代理通知失败（非阻塞）", exc_info=True)

    async def pause_session_turn(self, session_id: int) -> int:
        """用户停止当前轮：该会话内运行中的程序暂停（pause_kind=turn_stop），进度保留。返回暂停的程序数。"""
        paused = 0
        for program_id in await store.running_program_ids(session_id, statuses=("running",)):
            await self._pause_program(program_id, "turn_stop", "已停止当前轮，已保留进度，可继续")
            paused += 1
        return paused

    async def pause_session_programs(self, session_id: int, *, kind: str, reason: str) -> int:
        """会话归档：该会话内运行中的程序统一暂停，进度保留，恢复会话后可继续（设计 §5.3）。"""
        paused = 0
        for program_id in await store.running_program_ids(session_id, statuses=("running",)):
            await self._pause_program(program_id, kind, reason)
            paused += 1
        return paused

    async def cancel_session_programs(self, session_id: int) -> int:
        """会话被永久删除：其中未结束的程序取消（设计 §5.3）。"""
        cancelled = 0
        for program_id in await store.running_program_ids(
            session_id, statuses=("draft", "running", "waiting_human", "paused")
        ):
            await self.cancel(program_id, reason="session_deleted")
            cancelled += 1
        return cancelled

    # ── 公共入口 ─────────────────────────────────────────

    async def create_program(
        self,
        *,
        session_id: int,
        plan: dict[str, Any],
        workspace: str,
        origin_turn_id: int | None = None,
    ) -> int:
        """校验并落库一个程序（draft）。校验失败抛出 ValueError，错误信息即模型可读的诊断。"""
        spec, diags = parse_plan(plan)
        if spec is None:
            raise ValueError("；".join(d.message for d in diags))
        result = validate_plan(spec)
        if not result.ok or result.normalized is None:
            raise ValueError("；".join(d.message for d in result.errors))
        return await store.create_program(
            session_id=session_id,
            plan=result.normalized.model_dump(),
            workspace=workspace,
            origin_turn_id=origin_turn_id,
        )

    async def start(
        self,
        program_id: int,
        *,
        accept_local_changes: bool = False,
        shared_workspace: bool = False,
        turn_id: int | None = None,
    ) -> None:
        snap = await store.load_program(program_id)
        if snap is None:
            raise ValueError("工作流不存在")
        if snap["run"]["status"] != "draft":
            raise ValueError("只有待确认的工作流可以开始执行")
        if shared_workspace:
            # 用户选择共享工作区：子工作流直接在主工作区执行，能看到未提交的改动
            for flow in snap["flows"]:
                await self._mark_flow_shared(program_id, flow, "你选择了共享工作区")
            snap = await store.load_program(program_id)
        elif not accept_local_changes:
            files = await self._local_changes_before_worktrees(snap)
            if files:
                raise WorkflowLocalChangesError(files)
        await self._ensure_worktrees(program_id, snap)
        for flow in snap["flows"]:
            await store.update_run(flow["run"]["id"], status="running")
        # 节点子代理归属本次启动所在的轮次（节点轮次取 last_turn_id，见 store.load_node_context）
        await store.update_run(
            program_id,
            status="running",
            started_at=store.now_iso(),
            **({"last_turn_id": turn_id} if turn_id is not None else {}),
        )
        await self._log(program_id, "工作流开始执行")
        await self._recalc(program_id)
        self._wake_tick()

    async def pause(self, program_id: int, reason: str = "用户已暂停") -> None:
        await self._pause_program(program_id, "user", reason)

    async def recover_after_restart(self) -> int:
        """启动恢复扫描（§6.8）：进程重启后不存在在途任务，运行中的节点全部转为可续跑的中断，程序暂停等待用户。"""
        recovered = 0
        for program_id in await store.running_program_ids():
            snap = await store.load_program(program_id)
            if snap is None:
                continue
            for flow in snap["flows"]:
                for node in flow["nodes"]:
                    if node["status"] in ("running", "recovering", "queued"):
                        await store.update_node(
                            node["id"],
                            status="interrupted",
                            next_retry_at=None,
                            reason=None,
                            finished_at=store.now_iso(),
                            error_json={"message": "应用退出或重启，进度已保留"},
                            pending_json={"mode": "checkpoint", "feedback": ""},
                        )
            await self._pause_program(program_id, "restart", "应用退出或重启，已保留进度，可继续")
            recovered += 1
        return recovered

    async def resume(
        self,
        program_id: int,
        note: str = "",
        *,
        accept_drift: bool = False,
        automatic: bool = False,
        turn_id: int | None = None,
    ) -> None:
        """从断点续跑：中断的节点改为 checkpoint 模式重新入队，已完成节点不重跑。

        暂停期间写入范围内的文件被外部改动时，未经 accept_drift 确认不得续跑（§6.6）。
        automatic 表示网络恢复后的自动续跑：不重新开放自动续跑次数，手动续跑则重新开放。
        turn_id 为发起续跑的轮次（工作流工具调用）；无轮次的入口不改变节点的归属轮次。
        """
        snap = await store.load_program(program_id)
        if snap is None:
            raise ValueError("工作流不存在")
        if snap["run"]["status"] != "paused":
            raise ValueError("只有已暂停的工作流可以继续")
        if not automatic:
            self._auto_resume_tried.discard(program_id)
        if _token_budget_exhausted(snap):
            used = int(snap["run"]["token_used"] or 0)
            budget = int(snap["run"]["token_budget"] or 0)
            raise ValueError(
                f"token 预算仍已用完（已用 {used:,} / 预算 {budget:,}），"
                "请先在设置 → 工作流中调高预算后再继续"
            )
        ok, why = await self._probe(snap["run"]["session_id"])
        if not ok:
            if automatic:
                logger.info("[workflow] 自动续跑未执行：模型暂不可用（%s）", why)
                return
            raise WorkflowModelUnavailableError(why)
        if not accept_drift:
            drift = await self.drift_of(program_id)
            if drift:
                raise WorkflowDriftError(drift)
        await self._ensure_worktrees(program_id, snap)
        for flow in snap["flows"]:
            for node in flow["nodes"]:
                if node["status"] in ("interrupted", "recovering"):
                    await store.update_node(
                        node["id"],
                        status="pending",
                        next_retry_at=None,
                        reason=None,
                        pending_json={"mode": "checkpoint", "feedback": note or ""},
                    )
            if flow["run"]["status"] == "paused":
                await store.update_run(flow["run"]["id"], status="running")
        await store.update_run(
            program_id,
            status="running",
            pause_kind=None,
            pause_reason=None,
            paused_at=None,
            resume_count=(snap["run"]["resume_count"] or 0) + 1,
            fingerprint_json=None,
            **({"last_turn_id": turn_id} if turn_id is not None else {}),
        )
        suffix = f"：{note}" if note else ""
        confirmed = "；已确认暂停期间的工作区改动" if accept_drift else ""
        await self._log(program_id, f"从断点继续，保留已完成的进度{confirmed}{suffix}")
        await self._emit_run(program_id)
        self._wake_tick()

    async def add_node_note(self, program_id: int, flow_key: str, node_key: str, text: str) -> None:
        """运行中的步骤补充说明（设计 §7.6）：经子代理的指令通道下一步生效；未在运行则拒绝。"""
        snap = await store.load_program(program_id)
        if snap is None:
            raise ValueError("工作流不存在")
        flow = _find_flow(snap, flow_key)
        node = _find_node(flow, node_key) if flow else None
        if node is None:
            raise ValueError("节点不存在")
        if node["status"] not in ("running", "recovering") or not node.get("agent_id"):
            raise ValueError("这个步骤现在不在运行，补充说明无法送达")
        from app.orchestration.subagent import get_subagent_manager

        delivered = get_subagent_manager(snap["run"]["session_id"]).push_agent_input(
            int(node["agent_id"]), text.strip()
        )
        if not delivered:
            raise ValueError("这个步骤现在不在运行，补充说明无法送达")
        await self._log(program_id, f"已向『{flow_key}/{node_key}』补充说明，下一步生效")

    async def retry_node(self, program_id: int, flow_key: str, node_key: str, mode: str) -> None:
        """重试失败的节点（设计 §6.4）：checkpoint 从断点继续，fresh 从头重做该步骤。

        只接受已失败的节点，其它已完成的步骤保持不动。
        checkpoint 没有可用检查点时，由执行器回落为新建子代理。
        """
        if mode not in ("checkpoint", "fresh"):
            raise ValueError("重试方式只能是从断点继续或从头重做")
        snap = await store.load_program(program_id)
        if snap is None:
            raise ValueError("工作流不存在")
        if snap["run"]["status"] != "failed":
            raise ValueError("只有失败的工作流可以重试节点")
        flow = _find_flow(snap, flow_key)
        node = _find_node(flow, node_key) if flow else None
        if flow is None or node is None:
            raise ValueError("节点不存在")
        if node["status"] != "failed":
            raise ValueError("只能重试已失败的节点")
        if _token_budget_exhausted(snap):
            used = int(snap["run"]["token_used"] or 0)
            budget = int(snap["run"]["token_budget"] or 0)
            raise ValueError(
                f"token 预算仍已用完（已用 {used:,} / 预算 {budget:,}），"
                "请先在设置 → 工作流中调高预算后再重试"
            )
        ok, why = await self._probe(snap["run"]["session_id"])
        if not ok:
            raise WorkflowModelUnavailableError(why)
        await self._ensure_worktrees(program_id, snap)
        await store.update_node(
            node["id"],
            status="pending",
            reason=None,
            error_json=None,
            finished_at=None,
            next_retry_at=None,
            pending_json={"mode": mode, "feedback": ""},
        )
        await store.update_run(flow["run"]["id"], status="running", finished_at=None)
        await store.update_run(program_id, status="running", finished_at=None)
        label = "从断点继续" if mode == "checkpoint" else "从头重做"
        await self._log(
            program_id, f"节点『{flow_key}/{node_key}』{label}，其它已完成的步骤保持不变"
        )
        await self._emit_node(node["id"], status="pending")
        await self._emit_run(program_id)
        self._wake_tick()

    async def replace_draft_flow(
        self,
        program_id: int,
        flow_key: str,
        source_nodes: list[dict[str, Any]],
        source_label: str,
        *,
        validate: Callable[
            [dict[str, Any]], Awaitable[tuple[dict[str, Any] | None, list[dict[str, Any]]]]
        ],
    ) -> None:
        """草稿计划中替换某个子工作流的模板（设计 §7.7）：保留子工作流的标识、标题与依赖，只换步骤。

        仅草稿可用；改写后经与 plan_workflow 相同的校验，未通过则不落库。
        """
        snap = await store.load_program(program_id)
        if snap is None:
            raise ValueError("工作流不存在")
        if snap["run"]["status"] != "draft":
            raise ValueError("只有待确认的计划可以替换子工作流的模板")
        plan = copy.deepcopy(snap["run"]["graph"] or {})
        target = next((f for f in plan.get("flows", []) if f.get("key") == flow_key), None)
        if target is None:
            raise ValueError("子工作流不存在")
        if flow_key == INTEGRATION_FLOW_KEY:
            raise ValueError("集成验收由程序级验收生成，不能替换模板")
        target["nodes"] = copy.deepcopy(source_nodes)
        new_plan, errors = await validate(plan)
        if new_plan is None:
            raise WorkflowPlanInvalidError(errors)
        await store.apply_draft_plan(program_id, new_plan)
        await self._log(
            program_id, f"子工作流『{target.get('title') or flow_key}』改用模板『{source_label}』"
        )
        await self._emit_run(program_id)

    async def cancel(self, program_id: int, reason: str = "user") -> None:
        """取消运行。reason 为 adjusted 时表示已由新的计划替代（草稿的“调整”）。"""
        snap = await store.load_program(program_id)
        if snap is None:
            return
        # 先落定程序状态：在途节点的结算据此识别“已取消”并跳过改写，避免被当作暂停
        await store.update_run(
            program_id, status="cancelled", finished_at=store.now_iso(), closed_reason=reason
        )
        self._stop_program_tasks(program_id)
        self._pool.drop_waiting(lambda s: s.flow_key.startswith(f"{program_id}:"))
        for flow in snap["flows"]:
            for node in flow["nodes"]:
                if node["status"] not in ("done", "skipped", "failed", "cancelled"):
                    await store.update_node(
                        node["id"], status="cancelled", reason=None, finished_at=store.now_iso()
                    )
            await store.update_run(
                flow["run"]["id"], status="cancelled", finished_at=store.now_iso()
            )
        await store.update_run(
            program_id, status="cancelled", finished_at=store.now_iso(), closed_reason=reason
        )
        await self._log(program_id, "工作流已取消")
        await self._emit_run(program_id)

    async def amend(self, program_id: int, plan: dict[str, Any], *, reason: str = "") -> int:
        """运行中修订尚未开始的部分（设计 §4.5 第 5 点）。

        经同一套校验，已开始的节点保持原样，版本 +1。
        """
        snap = await store.load_program(program_id)
        if snap is None:
            raise ValueError("工作流不存在")
        if snap["run"]["status"] in _TERMINAL_FLOW:
            raise ValueError("已结束的工作流不能修订")
        rev = int(snap["run"]["plan_rev"] or 1)
        if rev >= 1 + MAX_PLAN_REVISIONS:
            raise ValueError(
                f"修订次数已达上限（{MAX_PLAN_REVISIONS} 次），请基于当前计划新建工作流"
            )
        spec, diags = parse_plan(plan)
        if spec is None:
            raise ValueError("；".join(d.message for d in diags))
        result = validate_plan(spec)
        if not result.ok or result.normalized is None:
            raise ValueError("；".join(d.message for d in result.errors))
        new_plan = result.normalized.model_dump()
        violation = _frozen_violation(snap, new_plan)
        if violation:
            raise ValueError(violation)
        await store.apply_plan_revision(program_id, new_plan, rev + 1, reason)
        await self._log(
            program_id, f"计划已修订（第 {rev + 1} 版）：{reason or '调整尚未开始的步骤'}"
        )
        await self._emit_run(program_id)
        await self._recalc(program_id)
        self._wake_tick()
        return rev + 1

    async def decide_gate(
        self, program_id: int, flow_key: str, node_key: str, decision: str, feedback: str = ""
    ) -> None:
        """人工闸门决定：approve 采纳当前结果；rework 再返工一轮；abort 中止该子工作流。"""
        snap = await store.load_program(program_id)
        if snap is None:
            raise ValueError("工作流不存在")
        flow = _find_flow(snap, flow_key)
        node = _find_node(flow, node_key) if flow else None
        if flow is None or node is None:
            raise ValueError("节点不存在")
        if node["status"] != "waiting_human":
            raise ValueError("该节点不在等待决定状态")
        if (node.get("conflict") or {}).get("kind") == "gate":
            await self._decide_gate_question(program_id, node, decision)
            return
        if (node.get("conflict") or {}).get("kind") == "review":
            await self._decide_review_gate(program_id, flow, node, decision, feedback)
        elif (node.get("conflict") or {}).get("kind") == "integration":
            await self._decide_integration(program_id, flow_key, node_key, node, decision, flow)
        elif (node.get("conflict") or {}).get("kind") == "scope_violation":
            await self._decide_scope_violation(
                program_id, flow_key, node_key, node, decision, feedback
            )
        elif decision == "rework" and node.get("conflict"):
            # 冲突闸门的“再试一次”：同一节点基于最新内容重做，用户说明作为反馈交给模型
            await store.update_node(
                node["id"],
                status="pending",
                finished_at=None,
                reason=None,
                conflict_json=None,
                pending_json={
                    "mode": "rework",
                    "feedback": (
                        f"写入冲突已由用户处理，请基于最新文件内容重做修改。{feedback}".strip()
                    ),
                },
            )
            await self._emit_node(node["id"], status="pending")
            await self._log(program_id, f"冲突闸门：再试一次 {flow_key}/{node_key}")
        elif decision == "approve":
            await store.update_node(
                node["id"],
                status="done",
                finished_at=store.now_iso(),
                reason=None,
                conflict_json=None,
            )
            await self._emit_node(node["id"], status="done", decision="approve")
            await self._log(program_id, f"已采纳当前结果：{flow_key}/{node_key}（风险已记录）")
        elif decision == "rework":
            if not node.get("rework_to"):
                raise ValueError("该评估没有可返工的实现步骤，请选择采纳或中止")
            max_rework = (
                node["max_rework"] if node["max_rework"] is not None else DEFAULT_MAX_REWORK
            ) + 1
            await store.update_node(node["id"], max_rework=max_rework)
            ctx = await store.load_node_context(node["id"])
            if ctx is None:
                raise ValueError("节点上下文缺失")
            await self._schedule_rework(
                ctx, node, node.get("verdict"), reason="用户要求再返工一轮", extra=feedback
            )
        elif decision == "abort":
            await self._cancel_flow(program_id, flow)
        else:
            raise ValueError(f"未知的闸门决定：{decision}")
        if (node.get("conflict") or {}).get("kind") in ("conflict", "integration"):
            await conflict_events.emit(
                program_id,
                conflict_events.RESOLVED,
                node_key,
                {"level": "L4", "decision": decision},
                broadcast=self._broadcast,
            )
        await self._recalc(program_id)
        self._wake_tick()

    async def _integrate(self, request: NodeRequest) -> NodeResult:
        """并入主工作区（独立工作树子工作流的最后一步，确定性执行，不调用模型）。

        冲突以 conflicts 交给合并步骤解决（冲突留在工作树中）；其它失败以 error 交给用户决定。
        """
        snap = await store.load_program(request.run_id)
        main = str(((snap or {}).get("run") or {}).get("workspace") or "")
        if not main or not request.workspace:
            return NodeResult(status="failed", error="缺少主工作区或子工作流的工作区")
        if request.workspace == main:
            # 子工作流并未使用独立工作树（共享执行）：没有需要并入的改动
            return NodeResult(
                status="done", output={"integrated": False, "summary": "共享工作区，无需并入"}
            )
        branch = isolation.branch_name(request.run_id, request.flow_key)
        outcome = await isolation.integrate(
            main,
            request.workspace,
            branch=branch,
            message=f"工作流：{request.goal[:40] or request.flow_key}（{request.flow_key}）",
        )
        if outcome.status == "conflict":
            return NodeResult(
                status="done",
                output={"conflicts": list(outcome.files), "summary": "并入主工作区时有冲突"},
            )
        if outcome.status == "merged":
            await isolation.remove(
                main, worktree=request.workspace, branch=branch, keep_branch=False
            )
            return NodeResult(
                status="done",
                output={
                    "integrated": True,
                    "summary": outcome.message or f"已并入主工作区（{branch}）",
                },
            )
        return NodeResult(status="failed", error=outcome.message or "并入主工作区失败")

    async def _execute_node(self, request: NodeRequest) -> NodeResult:
        """执行节点。独立工作树子工作流的节点开始前，先把主工作区的新改动同步进工作树（§5A）：
        同步不了（与子工作流已有的修改重叠）时节点失败并说明原因，用户处理后重试该节点。"""
        snap = await store.load_program(request.run_id)
        main = str(((snap or {}).get("run") or {}).get("workspace") or "")
        # 合并步骤处理的是并入时的 git 合并冲突（主分支已在工作树的合并里），不能再做差异式的同步
        if main and request.workspace and request.workspace != main and request.kind != "merge":
            synced = await isolation.sync_main_changes(
                main,
                request.workspace,
                branch=isolation.branch_name(request.run_id, request.flow_key),
            )
            if synced.status == "blocked":
                return NodeResult(status="failed", error=synced.message or "同步主工作区的改动失败")
            if synced.files:
                await self._log(
                    request.run_id,
                    "已把主工作区的新改动同步进子工作流：" + "、".join(synced.files[:5]),
                )
        if request.kind == "check":
            return await self._run_check_node(request)
        if request.kind == "gate":
            # 闸门不调用模型：结算时转为“等待你的决定”
            return NodeResult(status="gate", error=request.question or "需要你确认是否继续")
        return await self._executor.run(request)

    async def _run_command(
        self, command: str, cwd: str, *, session_id: int | None, origin: str
    ) -> dict[str, Any]:
        """运行一条检查命令：先过权限门控（plan-20261014-fix4：跟随会话权限模式），再执行。

        门控不放行时不执行，结果带 blocked 与原因，与命令失败区分开。
        """
        gate = await self._command_gate(
            command, session_id=session_id, workspace=cwd, origin=origin
        )
        if not gate.allowed:
            return {
                "cmd": command,
                "ok": False,
                "exit_code": None,
                "blocked": True,
                "output": gate.reason,
            }
        return await self._command_runner(command, cwd)

    async def _run_check_node(self, request: NodeRequest) -> NodeResult:
        """检查节点（设计 §4.5）：不调用模型，按顺序运行命令；全部通过则完成，否则失败。"""
        results: list[dict[str, Any]] = []
        origin = f"工作流步骤『{request.flow_key}/{request.node_key}』"
        for command in request.commands:
            outcome = await self._run_command(
                command, request.workspace, session_id=request.session_id, origin=origin
            )
            results.append(
                {
                    "command": command,
                    "ok": bool(outcome.get("ok")),
                    "output": str(outcome.get("output") or "")[-600:],
                    **({"blocked": True} if outcome.get("blocked") else {}),
                }
            )
            if not outcome.get("ok"):
                break
        failed = [item for item in results if not item["ok"]]
        if failed:
            first = failed[0]
            error = (
                f"检查未执行：{first['command']}（{first['output']}）"
                if first.get("blocked")
                else f"检查未通过：{first['command']}"
            )
            return NodeResult(status="failed", error=error, output={"checks": results})
        return NodeResult(
            status="done",
            text=f"检查通过：{len(results)} 条命令",
            output={"checks": results},
        )

    async def _gate_question(self, node_id: int, meta: dict[str, Any], question: str) -> None:
        """闸门节点（设计 §4.5）：等待你的决定，继续或中止。"""
        payload = {"kind": "gate", "message": question}
        await store.update_node(
            node_id,
            status="waiting_human",
            finished_at=None,
            reason=question,
            conflict_json=payload,
            pending_json=None,
        )
        await self._emit_node(node_id, status="waiting_human")
        await self._emit(
            meta["program"],
            "workflow.gate.opened",
            meta["key"],
            {
                "flow_key": meta["flow_key"],
                "node_id": node_id,
                "reason": question,
                "options": ["approve", "abort"],
            },
        )
        await self._log(
            meta["program"],
            f"需要你的决定：{meta['flow_key']}/{meta['key']}（{question}）",
            level="warn",
        )

    async def _gate_integration(
        self, node_id: int, meta: dict[str, Any], reason: str, *, files: Any = ()
    ) -> None:
        """并入主工作区需要用户决定：再试一次（例如先处理了主工作区的本地改动）或中止该子工作流。"""
        payload = {"kind": "integration", "message": reason, "files": [str(f) for f in files]}
        await store.update_node(
            node_id,
            status="waiting_human",
            finished_at=None,
            reason=reason,
            conflict_json=payload,
            pending_json=None,
        )
        await self._emit_node(node_id, status="waiting_human")
        await self._emit(
            meta["program"],
            "workflow.gate.opened",
            meta["key"],
            {
                "flow_key": meta["flow_key"],
                "reason": reason,
                "options": ["rework", "abort"],
                "issues": [],
                "conflict": True,
            },
        )
        await self._log(
            meta["program"],
            f"并入主工作区需要你的决定：{meta['flow_key']}（{reason}）",
            level="warn",
        )
        await conflict_events.emit(
            meta["program"],
            conflict_events.DETECTED,
            meta["key"],
            {"level": "L4", "paths": [str(f) for f in files], "reason": reason},
            broadcast=self._broadcast,
        )

    async def _decide_gate_question(
        self, program_id: int, node: dict[str, Any], decision: str
    ) -> None:
        """闸门（设计 §4.5）：继续则步骤完成，后续步骤可以开始；中止则步骤失败、子工作流停止。"""
        if decision not in ("approve", "abort"):
            raise ValueError("这一步只能选择继续或中止")
        if decision == "approve":
            await store.update_node(
                node["id"],
                status="done",
                finished_at=store.now_iso(),
                reason=None,
                conflict_json=None,
                output_json={"decision": "approve"},
            )
            await self._log(program_id, f"闸门『{node['key']}』已确认继续")
        else:
            await store.update_node(
                node["id"],
                status="failed",
                finished_at=store.now_iso(),
                reason="你中止了这一步",
                conflict_json=None,
                error_json={"message": "用户中止"},
            )
            await self._log(program_id, f"闸门『{node['key']}』已中止", level="warn")
        await self._emit_run(program_id)
        await self._recalc(program_id)
        self._wake_tick()

    async def _decide_review_gate(
        self,
        program_id: int,
        flow: dict[str, Any],
        node: dict[str, Any],
        decision: str,
        feedback: str,
    ) -> None:
        """评估待确认（plan-workflow-collab）：用户（或主代理转达用户的决定）确认后才返工。

        approve 采纳当前结果（问题记入汇报）；rework 带着评估问题与用户补充返工；
        abort 中止子工作流。
        """
        gate = node.get("conflict") or {}
        note = feedback.strip()
        if decision == "approve":
            await self._set_round_outcome(node["id"], "accepted", note)
            await store.update_node(
                node["id"],
                status="done",
                finished_at=store.now_iso(),
                reason=None,
                conflict_json=None,
            )
            await self._emit_node(node["id"], status="done", decision="approve")
            await self._log(
                program_id, f"已采纳当前结果：{flow['run']['package_key']}/{node['key']}"
            )
        elif decision == "rework":
            if not node.get("rework_to"):
                raise ValueError("这一步没有可返工的实现步骤，请选择采纳或中止")
            if gate.get("stage") in ("exhausted", "escalate"):
                # 返工次数已用尽（或评估者请求人工决定）时由用户追加一轮
                current = (
                    node["max_rework"] if node["max_rework"] is not None else DEFAULT_MAX_REWORK
                )
                await store.update_node(
                    node["id"], max_rework=max(current, int(node["rework_round"] or 0)) + 1
                )
            await self._set_round_outcome(node["id"], "rework", note)
            ctx = await store.load_node_context(node["id"])
            if ctx is None:
                raise ValueError("节点上下文缺失")
            await self._schedule_rework(
                ctx, ctx["node"], node.get("verdict"), reason="已确认返工", extra=note
            )
        elif decision == "abort":
            await self._set_round_outcome(node["id"], "aborted", note)
            await self._cancel_flow(program_id, flow)
        else:
            raise ValueError("评估结论只能选择采纳、返工或中止")

    async def _set_round_outcome(self, node_id: int, outcome: str, note: str = "") -> None:
        """记下最近一轮评估的处理结果（过程报告据此说明“返工 / 采纳 / 中止”及用户说明）。"""
        ctx = await store.load_node_context(node_id)
        if ctx is None:
            return
        output = dict(ctx["node"].get("output") or {})
        rounds = [dict(r) for r in output.get("rounds") or []]
        if rounds:
            rounds[-1]["outcome"] = outcome
            if note:
                rounds[-1]["user_note"] = note[:600]
        output["rounds"] = rounds
        if note:
            output["user_note"] = note[:600]
        await store.update_node(node_id, output_json=output)

    async def _decide_integration(
        self,
        program_id: int,
        flow_key: str,
        node_key: str,
        node: dict[str, Any],
        decision: str,
        flow: dict[str, Any],
    ) -> None:
        if decision == "rework":
            await store.update_node(
                node["id"],
                status="pending",
                finished_at=None,
                reason=None,
                conflict_json=None,
                pending_json={"mode": "rework", "feedback": "用户要求再次并入主工作区"},
            )
            await self._emit_node(node["id"], status="pending")
            await self._log(program_id, f"再次并入主工作区：{flow_key}")
        elif decision == "abort":
            await self._cancel_flow(program_id, flow)
        else:
            raise ValueError("并入主工作区的决定只能是再试一次或中止")

    async def _local_changes_before_worktrees(self, snap: dict[str, Any]) -> list[str]:
        """还需要新建工作树时，主工作区未提交的改动（已有工作树的运行不再提示）。"""
        main = str(snap["run"].get("workspace") or "")
        needs = any(
            flow["run"].get("isolation") == "worktree"
            and not (flow["run"].get("workspace") and os.path.isdir(str(flow["run"]["workspace"])))
            for flow in snap["flows"]
        )
        if not needs or not main:
            return []
        return await isolation.local_changes(main)

    async def _mark_flow_shared(
        self, program_id: int, flow: dict[str, Any], reason: str, *, level: str = "info"
    ) -> None:
        """子工作流改为共享工作区执行：改动直接落在主工作区，并入步骤不再需要。"""
        run = flow["run"]
        if run.get("isolation") != "worktree":
            return
        await store.update_run(run["id"], isolation="shared", workspace="")
        for node in flow["nodes"]:
            if node["kind"] == "integrate" and node["status"] == "pending":
                await store.update_node(
                    node["id"], status="skipped", reason="共享工作区执行，无需并入"
                )
        await self._log(
            program_id,
            f"子工作流『{run.get('package_key')}』改为共享工作区执行（{reason}）",
            level=level,
        )

    async def _ensure_worktrees(self, program_id: int, snap: dict[str, Any]) -> None:
        """独立工作树子工作流：开始 / 续跑前确保工作树存在。

        不是可用的 git 仓库时改为共享工作区执行。
        """
        main = str(snap["run"].get("workspace") or "")
        for flow in snap["flows"]:
            run = flow["run"]
            if run.get("isolation") != "worktree":
                continue
            ws = str(run.get("workspace") or "")
            if ws and ws != main and os.path.isdir(ws):
                continue  # 已准备好的独立工作树（进程重启后复用）
            try:
                wt = await isolation.prepare(
                    main, run_id=program_id, flow_key=str(run.get("package_key") or "")
                )
            except isolation.WorktreeUnavailable as exc:
                await self._mark_flow_shared(program_id, flow, exc.reason, level="warn")
                continue
            await store.update_run(run["id"], workspace=wt.path)
            origin = "以主工作区当前的未提交改动为起点" if wt.baseline else "基于最近一次提交"
            flow_name = run.get("package_key")
            await self._log(
                program_id,
                f"子工作流『{flow_name}』在独立工作树中执行（分支 {wt.branch}，{origin}）",
            )

    async def _decide_scope_violation(
        self,
        program_id: int,
        flow_key: str,
        node_key: str,
        node: dict[str, Any],
        decision: str,
        feedback: str,
    ) -> None:
        """越界改动的闸门：include 把文件纳入本步骤范围（并加写入租约）后让节点核对并继续。

        abort 中止子工作流。
        """
        gate = node.get("conflict") or {}
        if decision == "include":
            paths = [p for p in gate.get("paths") or [] if isinstance(p, str) and p]
            ctx = await store.load_node_context(node["id"])
            if not paths or ctx is None:
                raise ValueError("没有可纳入范围的文件")
            owner = LeaseOwner(
                run_id=int(ctx["flow_run_id"]),
                node_key=node_key,
                session_id=int(ctx["session_id"]),
            )
            granted, holders = self._leases.acquire(ctx["workspace"], owner, tuple(paths))
            if not granted:
                holder = holders[0].holder.node_key if holders else "其它步骤"
                raise ValueError(f"这些文件正被『{holder}』占用，请稍后再决定")
            await store.add_node_scope(node["id"], paths)
            note = (
                f"以下文件已被你的命令修改，现已纳入本步骤写入范围：{'、'.join(paths)}。"
                "请核对这些改动是否符合任务要求，必要时修正后再继续。"
            )
            await store.update_node(
                node["id"],
                status="pending",
                finished_at=None,
                reason=None,
                conflict_json=None,
                pending_json={"mode": "rework", "feedback": f"{note}{feedback}".strip()},
            )
            await self._emit_node(node["id"], status="pending")
            await self._log(program_id, f"越界改动已纳入范围：{flow_key}/{node_key}")
        elif decision == "abort":
            flow = _find_flow(await store.load_program(program_id) or {}, flow_key)
            if flow is None:
                raise ValueError("子工作流不存在")
            await self._cancel_flow(program_id, flow)
        else:
            raise ValueError("越界改动只能选择纳入范围或中止")

    async def tick(self) -> None:
        """一次调度：为运行中（含等待决定但其它子工作流仍在跑）的程序挑选可启动节点并启动。"""
        for program_id in await store.running_program_ids():
            snap = await store.load_program(program_id)
            if snap is None:
                continue
            if _token_budget_exhausted(snap):
                # 预算用完：不再启动新节点；在途节点结束后整体暂停，已花费的 token 不被浪费
                if not self._in_flight(program_id):
                    await self._pause_program(program_id, "budget", _budget_reason(snap))
                    await self._notify_program(program_id, _budget_notice)
                continue
            flow_status = {f["run"]["package_key"]: f["run"]["status"] for f in snap["flows"]}
            for flow in snap["flows"]:
                run = flow["run"]
                if run["status"] not in ("running", "waiting_human"):
                    continue
                flow_ready = all(flow_status.get(dep) == "succeeded" for dep in run["depends_on"])
                views = [NodeView(n["key"], n["status"], tuple(n["inputs"])) for n in flow["nodes"]]
                by_key = {n["key"]: n for n in flow["nodes"]}
                for key in ready_node_keys(views, flow_ready=flow_ready):
                    await self._enqueue(snap["run"], run, by_key[key])
        for slot in self._pool.pop_admissible(accept=self._accept):
            await self._start(int(slot.node_id))
        await self._refresh_reasons()

    async def run_until_idle(self, timeout: float = 60.0) -> None:
        """驱动直到没有在跑的节点与待触发的重试（测试与后台循环共用）。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            started_before = self._started
            await self.tick()
            waiting = [t for t in (*self._tasks.values(), *self._timers.values()) if not t.done()]
            if waiting:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError("工作流运行超时")
                await asyncio.wait(waiting, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
                continue
            # 本轮虽已无在跑任务，但若刚启动过节点（可能已在 tick 内完成），仍需再调度一轮
            if self._started != started_before:
                continue
            return

    # ── 调度与准入 ───────────────────────────────────────

    def _wake_tick(self) -> None:
        """后台循环模式下唤醒调度；测试模式由 run_until_idle 驱动，此处无副作用。"""

    async def _enqueue(
        self, program: dict[str, Any], flow: dict[str, Any], node: dict[str, Any]
    ) -> None:
        node_id = node["id"]
        self._meta[node_id] = {
            "program": program["id"],
            "session": program["session_id"],
            "flow_run": flow["id"],
            "flow_key": flow["package_key"] or "",
            "key": node["key"],
            "kind": node["kind"],
            "scope": tuple(node["write_scope"]),
            "workspace": flow.get("workspace") or program["workspace"],
        }
        self._pool.enqueue(
            Slot(
                node_id=str(node_id),
                session_id=program["session_id"],
                flow_key=f"{program['id']}:{flow['package_key']}",
            )
        )
        await store.update_node(node_id, status="queued", reason=_QUEUED_TEXT)
        self._reason_cache[node_id] = _QUEUED_TEXT
        await self._emit_node(node_id, status="queued")

    def _accept(self, slot: Slot) -> bool:
        """写入范围准入：全部或无地申请租约；冲突则保持排队并记录阻塞者。"""
        node_id = int(slot.node_id)
        meta = self._meta.get(node_id)
        if meta is None:
            return False
        scope = meta["scope"]
        if not scope:
            return True
        owner = LeaseOwner(
            run_id=meta["flow_run"], node_key=meta["key"], session_id=meta["session"]
        )
        granted, conflicts = self._leases.acquire(meta["workspace"], owner, scope)
        if not granted:
            first = conflicts[0]
            self._lease_block[node_id] = (
                f"排队中：等『{first.holder.node_key}』改完 {first.requested} 再开始，"
                "避免同时改同一处"
            )
            return False
        self._lease_block.pop(node_id, None)
        self._owners[node_id] = owner
        return True

    async def _refresh_reasons(self) -> None:
        for node_id in list(self._meta):
            if node_id in self._tasks:
                continue
            pool_reason = self._pool.block_reason(str(node_id))
            if pool_reason is None:
                continue
            if pool_reason == REASON_GRANTED:
                text = self._lease_block.get(node_id, "排队中：等其它步骤改完相关文件再开始")
            else:
                text = _POOL_REASON_TEXT.get(pool_reason, _QUEUED_TEXT)
            if self._reason_cache.get(node_id) != text:
                self._reason_cache[node_id] = text
                await store.update_node(node_id, reason=text)

    async def _start(self, node_id: int) -> None:
        ctx = await store.load_node_context(node_id)
        if ctx is None:
            self._release(node_id)
            return
        self._started += 1
        node = ctx["node"]
        pending = node.get("pending") or {}
        mode = pending.get("mode", "fresh")
        attempt_no = node["attempt_no"] + 1
        attempt_id = await store.add_attempt(node_id, attempt_no, mode)
        await store.update_node(
            node_id,
            status="running",
            attempt_no=attempt_no,
            started_at=store.now_iso(),
            reason=None,
            pending_json=None,
        )
        self._reason_cache.pop(node_id, None)
        request = NodeRequest(
            run_id=ctx["program_id"],
            session_id=ctx["session_id"],
            flow_key=ctx["flow_key"],
            node_key=node["key"],
            node_id=node_id,
            attempt_id=attempt_id,
            attempt_no=attempt_no,
            kind=node["kind"],
            role=node["role"],
            task=node["task"],
            acceptance=tuple(node["acceptance"]),
            write_scope=tuple(node["write_scope"]),
            mode=mode,
            feedback=str(pending.get("feedback") or ""),
            upstream=ctx["upstream"],
            workspace=ctx["workspace"],
            goal=ctx["goal"],
            agent_id=node["agent_id"],
            turn_id=ctx["turn_id"],
            commands=tuple(node.get("commands") or ()),
            question=str(node.get("question") or ""),
            rework_source=str(pending.get("source") or ""),
            on_agent=lambda agent_id, _nid=node_id: self._bind_agent(_nid, agent_id),
        )
        self._tasks[node_id] = asyncio.create_task(self._run(node_id, request))
        await self._emit_node(node_id, status="running", attempt_no=attempt_no, mode=mode)

    async def _bind_agent(self, node_id: int, agent_id: int) -> None:
        """新建的节点子代理立即写回节点（plan-workflow-collab）：运行中点开节点就是当前子代理。"""
        await store.update_node(node_id, agent_id=agent_id)
        await self._emit_node(node_id, status="running", agent_id=agent_id)

    def _release(self, node_id: int) -> None:
        self._pool.release(str(node_id))
        owner = self._owners.pop(node_id, None)
        meta = self._meta.get(node_id)
        if owner is not None and meta is not None:
            self._leases.release(meta["workspace"], owner)

    # ── 节点执行与结算 ───────────────────────────────────

    async def _run(self, node_id: int, request: NodeRequest) -> None:
        try:
            try:
                if request.kind == "integrate":
                    result = await self._integrate(request)
                else:
                    result = await self._execute_node(request)
            except asyncio.CancelledError:
                result = NodeResult(status="interrupted", error="已暂停")
            except Exception as exc:  # 执行器内部异常按确定性失败处理，不让调度循环崩溃
                result = NodeResult(status="failed", error=f"{type(exc).__name__}: {exc}"[:300])
            try:
                await self._settle(node_id, request, result)
            except Exception:
                # 结算失败不能静默丢失：记录日志并把节点标为失败，避免永远停留在 running
                logger.exception("[workflow] 节点结算失败 node=%s", node_id)
                await store.update_node(
                    node_id,
                    status="failed",
                    finished_at=store.now_iso(),
                    error_json={"message": "节点结算失败，详见服务端日志"},
                )
        finally:
            # 结算（含落库与重算状态）完成后才移除句柄，驱动循环据此判断是否仍有在途工作
            self._tasks.pop(node_id, None)

    async def _settle(self, node_id: int, request: NodeRequest, result: NodeResult) -> None:
        meta = self._meta.get(node_id)
        attempt_status = {"done": "done", "interrupted": "interrupted", "gate": "done"}.get(
            result.status, "failed"
        )
        snap = await store.load_program(request.run_id)
        cancelled = snap is not None and snap["run"]["status"] == "cancelled"
        if cancelled:
            attempt_status = "cancelled"
        await store.finish_attempt(
            request.attempt_id,
            status=attempt_status,
            reason=result.error,
            tool_trace=result.tool_trace,
        )
        await store.add_tokens(request.run_id, node_id, int(result.tokens or 0))
        self._release(node_id)
        # 程序已取消：取消时节点已统一落定，在途结果只记录尝试，不再改写节点状态
        if meta is None or cancelled:
            return
        program_id = meta["program"]
        if result.agent_id is not None:
            await store.update_node(node_id, agent_id=result.agent_id)
        if result.status == "gate":
            await self._gate_question(node_id, meta, result.error)
            await self._recalc(program_id)
            return
        # 约束层判定需要用户决定（写入冲突多次未能合并）：节点转入冲突闸门，不按结果收尾
        gate = pop_gate(node_id)
        if gate is not None:
            if request.kind == "merge":
                # 合并步骤自身又遇到冲突：不再递归合并，把等待它的节点交回用户决定
                await self._fail_merge(node_id, meta, "合并时又遇到写入冲突，已转交你决定")
            elif _merge_allowed(gate) and await self._merge_budget_left(node_id):
                await self._start_merge(node_id, meta, gate)
            else:
                await self._gate_on_conflict(node_id, meta, gate)
            if result.agent_id is not None:
                forget_agent(result.agent_id)
            await self._recalc(program_id)
            return
        if request.kind == "integrate" and (result.output or {}).get("conflicts"):
            files = [str(f) for f in result.output["conflicts"]]
            gate = {
                "kind": "conflict",
                "integration": True,
                "paths": files,
                "path": files[0],
                "reason": f"并入主工作区时『{'、'.join(files[:3])}』有 git 冲突",
            }
            if _merge_allowed(gate) and await self._merge_budget_left(node_id):
                await self._start_merge(node_id, meta, gate)
            else:
                await self._gate_integration(node_id, meta, gate["reason"], files=files)
            await self._recalc(program_id)
            return
        if result.status in ("done", "failed") and result.agent_id is not None:
            forget_agent(result.agent_id)  # 节点进入终态：释放读账本与读集索引
        if result.status == "done":
            self._retries.pop(node_id, None)
            await self._on_done(node_id, request, result)
        elif result.status == "interrupted":
            await store.update_node(
                node_id,
                status="interrupted",
                finished_at=store.now_iso(),
                error_json={"message": result.error or "已中断"},
                pending_json={"mode": "checkpoint", "feedback": ""},
            )
            await self._emit_node(node_id, status="interrupted")
            # 执行器报告的中断（非本运行主动暂停造成）：整体暂停，保留进度等待用户“继续”
            snap = await store.load_program(program_id)
            if snap is not None and snap["run"]["status"] in ("running", "waiting_human"):
                kind = "network" if result.transient else "error"
                await self._pause_program(
                    program_id, kind, result.error or "节点已中断，进度已保留"
                )
        elif result.transient:
            await self._on_transient(node_id, request, result)
        else:
            await self._on_failed(node_id, request, result)
        await self._recalc(program_id)

    async def _merge_budget_left(self, node_id: int) -> bool:
        """同一节点最多追加 MAX_MERGE_STEPS 个合并步骤。

        仍然冲突时交给用户，避免“合并→返工→再冲突”无止境。
        """
        ctx = await store.load_node_context(node_id)
        if ctx is None:
            return False
        prefix = f"merge-{ctx['node']['key']}-"
        return await store.count_merge_steps(ctx["flow_run_id"], prefix) < MAX_MERGE_STEPS

    async def _start_merge(self, node_id: int, meta: dict[str, Any], gate: dict[str, Any]) -> None:
        """L3 合并步骤：同文件真冲突（或并入的 git 冲突）经重做仍失败时追加一个合并节点。

        合并节点独占这些文件，三版内容交给它；原节点进入“等待合并”，合并完成后带着说明继续。
        """
        ctx = await store.load_node_context(node_id)
        if ctx is None:
            return
        node = ctx["node"]
        paths = [str(p) for p in (gate.get("paths") or [gate.get("path") or ""]) if p]
        merge_key = f"merge-{node['key']}-{int(node['attempt_no'])}"[:60]
        title = f"合并『{paths[0]}』" if len(paths) == 1 else f"合并 {len(paths)} 个文件"
        await store.create_runtime_node(
            ctx["flow_run_id"],
            node_key=merge_key,
            title=title[:40],
            kind="merge",
            task=_merge_task(paths, gate),
            acceptance=[
                "两侧各自的改动意图都保留",
                "合并结果中，两侧都未改动的部分保持不变",
            ],
            write_scope=paths,
        )
        await store.update_node(
            node_id,
            status="awaiting_merge",
            finished_at=None,
            reason=f"等待合并步骤『{merge_key}』完成",
            conflict_json=None,
            pending_json={
                "mode": "rework",
                "feedback": "",
                "awaiting_merge": merge_key,
                "path": paths[0],
                "gate": gate,
            },
        )
        await self._emit_node(node_id, status="awaiting_merge")
        # L3：并入冲突转交合并步骤
        await conflict_events.emit(
            meta["program"],
            conflict_events.DETECTED,
            meta["key"],
            {"level": "L3", "paths": paths, "merge_step": merge_key},
            broadcast=self._broadcast,
        )
        await self._log(
            meta["program"],
            f"写入冲突转交合并步骤：{meta['flow_key']}/{meta['key']}（{'、'.join(paths[:3])}）",
        )

    async def _fail_merge(self, node_id: int, meta: dict[str, Any], message: str) -> None:
        """合并步骤未能完成：自身记为已跳过（由用户的决定接管，不让子工作流判为失败），并把等待它的节点转为用户决定。"""
        await store.update_node(
            node_id,
            status="skipped",
            finished_at=store.now_iso(),
            reason=message,
            error_json={"message": message},
        )
        await self._emit_node(node_id, status="skipped")
        await self._log(
            meta["program"], f"合并步骤未完成：{meta['key']}（{message}）", level="warn"
        )
        await self._resolve_awaiting(meta["program"], meta["flow_run"], meta["key"], ok=False)

    async def _resolve_awaiting(
        self, program_id: int, flow_run_id: int, merge_key: str, *, ok: bool, detail: str = ""
    ) -> None:
        """合并步骤结束后解除等待它的节点：成功则带上合并说明继续；失败则沿用原冲突，交给用户决定。"""
        snap = await store.load_program(program_id)
        flow = next(
            (f for f in (snap or {}).get("flows", []) if f["run"]["id"] == flow_run_id), None
        )
        if flow is None:
            return
        flow_key = flow["run"]["package_key"] or ""
        for node in flow["nodes"]:
            pending = node.get("pending") or {}
            if node["status"] != "awaiting_merge" or pending.get("awaiting_merge") != merge_key:
                continue
            if ok:
                path = str(pending.get("path") or "")
                tail = f"（{detail}）" if detail else ""
                note = (
                    f"『{path}』已由合并步骤合并为一版{tail}。"
                    "请基于最新内容核对你剩余的修改，并完成本步骤。"
                )
                await store.update_node(
                    node["id"],
                    status="pending",
                    finished_at=None,
                    reason=None,
                    pending_json={"mode": "rework", "feedback": note},
                )
                await self._emit_node(node["id"], status="pending")
                await self._log(program_id, f"合并完成，继续原步骤：{flow_key}/{node['key']}")
                await conflict_events.emit(
                    program_id,
                    conflict_events.RESOLVED,
                    node["key"],
                    {"level": "L3", "paths": [path] if path else [], "result": "merged"},
                    broadcast=self._broadcast,
                )
                continue
            meta = {"program": program_id, "flow_key": flow_key, "key": node["key"]}
            gate = pending.get("gate") or {
                "kind": "conflict",
                "path": pending.get("path"),
                "reason": "合并未能完成，需要你决定",
            }
            if gate.get("integration"):
                await self._gate_integration(
                    node["id"],
                    meta,
                    str(gate.get("reason") or "并入主工作区未能完成，需要你决定"),
                    files=gate.get("paths") or [],
                )
            else:
                await self._gate_on_conflict(node["id"], meta, gate)

    async def _gate_on_conflict(
        self, node_id: int, meta: dict[str, Any], gate: dict[str, Any]
    ) -> None:
        """冲突闸门（L4）：节点等待用户决定，闸门携带基线 / 当前 / 待写入三栏数据。"""
        reason = str(gate.get("reason") or "写入冲突，需要你决定")
        await store.update_node(
            node_id,
            status="waiting_human",
            finished_at=None,
            reason=reason,
            conflict_json=gate,
            pending_json=None,
        )
        await self._emit_node(node_id, status="waiting_human")
        await self._emit(
            meta["program"],
            "workflow.gate.opened",
            meta["key"],
            {
                "flow_key": meta["flow_key"],
                "reason": reason,
                "options": _gate_options(gate),
                "issues": [],
                "conflict": True,
            },
        )
        await self._log(
            meta["program"],
            f"写入冲突需要你的决定：{meta['flow_key']}/{meta['key']}（{reason}）",
            level="warn",
        )
        # L4：重做与合并都未能化解，闸门三栏对照交给用户
        await conflict_events.emit(
            meta["program"],
            conflict_events.DETECTED,
            meta["key"],
            {"level": "L4", "path": gate.get("path"), "reason": reason},
            broadcast=self._broadcast,
        )

    async def _on_done(self, node_id: int, request: NodeRequest, result: NodeResult) -> None:
        meta = self._meta[node_id]
        if request.kind == "review":
            await self._handle_review(node_id, result)
            return
        output = dict(result.output or {"text": result.text})  # plan-156-620：产出原样落库，不截断
        if request.kind == "execute":
            # 返工记录（plan-workflow-collab）：每轮要处理什么、整改说明是什么，随过程报告交给主代理
            ctx = await store.load_node_context(node_id)
            previous = list(((ctx or {}).get("node", {}).get("output") or {}).get("reworks") or [])
            if request.rework_source == "review" and request.feedback:
                previous.append(
                    {
                        "round": len(previous) + 1,
                        "feedback": request.feedback[:800],
                        "summary": str(output.get("summary") or output.get("text") or "")[:800],
                    }
                )
            if previous:
                output["reworks"] = previous[-6:]
        await store.update_node(
            node_id,
            status="done",
            finished_at=store.now_iso(),
            error_json=None,
            output_json=output,
        )
        await self._emit_node(node_id, status="done")
        await self._log(meta["program"], f"节点『{meta['flow_key']}/{meta['key']}』已完成")
        if request.kind == "merge":
            summary = str((result.output or {}).get("summary") or result.text or "")[:80]
            await self._resolve_awaiting(
                meta["program"], meta["flow_run"], meta["key"], ok=True, detail=summary
            )

    async def _handle_review(self, node_id: int, result: NodeResult) -> None:
        ctx = await store.load_node_context(node_id)
        if ctx is None:
            return
        node = ctx["node"]
        program_id = ctx["program_id"]
        verdict = validate_verdict(result.output) if result.output else None
        if verdict is None:
            verdict = parse_verdict(result.text)
        if verdict is None and node_id not in self._repaired:
            self._repaired.add(node_id)
            await store.update_node(
                node_id,
                status="pending",
                pending_json={"mode": "repair", "feedback": REPAIR_HINT},
                reason="评估输出不合规，追问一次格式修复",
            )
            await self._emit_node(node_id, status="pending")
            await self._log(program_id, f"评估输出不合规，已追问一次格式修复：{node['key']}")
            return
        self._repaired.discard(node_id)  # 每一轮评估都可以追问一次格式修复
        origin = f"工作流评估『{ctx['flow_key']}/{node['key']}』"
        checks = [
            await self._run_command(
                cmd, ctx["workspace"], session_id=ctx.get("session_id"), origin=origin
            )
            for cmd in node["commands"]
        ]
        checks_passed = all(bool(c.get("ok")) for c in checks)
        # 检查结果始终记录；关闭“必须通过检查”后，检查失败不再阻止判定为通过
        checks_ok = checks_passed or not settings.workflow_require_checks
        max_rework = node["max_rework"] if node["max_rework"] is not None else DEFAULT_MAX_REWORK
        decision = decide_review(
            verdict,
            checks_passed=checks_ok,
            rework_round=node["rework_round"],
            max_rework=max_rework,
        )
        if not node.get("rework_to") and decision.action != "pass":
            # 没有返工目标的评估（如集成验收、汇总）无事可返：结论记入汇报，
            # 由主代理与用户决定是否整改
            # （plan-workflow-collab：不再卡在“采纳 / 中止”的闸门上）
            base = decision.reason.split("；返工次数已用尽")[0]
            decision = ReviewDecision("record", f"{base}，已记入汇报")
        output = dict(result.output or {"text": result.text})  # plan-156-620：产出原样落库，不截断
        rounds = list(((node.get("output") or {}).get("rounds")) or [])
        rounds.append(_round_entry(len(rounds) + 1, decision, verdict, checks, checks_ok))
        output["rounds"] = rounds[-8:]
        await store.update_node(
            node_id, verdict_json=verdict, checks_json=checks, output_json=output
        )
        flow_node = f"{ctx['flow_key']}/{node['key']}"
        if decision.action == "pass":
            await store.update_node(
                node_id, status="done", finished_at=store.now_iso(), reason=None
            )
            await self._emit_node(node_id, status="done", verdict="pass")
            await self._log(program_id, f"评估通过：{flow_node}")
        elif decision.action == "record":
            await store.update_node(
                node_id, status="done", finished_at=store.now_iso(), reason=decision.reason
            )
            await self._emit_node(node_id, status="done", verdict="record")
            await self._log(program_id, f"评估完成，遗留问题已记入汇报：{flow_node}", level="warn")
        elif decision.action == "rework" and not settings.workflow_rework_confirm:
            await self._schedule_rework(
                ctx, node, verdict, reason=decision.reason, extra="", checks=checks
            )
        elif decision.action == "rework":
            await self._open_review_gate(
                ctx, node, verdict, f"评估发现需要修改的问题：{decision.reason}", stage="confirm"
            )
        else:
            stage = "escalate" if verdict and verdict.get("decision") == "escalate" else "exhausted"
            await self._open_review_gate(ctx, node, verdict, decision.reason, stage=stage)

    async def _open_review_gate(
        self,
        ctx: dict[str, Any],
        node: dict[str, Any],
        verdict: dict[str, Any] | None,
        reason: str,
        *,
        stage: str = "confirm",
    ) -> None:
        """评估结论待确认：节点等待决定，结论交给主代理核实并向用户汇报（plan-workflow-collab）。

        stage：confirm 返工前确认；exhausted 返工次数已用尽；escalate 评估者请求人工决定。
        闸门只提供实际可用的选项（有返工目标才有“返工”）。
        """
        program_id = ctx["program_id"]
        options = ["approve", "abort"]
        if node.get("rework_to"):
            options.insert(1, "rework")
        before = await store.load_program(program_id)
        await store.update_node(
            node["id"],
            status="waiting_human",
            finished_at=None,
            reason=reason,
            conflict_json={"kind": "review", "stage": stage, "message": reason},
            pending_json=None,
        )
        await self._emit_node(node["id"], status="waiting_human")
        await self._emit(
            program_id,
            "workflow.gate.opened",
            node["key"],
            {
                "flow_key": ctx["flow_key"],
                "reason": reason,
                "options": options,
                "issues": (verdict or {}).get("issues", []),
            },
        )
        await self._log(
            program_id,
            f"等待确认：{ctx['flow_key']}/{node['key']}（{reason}）",
            level="warn",
        )
        # 程序已在等待确认（另一处闸门）时不会再有状态转换，这里单独把本步骤交给主代理
        if before is not None and before["run"]["status"] == "waiting_human":
            await self._notify_program(
                program_id, lambda snap, _nid=node["id"]: _gate_notice(snap, _nid)
            )

    async def _schedule_rework(
        self,
        ctx: dict[str, Any],
        review: dict[str, Any],
        verdict: dict[str, Any] | None,
        *,
        reason: str,
        extra: str,
        checks: list[dict[str, Any]] | None = None,
    ) -> None:
        """把评估问题交回实现节点（同一子代理线程续做）；评估节点记一轮返工，
        待实现完成后在同一个评估子代理中复审（plan-workflow-collab：同一把尺子，避免标准漂移）。"""
        target_key = review.get("rework_to")
        target = await store.find_node(ctx["flow_run_id"], target_key) if target_key else None
        if target is None:
            await self._open_review_gate(
                ctx,
                review,
                verdict,
                "评估要求返工，但没有可返工的实现步骤，需要你决定",
                stage="escalate",
            )
            return
        feedback = issues_as_feedback(verdict)
        failed_checks = [c for c in (checks or review.get("checks") or []) if not c.get("ok")]
        if failed_checks:
            feedback += "\n未通过的检查：\n" + "\n".join(
                f"- {c.get('cmd') or c.get('command')}：{str(c.get('output') or '')[-400:]}"
                for c in failed_checks[:3]
            )
        if extra:
            feedback = f"{feedback}\n{report.USER_NOTE_MARK}{extra}"
        round_no = (review["rework_round"] or 0) + 1
        await store.update_node(
            target["id"],
            status="pending",
            reason=None,
            pending_json={"mode": "rework", "feedback": feedback, "source": "review"},
        )
        await store.update_node(
            review["id"],
            status="pending",
            rework_round=round_no,
            reason=f"等待『{target_key}』返工完成后复审",
            conflict_json=None,
            pending_json={
                "mode": "rework",
                "feedback": rereview_note(verdict, round_no=round_no, extra=extra),
            },
        )
        await store.bump_rework_total(ctx["program_id"])
        await self._emit_node(target["id"], status="pending")
        await self._emit_node(review["id"], status="pending")
        await self._log(
            ctx["program_id"],
            f"{reason}：第 {round_no} 轮返工，已交回『{target_key}』",
            level="warn",
        )

    async def _on_transient(self, node_id: int, request: NodeRequest, result: NodeResult) -> None:
        meta = self._meta[node_id]
        plan = list(self._retry_plan())
        count = self._retries.get(node_id, 0)
        if count < len(plan):
            delay = float(plan[count])
            self._retries[node_id] = count + 1
            next_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
            await store.update_node(
                node_id,
                status="recovering",
                next_retry_at=next_at,
                reason=f"网络重试 {count + 1}/{len(plan)}",
                error_json={"message": result.error, "transient": True},
                pending_json={"mode": "checkpoint", "feedback": ""},
            )
            self._timers[node_id] = asyncio.create_task(self._retry_later(node_id, delay))
            await self._emit_node(node_id, status="recovering", next_retry_at=next_at)
            await self._log(
                meta["program"],
                f"网络中断，节点『{meta['key']}』第 {count + 1}/{len(plan)} 次重试",
                level="warn",
            )
            return
        await store.update_node(
            node_id,
            status="interrupted",
            finished_at=store.now_iso(),
            error_json={"message": result.error, "transient": True},
            pending_json={"mode": "checkpoint", "feedback": ""},
        )
        await self._pause_program(
            meta["program"], "network", f"网络中断，已保留进度（节点『{meta['key']}』）"
        )

    async def _retry_later(self, node_id: int, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            await store.update_node(node_id, status="pending", next_retry_at=None)
        except asyncio.CancelledError:
            return
        finally:
            self._timers.pop(node_id, None)

    async def _on_failed(self, node_id: int, request: NodeRequest, result: NodeResult) -> None:
        meta = self._meta[node_id]
        if request.kind == "integrate":
            await self._gate_integration(node_id, meta, result.error[:300] or "并入主工作区失败")
            return
        if request.kind == "merge":
            await self._fail_merge(node_id, meta, result.error[:300] or "合并步骤失败")
            return
        await store.update_node(
            node_id,
            status="failed",
            finished_at=store.now_iso(),
            reason=result.error[:300],
            error_json={"message": result.error[:300]},
            # 检查节点失败时保留逐条命令结果（哪条未通过 / 未执行及原因），面板据此展示
            **({"output_json": result.output} if result.output else {}),
        )
        await self._emit_node(node_id, status="failed")
        await self._log(
            meta["program"],
            f"节点『{meta['flow_key']}/{meta['key']}』失败：{result.error[:120]}",
            level="error",
        )

    # ── 暂停、取消、程序状态 ─────────────────────────────

    def _stop_program_tasks(self, program_id: int) -> None:
        current = asyncio.current_task()
        for node_id, meta in self._meta.items():
            if meta["program"] != program_id:
                continue
            task = self._tasks.get(node_id)
            # 当前任务若正是本节点的结算过程（如网络重试耗尽后暂停），不能自我取消，否则落库中断
            if task is not None and task is not current:
                task.cancel()
            timer = self._timers.pop(node_id, None)
            if timer is not None:
                timer.cancel()

    async def _pause_program(self, program_id: int, kind: str, reason: str) -> None:
        self._stop_program_tasks(program_id)
        self._pool.drop_waiting(lambda s: s.flow_key.startswith(f"{program_id}:"))
        snap = await store.load_program(program_id)
        if snap is None:
            return
        for flow in snap["flows"]:
            for node in flow["nodes"]:
                if node["status"] == "queued":
                    await store.update_node(node["id"], status="pending", reason=None)
            if flow["run"]["status"] in ("running", "waiting_human"):
                await store.update_run(flow["run"]["id"], status="paused")
        await store.update_run(
            program_id,
            status="paused",
            pause_kind=kind,
            pause_reason=reason[:300],
            paused_at=store.now_iso(),
        )
        # 暂停时记录写入范围的指纹：续跑前据此发现外部改动（§6.6）
        await self._capture_fingerprint(program_id)
        await self._emit_run(program_id)
        await self._log(program_id, f"已暂停：{reason}", level="warn")
        if kind == "network":
            self._schedule_auto_resume(program_id)

    def _schedule_auto_resume(self, program_id: int) -> None:
        """网络中断暂停后，按设置在稍后自动续跑一次（设置关闭或已尝试过则不调度）。"""
        from app.core.config import settings

        if not settings.workflow_auto_resume_on_network or program_id in self._auto_resume_tried:
            return
        self._auto_resume_tried.add(program_id)
        asyncio.get_running_loop().create_task(self._auto_resume_later(program_id))

    async def _auto_resume_later(self, program_id: int) -> None:
        await asyncio.sleep(AUTO_RESUME_DELAY_SEC)
        snap = await store.load_program(program_id)
        if (
            snap is None
            or snap["run"]["status"] != "paused"
            or snap["run"]["pause_kind"] != "network"
        ):
            return
        try:
            await self.resume(program_id, note="网络恢复后自动续跑", automatic=True)
        except ValueError as exc:  # 例如工作区有改动需要确认、预算已用完：保持暂停，交给用户
            logger.info("[workflow] 自动续跑未执行：%s", exc)

    async def _capture_fingerprint(self, program_id: int) -> None:
        """暂停时记录工作区：写入范围内文件的指纹与小型文本内容，以及 Git 的 HEAD（§6.6）。"""
        snap = await store.load_program(program_id)
        if snap is None or not snap["run"]["workspace"]:
            return
        workspace = snap["run"]["workspace"]
        patterns = _scope_patterns(snap)
        files: dict[str, str] = {}
        contents: dict[str, str] = {}
        if patterns:
            files = await asyncio.to_thread(snapshot_fingerprint, workspace, patterns)
            contents = await asyncio.to_thread(snapshot_contents, workspace, list(files))
        head = await isolation.head_of(workspace)
        await store.update_run(
            program_id, fingerprint_json=fingerprint_record(files, contents, head)
        )

    async def drift_report(self, program_id: int) -> dict[str, Any]:
        """续跑前的工作区报告：写入范围内被外部改动的文件，以及暂停期间 HEAD 的变化。

        未记录指纹时视为无漂移；HEAD 变化只作提示，不阻止续跑（用户可能只是提交了自己的改动）。
        """
        record = await store.load_fingerprint(program_id)
        snap = await store.load_program(program_id)
        if not record or snap is None or not snap["run"]["workspace"]:
            return {"files": [], "head": None}
        workspace = snap["run"]["workspace"]
        patterns = _scope_patterns(snap)
        files: list[str] = []
        if patterns:
            after = await asyncio.to_thread(snapshot_fingerprint, workspace, patterns)
            files = diff_fingerprints(files_of(record), after)
        head_then = head_of_record(record)
        head_now = await isolation.head_of(workspace)
        head = (
            {"from": head_then, "to": head_now}
            if head_then and head_now and head_then != head_now
            else None
        )
        return {"files": files, "head": head}

    async def drift_of(self, program_id: int) -> list[str]:
        """暂停后写入范围内被外部改动（或新增 / 删除）的文件；未记录指纹时视为无漂移。"""
        return (await self.drift_report(program_id))["files"]

    async def drift_diff(self, program_id: int, rel: str) -> dict[str, Any]:
        """某个漂移文件自暂停以来的差异（统一格式）。只接受当前改动清单中的文件，避免读取任意路径。"""
        record = await store.load_fingerprint(program_id)
        snap = await store.load_program(program_id)
        if not record or snap is None or not snap["run"]["workspace"]:
            raise ValueError("没有暂停时的记录，无法比较")
        if rel not in await self.drift_of(program_id):
            raise ValueError("该文件不在本次改动清单中")
        workspace = snap["run"]["workspace"]
        existed = files_of(record).get(rel, ABSENT) != ABSENT
        now_path = Path(workspace) / rel
        exists_now = await asyncio.to_thread(now_path.is_file)
        status = "added" if not existed else ("modified" if exists_now else "deleted")
        before_text = contents_of(record).get(rel, "")
        if existed and rel not in contents_of(record):
            return {"path": rel, "status": status, "diff": None, "reason": _TOO_LARGE_TO_DIFF}
        after_text = ""
        if exists_now:
            text = await asyncio.to_thread(read_text_for_diff, now_path)
            if text is None:
                return {"path": rel, "status": status, "diff": None, "reason": _TOO_LARGE_TO_DIFF}
            after_text = text
        return {
            "path": rel,
            "status": status,
            "diff": unified_diff(rel, before_text, after_text),
            "reason": None,
        }

    async def _cancel_flow(self, program_id: int, flow: dict[str, Any]) -> None:
        current = asyncio.current_task()
        for node_id, meta in list(self._meta.items()):
            if meta["program"] == program_id and meta["flow_run"] == flow["run"]["id"]:
                task = self._tasks.get(node_id)
                if task is not None and task is not current:
                    task.cancel()
                timer = self._timers.pop(node_id, None)
                if timer is not None:
                    timer.cancel()
        self._pool.drop_waiting(
            lambda s: s.flow_key == f"{program_id}:{flow['run']['package_key']}"
        )
        for node in flow["nodes"]:
            if node["status"] not in ("done", "skipped", "failed", "cancelled"):
                await store.update_node(
                    node["id"], status="cancelled", reason=None, finished_at=store.now_iso()
                )
        await store.update_run(flow["run"]["id"], status="cancelled", finished_at=store.now_iso())
        await self._log(program_id, f"已中止子工作流『{flow['run']['package_key']}』", level="warn")
        ws = str(flow["run"].get("workspace") or "")
        if flow["run"].get("isolation") == "worktree" and ws and os.path.isdir(ws):
            snap = await store.load_program(program_id)
            main = str(((snap or {}).get("run") or {}).get("workspace") or "")
            if main:
                await isolation.remove(
                    main,
                    worktree=ws,
                    branch=isolation.branch_name(program_id, flow["run"]["package_key"] or ""),
                    keep_branch=True,
                )

    async def _recalc(self, program_id: int) -> None:
        """由节点状态派生子工作流与程序状态；暂停与取消为人工状态，不被覆盖。"""
        snap = await store.load_program(program_id)
        if snap is None:
            return
        flow_status: dict[str, str] = {}
        for flow in snap["flows"]:
            run = flow["run"]
            if run["status"] == "paused":
                flow_status[run["package_key"]] = "paused"
                continue
            status = derive_flow_status(
                [n["status"] for n in flow["nodes"]],
                cancelled=run["status"] == "cancelled",
            )
            if status != run["status"]:
                await store.update_run(
                    run["id"],
                    status=status,
                    finished_at=store.now_iso() if status in _TERMINAL_FLOW else None,
                )
                await self._log(
                    program_id, f"子工作流『{run['package_key']}』{_flow_label(status)}"
                )
            flow_status[run["package_key"]] = status
        root = snap["run"]
        if root["status"] in ("paused", "cancelled"):
            return
        program_status = derive_program_status(flow_status.values())
        if program_status == root["status"]:
            return
        await store.update_run(
            program_id,
            status=program_status,
            finished_at=store.now_iso() if program_status in _TERMINAL_FLOW else None,
        )
        await self._emit_run(program_id)
        # 状态转换时各通知一次主代理（完成 / 失败 / 等待用户决定）
        if program_status == "succeeded":
            await self._log(program_id, "工作流已完成")
            await self._notify_program(program_id, _succeeded_notice)
        elif program_status == "failed":
            await self._log(program_id, "工作流失败，可在节点上重试或调整计划", level="error")
            await self._notify_program(program_id, _failed_notice)
        elif program_status == "waiting_human":
            await self._notify_program(program_id, _gate_notice)

    # ── 事件 ─────────────────────────────────────────────

    async def _emit(
        self, program_id: int, event_type: str, node_key: str | None, payload: dict[str, Any]
    ) -> None:
        emitted = await store.emit_event(program_id, event_type, node_key, payload)
        await self._broadcast(emitted["session_id"], emitted["envelope"])

    async def _log(self, program_id: int, text: str, *, level: str = "info") -> None:
        await self._emit(program_id, "workflow.log.appended", None, {"text": text, "level": level})

    async def _emit_node(self, node_id: int, **fields: Any) -> None:
        meta = self._meta.get(node_id)
        if meta is None:
            return
        await self._emit(
            meta["program"],
            "workflow.node.updated",
            meta["key"],
            {"flow_key": meta["flow_key"], "node_id": node_id, **fields},
        )

    async def _emit_run(self, program_id: int) -> None:
        snap = await store.load_program(program_id)
        if snap is None:
            return
        nodes = [n for f in snap["flows"] for n in f["nodes"]]
        done = sum(1 for n in nodes if n["status"] in ("done", "skipped"))
        await self._emit(
            program_id,
            "workflow.run.updated",
            None,
            {
                "status": snap["run"]["status"],
                "pause_kind": snap["run"]["pause_kind"],
                "progress": {"done": done, "total": len(nodes)},
                "session_id": snap["run"]["session_id"],
            },
        )

    async def _command(self, command: str, cwd: str) -> dict[str, Any]:
        return await self._command_runner(command, cwd)


def _round_entry(
    round_no: int,
    decision: ReviewDecision,
    verdict: dict[str, Any] | None,
    checks: list[dict[str, Any]],
    checks_ok: bool,
) -> dict[str, Any]:
    """一轮评估的记录：结论、问题与处理结果（过程报告与面板“评估记录”共用）。"""
    outcome = {"pass": "passed", "record": "recorded"}.get(decision.action, "pending")
    if decision.action == "rework" and not settings.workflow_rework_confirm:
        outcome = "rework"
    issues = [
        {
            "severity": i.get("severity"),
            "file": i.get("file"),
            "line": i.get("line"),
            "problem": str(i.get("problem") or "")[:300],
        }
        for i in (verdict or {}).get("issues", [])
    ][:12]
    return {
        "round": round_no,
        "action": decision.action,
        "reason": decision.reason,
        "summary": str((verdict or {}).get("summary") or "")[:400],
        "issues": issues,
        "checks_ok": checks_ok and all(bool(c.get("ok")) for c in checks),
        "outcome": outcome,
        "at": store.now_iso(),
    }


def _merge_allowed(gate: dict[str, Any]) -> bool:
    """L3 合并步骤只处理同文件真冲突：要求冲突策略为 queue 且允许自动合并，否则直接问用户。"""
    return (
        gate.get("kind") == "conflict"
        and settings.workflow_conflict_policy == "queue"
        and settings.workflow_auto_merge
    )


def _merge_task(paths: list[str], gate: dict[str, Any]) -> str:
    """合并步骤的任务说明：并入冲突（文件含冲突标记）或同文件三版内容（基线 / 当前 / 待写入）。"""
    names = "、".join(paths)
    if gate.get("integration"):
        return (
            f"把子工作流的改动并入主工作区时，以下文件出现了 git 冲突：{names}。"
            "文件中冲突标记（<<<<<<< / ======= / >>>>>>>）之间是两侧各自的内容。"
            "请逐个解决：保留双方各自的意图，删除全部冲突标记，用 fs_write 写入解决后的完整内容"
            "（只能写这些文件）。最终结论中简要说明解决了哪些冲突。"
        )
    path = paths[0] if paths else ""
    return (
        f"两个步骤同时修改了『{path}』，自动合并没有成功。请把两侧的改动合并为一版，"
        "保留双方各自的意图，然后用 fs_write 写入这个文件（只能写它）。"
        "最终结论中简要说明合并了哪些改动。\n\n"
        f"【基线（修改前读取到的版本）】\n{gate.get('base') or ''}\n\n"
        f"【当前磁盘版本（其它步骤已写入）】\n{gate.get('current') or ''}\n\n"
        f"【本次待写入版本（本步骤想写入的内容）】\n{gate.get('pending') or ''}"
    )


def _gate_options(gate: dict[str, Any]) -> list[str]:
    """冲突类闸门的决定选项：越界改动只能纳入范围或中止；写入冲突可保留、再试或中止。"""
    if gate.get("kind") == "scope_violation":
        return ["include", "abort"]
    return ["approve", "rework", "abort"]


def _find_flow(snap: dict[str, Any], flow_key: str) -> dict[str, Any] | None:
    for flow in snap["flows"]:
        if flow["run"]["package_key"] == flow_key:
            return flow
    return None


def _find_node(flow: dict[str, Any], node_key: str) -> dict[str, Any] | None:
    for node in flow["nodes"]:
        if node["key"] == node_key:
            return node
    return None


def _flow_label(status: str) -> str:
    return {
        "running": "开始运行",
        "waiting_human": "等待你的决定",
        "failed": "失败",
        "succeeded": "已完成",
        "paused": "已暂停",
        "cancelled": "已取消",
    }.get(status, status)
