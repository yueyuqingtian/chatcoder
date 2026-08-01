"""任务 DAG 引擎：构建、拓扑排序、动态变更（insert/cancel/replan）。"""
from collections import deque
from dataclasses import dataclass, field


@dataclass
class TaskNode:
    task_id: int
    title: str
    assignee_id: int | None = None
    depends_on: list[int] = field(default_factory=list)
    status: str = "pending"


@dataclass
class TaskDAG:
    """任务有向无环依赖图。"""

    session_id: int
    nodes: dict[int, TaskNode] = field(default_factory=dict)
    # 邻接表：from -> [to...]
    adjacency: dict[int, list[int]] = field(default_factory=dict)
    in_degree: dict[int, int] = field(default_factory=dict)

    def add_node(self, node: TaskNode) -> None:
        self.nodes[node.task_id] = node
        self.adjacency.setdefault(node.task_id, [])
        self.in_degree.setdefault(node.task_id, 0)
        for dep in node.depends_on:
            self.adjacency.setdefault(dep, []).append(node.task_id)
            self.in_degree[node.task_id] = self.in_degree.get(node.task_id, 0) + 1

    def add_edge(self, from_id: int, to_id: int) -> None:
        """运行时动态加边（insert 场景）。"""
        if to_id not in self.adjacency.setdefault(from_id, []):
            self.adjacency[from_id].append(to_id)
            self.in_degree[to_id] = self.in_degree.get(to_id, 0) + 1
            if to_id in self.nodes:
                self.nodes[to_id].depends_on.append(from_id)

    def cancel(self, task_id: int) -> None:
        """运行时取消节点（cancel 场景）：移除其所有边。"""
        if task_id in self.adjacency:
            for child in self.adjacency[task_id]:
                self.in_degree[child] = max(0, self.in_degree.get(child, 0) - 1)
            del self.adjacency[task_id]
        # 从其他节点的 depends_on 移除
        for node in self.nodes.values():
            if task_id in node.depends_on:
                node.depends_on.remove(task_id)
        self.nodes.pop(task_id, None)
        self.in_degree.pop(task_id, None)

    def topological_order(self) -> list[int]:
        """Kahn 算法拓扑排序。"""
        in_deg = dict(self.in_degree)
        queue = deque([n for n, d in in_deg.items() if d == 0])
        order: list[int] = []
        while queue:
            node = queue.popleft()
            order.append(node)
            for child in self.adjacency.get(node, []):
                in_deg[child] -= 1
                if in_deg[child] == 0:
                    queue.append(child)
        return order

    def ready_tasks(self) -> list[int]:
        """返回当前可调度的任务：入度为 0 且状态为 pending。"""
        return [
            tid
            for tid, deg in self.in_degree.items()
            if deg == 0 and self.nodes[tid].status == "pending"
        ]

    def parallel_layers(self) -> list[list[int]]:
        """按层级分组，同层可并行执行。"""
        in_deg = dict(self.in_degree)
        layers: list[list[int]] = []
        current = [n for n, d in in_deg.items() if d == 0]
        while current:
            layers.append(current)
            next_layer: list[int] = []
            for node in current:
                for child in self.adjacency.get(node, []):
                    in_deg[child] -= 1
                    if in_deg[child] == 0:
                        next_layer.append(child)
            current = next_layer
        return layers

    def has_cycle(self) -> bool:
        """检测是否存在环。"""
        return len(self.topological_order()) != len(self.nodes)
