"""工作流状态推进（纯逻辑，设计 §5.3）：节点就绪判定与子工作流 / 程序状态派生。"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

# 节点“已完成”语义：依赖它的下游可以启动
SATISFIED_NODE_STATUSES = frozenset({"done", "skipped"})


@dataclass(frozen=True)
class NodeView:
    key: str
    status: str
    inputs: tuple[str, ...]


def ready_node_keys(nodes: Sequence[NodeView], *, flow_ready: bool) -> list[str]:
    """待启动（pending）且全部上游已满足的节点。

    flow_ready 为 False（所依赖的子工作流尚未成功）时，本子工作流没有可启动节点。
    """
    if not flow_ready:
        return []
    by_key = {n.key: n for n in nodes}
    ready: list[str] = []
    for node in nodes:
        if node.status != "pending":
            continue
        upstream_ok = all(
            by_key[inp].status in SATISFIED_NODE_STATUSES for inp in node.inputs if inp in by_key
        )
        if upstream_ok:
            ready.append(node.key)
    return ready


def derive_flow_status(statuses: Iterable[str], *, cancelled: bool = False) -> str:
    """由节点状态派生子工作流状态。"""
    if cancelled:
        return "cancelled"
    items = list(statuses)
    if not items:
        return "succeeded"
    if "waiting_human" in items:
        return "waiting_human"
    if "failed" in items:
        return "failed"
    if all(s in ("done", "skipped", "cancelled") for s in items):
        return "cancelled" if all(s == "cancelled" for s in items) else "succeeded"
    if "interrupted" in items and not any(s in ("running", "recovering", "queued") for s in items):
        return "paused"
    return "running"


def derive_program_status(flow_statuses: Iterable[str]) -> str:
    """由子工作流状态派生程序状态。被用户中止（cancelled）的子工作流不计入失败。"""
    items = list(flow_statuses)
    if not items:
        return "succeeded"
    if all(s == "cancelled" for s in items):
        return "cancelled"
    if "failed" in items:
        return "failed"
    if "waiting_human" in items:
        return "waiting_human"
    if "paused" in items:
        return "paused"
    if "running" in items:
        return "running"
    return "succeeded"
