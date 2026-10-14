"""节点执行器接口（设计 §5.5）。

运行时只依赖本接口：真实实现由子代理（SubagentManager）完成，测试中由脚本化的假执行器替代，
从而不依赖真实模型即可验证调度、返工、闸门与续跑。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class NodeRequest:
    run_id: int  # 程序（根）运行 id
    session_id: int
    flow_key: str
    node_key: str
    node_id: int
    attempt_id: int
    attempt_no: int
    kind: str  # explore / execute / review / merge
    role: str | None
    task: str
    acceptance: tuple[str, ...]
    write_scope: tuple[str, ...]
    # fresh 新建 / checkpoint 断点续跑 / rework 返工 / repair 追问格式
    mode: str
    feedback: str  # 返工说明、续跑补充说明或格式修复提示
    upstream: dict[str, dict[str, Any]] = field(default_factory=dict)  # 上游节点的结构化产出
    workspace: str = ""
    goal: str = ""  # 程序目标（作为原始诉求注入子代理上下文）
    agent_id: int | None = None  # 该节点上一次执行的子代理（续跑 / 返工时沿用同一线程）
    turn_id: int | None = None  # 归属的主代理轮次
    commands: tuple[str, ...] = ()  # 检查节点的命令（确定性执行，不调用模型）
    question: str = ""  # 闸门节点等待决定的问题
    rework_source: str = ""  # 返工来源：review 表示评估打回（记入返工记录）；其它为冲突重做等
    # 子代理一经派发即回报（plan-workflow-collab）：节点运行中就指向当前子代理，点开不会进错线程
    on_agent: Callable[[int], Awaitable[None]] | None = field(default=None, compare=False)


@dataclass
class NodeResult:
    status: str  # done / failed / interrupted
    text: str = ""  # 最终输出原文（评估节点据此解析 verdict）
    output: dict[str, Any] | None = None  # 结构化产出（探索的 plan、实现的 verification 等）
    error: str = ""
    transient: bool = False  # 网络 / 网关类瞬时故障：可重试或暂停为可续跑
    tokens: int = 0
    agent_id: int | None = None
    tool_trace: list[dict[str, Any]] = field(default_factory=list)


class NodeExecutor(Protocol):
    async def run(self, req: NodeRequest) -> NodeResult: ...
