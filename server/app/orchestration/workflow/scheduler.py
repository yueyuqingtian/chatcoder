"""工作流并发池（纯逻辑，无线程与运行时副作用）。

对应设计 §5.4：全局、单会话、单子工作流三级上限取最紧者；0 表示“不限”。
等待队列按“会话间轮询、会话内 FIFO（跳过当前不可启动的项）”分配名额，单个会话不会饿死其他会话。
写入租约（§5A）由调用方在启动前另行检查，本模块只管并发名额。
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

REASON_GRANTED = "granted"
REASON_GLOBAL_FULL = "global_full"
REASON_SESSION_FULL = "session_full"
REASON_FLOW_FULL = "flow_full"

REASON_TEXT: dict[str, str] = {
    REASON_GLOBAL_FULL: "全局上限已满",
    REASON_SESSION_FULL: "本会话上限已满",
    REASON_FLOW_FULL: "子工作流内并行上限已满",
}


@dataclass(frozen=True)
class PoolLimits:
    global_limit: int = 3  # 所有会话合计，0 = 不限（保守默认）
    per_session_limit: int = 2  # 同一会话合计，0 = 不限（保守默认）
    per_flow_limit: int = 2  # 单个子工作流内并行节点，至少为 1

    def __post_init__(self) -> None:
        if self.global_limit < 0 or self.per_session_limit < 0:
            raise ValueError("并发上限不能为负数（0 表示不限）")
        if self.per_flow_limit < 1:
            raise ValueError("子工作流内并行上限至少为 1")


@dataclass(frozen=True)
class Slot:
    node_id: str  # 节点尝试的唯一标识，如 "run12:impl:attempt3"
    session_id: int
    flow_key: str  # 子工作流标识


@dataclass(frozen=True)
class AdmitResult:
    granted: bool
    reason: str  # granted / global_full / session_full / flow_full


class WorkflowPool:
    def __init__(self, limits: PoolLimits | None = None) -> None:
        self._limits = limits or PoolLimits()
        self._running: dict[str, Slot] = {}
        self._waiting: dict[int, deque[Slot]] = {}
        self._rotation: deque[int] = deque()  # 会话轮询顺序，仅包含仍有排队项的会话
        self._lock = threading.RLock()

    @property
    def limits(self) -> PoolLimits:
        return self._limits

    def set_limits(self, limits: PoolLimits) -> None:
        """调小不撤销已在运行的节点；调大后需调用 pop_admissible 唤醒排队项。"""
        with self._lock:
            self._limits = limits

    def admit(self, slot: Slot) -> AdmitResult:
        """申请名额：有余量则立即授予；否则进入排队并返回阻塞原因。"""
        with self._lock:
            if slot.node_id in self._running:
                return AdmitResult(True, REASON_GRANTED)
            if self._is_waiting(slot.node_id):
                return AdmitResult(False, self._decide(self._find_waiting(slot.node_id)))
            reason = self._decide(slot)
            if reason == REASON_GRANTED:
                self._running[slot.node_id] = slot
                return AdmitResult(True, reason)
            self._enqueue(slot)
            return AdmitResult(False, reason)

    def release(self, node_id: str) -> Slot | None:
        """释放名额（幂等）。调用方随后应调用 pop_admissible 唤醒排队项。"""
        with self._lock:
            return self._running.pop(node_id, None)

    def enqueue(self, slot: Slot) -> None:
        """把节点放入等待队列（不立即授予）；已在运行或已排队则忽略。"""
        with self._lock:
            if slot.node_id in self._running or self._is_waiting(slot.node_id):
                return
            self._enqueue(slot)

    def pop_admissible(self, accept: Callable[[Slot], bool] | None = None) -> list[Slot]:
        """在余量允许的范围内授予排队项：会话间轮询，会话内取第一个可启动的项。

        accept 为额外准入检查（如写入范围租约）：返回 False 的项保持排队，不占名额。
        """
        with self._lock:
            granted: list[Slot] = []
            while True:
                picked: Slot | None = None
                for _ in range(len(self._rotation)):
                    sid = self._rotation[0]
                    self._rotation.rotate(-1)
                    picked = self._take_first_admissible(sid, accept)
                    if picked is not None:
                        break
                if picked is None:
                    break
                self._running[picked.node_id] = picked
                granted.append(picked)
            self._prune_empty_sessions()
            return granted

    def drop_waiting(self, predicate: Callable[[Slot], bool]) -> list[Slot]:
        """移除满足条件的排队项（如某运行被暂停或取消）。"""
        with self._lock:
            dropped: list[Slot] = []
            for queue in self._waiting.values():
                kept = deque()
                for slot in queue:
                    (dropped if predicate(slot) else kept).append(slot)
                queue.clear()
                queue.extend(kept)
            self._prune_empty_sessions()
            return dropped

    def block_reason(self, node_id: str) -> str | None:
        """排队项当前的阻塞原因（供 UI 显示“等待……”）；未排队则为 None。"""
        with self._lock:
            slot = self._find_waiting(node_id)
            return None if slot is None else self._decide(slot)

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            per_session: dict[int, int] = {}
            for slot in self._running.values():
                per_session[slot.session_id] = per_session.get(slot.session_id, 0) + 1
            return {
                "global": {"in_use": len(self._running), "limit": self._limits.global_limit},
                "session": {
                    "limit": self._limits.per_session_limit,
                    "in_use": per_session,
                },
                "queued": sum(len(q) for q in self._waiting.values()),
            }

    # ── 内部 ─────────────────────────────────────────────

    def _decide(self, slot: Slot) -> str:
        lim = self._limits
        if lim.global_limit and len(self._running) >= lim.global_limit:
            return REASON_GLOBAL_FULL
        if lim.per_session_limit and self._session_in_use(slot.session_id) >= lim.per_session_limit:
            return REASON_SESSION_FULL
        if self._flow_in_use(slot.flow_key) >= lim.per_flow_limit:
            return REASON_FLOW_FULL
        return REASON_GRANTED

    def _session_in_use(self, session_id: int) -> int:
        return sum(1 for s in self._running.values() if s.session_id == session_id)

    def _flow_in_use(self, flow_key: str) -> int:
        return sum(1 for s in self._running.values() if s.flow_key == flow_key)

    def _enqueue(self, slot: Slot) -> None:
        queue = self._waiting.setdefault(slot.session_id, deque())
        if slot.session_id not in self._rotation:
            self._rotation.append(slot.session_id)
        queue.append(slot)

    def _is_waiting(self, node_id: str) -> bool:
        return self._find_waiting(node_id) is not None

    def _find_waiting(self, node_id: str) -> Slot | None:
        for queue in self._waiting.values():
            for slot in queue:
                if slot.node_id == node_id:
                    return slot
        return None

    def _take_first_admissible(
        self, session_id: int, accept: Callable[[Slot], bool] | None
    ) -> Slot | None:
        queue = self._waiting.get(session_id)
        if not queue:
            return None
        for idx, slot in enumerate(queue):
            if self._decide(slot) == REASON_GRANTED and (accept is None or accept(slot)):
                del queue[idx]
                return slot
        return None

    def _prune_empty_sessions(self) -> None:
        for sid in [sid for sid, q in self._waiting.items() if not q]:
            del self._waiting[sid]
        self._rotation = deque(sid for sid in self._rotation if sid in self._waiting)
