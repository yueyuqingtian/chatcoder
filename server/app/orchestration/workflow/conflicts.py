"""工作流写入范围、进程级租约与读账本（纯逻辑，无数据库与运行时副作用）。

对应设计 §5A.1：写入范围是契约；租约以工作区根路径为键、进程级共享（跨会话可见）；
全部或无加锁；写入前 CAS（读账本记录内容哈希，小文件保留快照作为三方合并基线）。
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass

# 读账本保留内容快照的上限；超过则只存哈希，冲突时降级为重做（L2）
SNAPSHOT_LIMIT_BYTES = 256 * 1024

_GLOB_CHARS = frozenset("*?[")


def normalize_scope(pattern: str) -> str | None:
    """把写入范围规范化为工作区内的 POSIX 相对路径。

    绝对路径、盘符、含 `..` 的范围视为非法，返回 None。
    """
    raw = (pattern or "").strip().replace("\\", "/")
    if not raw:
        return None
    if raw.startswith("/") or (len(raw) > 1 and raw[1] == ":"):
        return None
    segments: list[str] = []
    for seg in raw.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            return None
        segments.append(seg)
    return "/".join(segments) or None


def _static_prefix(scope: str) -> tuple[str, ...]:
    """取范围中第一个含通配符的段之前的静态目录前缀。"""
    out: list[str] = []
    for seg in scope.split("/"):
        if any(ch in _GLOB_CHARS for ch in seg):
            break
        out.append(seg)
    return tuple(out)


def _is_segment_prefix(head: tuple[str, ...], full: tuple[str, ...]) -> bool:
    return len(head) <= len(full) and full[: len(head)] == head


def scopes_overlap(a: str, b: str) -> bool:
    """两个写入范围是否可能命中同一文件。

    保守判定：拿不准（含非法范围）一律视为重叠。
    """
    na, nb = normalize_scope(a), normalize_scope(b)
    if na is None or nb is None:
        return True
    pa, pb = _static_prefix(na), _static_prefix(nb)
    return _is_segment_prefix(pa, pb) or _is_segment_prefix(pb, pa)


@dataclass(frozen=True)
class LeaseOwner:
    """租约持有者。

    同一节点（run_id + node_key）的多次 attempt 视为同一持有者，不会自我冲突。
    """

    run_id: int
    node_key: str
    attempt_id: int | None = None
    session_id: int | None = None  # 仅作审计字段，不参与互斥判定


@dataclass(frozen=True)
class WriteLease:
    workspace: str
    pattern: str
    owner: LeaseOwner
    acquired_at: float


@dataclass(frozen=True)
class LeaseConflict:
    requested: str
    held: str
    holder: LeaseOwner


def _identity(owner: LeaseOwner) -> tuple[int, str]:
    return (owner.run_id, owner.node_key)


class WriteLeaseTable:
    """进程级写入租约表：键为工作区根路径；同一工作区内重叠的写入范围互斥。"""

    def __init__(self) -> None:
        self._leases: list[WriteLease] = []
        self._lock = threading.RLock()

    @staticmethod
    def workspace_key(workspace: str) -> str:
        return os.path.normcase(os.path.abspath(workspace))

    def acquire(
        self, workspace: str, owner: LeaseOwner, patterns: Iterable[str]
    ) -> tuple[bool, list[LeaseConflict]]:
        """全部或无地申请写入范围。

        任一范围与他人持有的范围重叠，则整体拒绝且不记录任何租约
        （不存在“持有一部分再等另一部分”）。返回 (是否授予, 冲突列表)。
        """
        ws = self.workspace_key(workspace)
        wanted: list[str] = []
        for p in patterns:
            n = normalize_scope(p)
            if n is None:
                raise ValueError(f"非法写入范围：{p!r}")
            wanted.append(n)
        wanted = sorted(set(wanted))  # 按路径排序，保证结果与日志顺序确定
        ident = _identity(owner)
        with self._lock:
            conflicts: list[LeaseConflict] = []
            for req in wanted:
                for lease in self._leases:
                    if lease.workspace != ws or _identity(lease.owner) == ident:
                        continue
                    if scopes_overlap(req, lease.pattern):
                        conflicts.append(LeaseConflict(req, lease.pattern, lease.owner))
            if conflicts:
                return False, conflicts
            now = time.time()
            held = {
                (lease.workspace, lease.pattern)
                for lease in self._leases
                if _identity(lease.owner) == ident
            }
            for req in wanted:
                if (ws, req) not in held:
                    self._leases.append(WriteLease(ws, req, owner, now))
            return True, []

    def release(self, workspace: str, owner: LeaseOwner) -> list[WriteLease]:
        """释放某节点在该工作区的全部租约（节点结束、暂停、取消时调用）。"""
        ws = self.workspace_key(workspace)
        ident = _identity(owner)
        with self._lock:
            freed = [
                lease
                for lease in self._leases
                if lease.workspace == ws and _identity(lease.owner) == ident
            ]
            self._leases = [
                lease
                for lease in self._leases
                if not (lease.workspace == ws and _identity(lease.owner) == ident)
            ]
            return freed

    def release_run(self, run_id: int) -> list[WriteLease]:
        """释放某运行的全部租约（暂停、取消、启动恢复扫描时调用，避免遗留租约）。"""
        with self._lock:
            freed = [lease for lease in self._leases if lease.owner.run_id == run_id]
            self._leases = [lease for lease in self._leases if lease.owner.run_id != run_id]
            return freed

    def holders(self, workspace: str | None = None) -> list[WriteLease]:
        with self._lock:
            if workspace is None:
                return list(self._leases)
            ws = self.workspace_key(workspace)
            return [lease for lease in self._leases if lease.workspace == ws]


_TABLE = WriteLeaseTable()


def get_lease_table() -> WriteLeaseTable:
    """进程级单例：所有会话、所有程序共用，保证跨会话同一工作区也能互斥。"""
    return _TABLE


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class ReadRecord:
    sha: str
    snapshot: str | None


@dataclass(frozen=True)
class CasResult:
    ok: bool
    # no_prior_read 尚无读取记录 / unchanged 未变可写 / changed 被锁外改动 / deleted 已被删除
    reason: str
    base_snapshot: str | None = None


class ReadLedger:
    """单个 attempt 的读账本：记录读取时的内容哈希；小文件另存快照，作为三方合并基线。"""

    def __init__(self) -> None:
        self._records: dict[str, ReadRecord] = {}

    @staticmethod
    def _key(path: str) -> str:
        return os.path.normcase(normalize_scope(path) or path)

    def record_read(self, path: str, text: str) -> None:
        snap = text if len(text.encode("utf-8")) <= SNAPSHOT_LIMIT_BYTES else None
        self._records[self._key(path)] = ReadRecord(content_hash(text), snap)

    def mark_written(self, path: str, text: str) -> None:
        """写入成功后，本 attempt 的基线即为刚写入的内容（同一 attempt 连续编辑不误判冲突）。"""
        self.record_read(path, text)

    def verify_write(self, path: str, current_text: str | None) -> CasResult:
        rec = self._records.get(self._key(path))
        if rec is None:
            return CasResult(False, "no_prior_read")
        if current_text is None:
            return CasResult(False, "deleted", rec.snapshot)
        if content_hash(current_text) == rec.sha:
            return CasResult(True, "unchanged", rec.snapshot)
        return CasResult(False, "changed", rec.snapshot)


class ReadIndex:
    """读集索引：文件 → 读过它的 attempt。

    写入后据此向其它运行中 attempt 发失效通知（§5A.1 第 10 点）。
    """

    def __init__(self) -> None:
        self._readers: dict[str, set[str]] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _key(workspace: str, path: str) -> str:
        ws = os.path.normcase(os.path.abspath(workspace))
        return f"{ws}::{normalize_scope(path) or path}"

    def add(self, workspace: str, path: str, attempt_key: str) -> None:
        with self._lock:
            self._readers.setdefault(self._key(workspace, path), set()).add(attempt_key)

    def remove_attempt(self, attempt_key: str) -> None:
        with self._lock:
            for readers in self._readers.values():
                readers.discard(attempt_key)

    def readers_of(self, workspace: str, path: str, *, exclude: str | None = None) -> list[str]:
        with self._lock:
            readers = self._readers.get(self._key(workspace, path), set())
            return sorted(r for r in readers if r != exclude)
