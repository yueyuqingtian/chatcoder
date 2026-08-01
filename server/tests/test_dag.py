"""TaskDAG 引擎单测。"""
from app.orchestration.dag import TaskDAG, TaskNode


def _make_dag() -> TaskDAG:
    """构造示例 DAG:
       1 → 2 → 4
       1 → 3 ↗
    节点 1 入度0,2/3 依赖1,4 依赖 2 与 3。
    """
    dag = TaskDAG(session_id=1)
    dag.add_node(TaskNode(task_id=1, title="A"))
    dag.add_node(TaskNode(task_id=2, title="B", depends_on=[1]))
    dag.add_node(TaskNode(task_id=3, title="C", depends_on=[1]))
    dag.add_node(TaskNode(task_id=4, title="D", depends_on=[2, 3]))
    return dag


def test_topological_order_linear_and_parallel():
    dag = _make_dag()
    order = dag.topological_order()
    # 1 必须在 2/3 之前,2/3 必须在 4 之前
    assert order[0] == 1
    assert order[-1] == 4
    assert set(order[1:3]) == {2, 3}
    assert len(order) == 4


def test_parallel_layers():
    dag = _make_dag()
    layers = dag.parallel_layers()
    assert layers[0] == [1]
    assert set(layers[1]) == {2, 3}
    assert layers[2] == [4]
    assert len(layers) == 3


def test_ready_tasks_initial():
    dag = _make_dag()
    assert dag.ready_tasks() == [1]


def test_ready_tasks_after_completion():
    dag = _make_dag()
    # 模拟 1 完成(状态改 done)
    dag.nodes[1].status = "done"
    # 入度0且pending:无(因为 2/3 入度=1)
    assert dag.ready_tasks() == []
    # 减入度(模拟调度器行为)
    for child in dag.adjacency[1]:
        dag.in_degree[child] -= 1
    ready = set(dag.ready_tasks())
    assert ready == {2, 3}


def test_add_edge_runtime():
    dag = TaskDAG(session_id=1)
    dag.add_node(TaskNode(task_id=1, title="A"))
    dag.add_node(TaskNode(task_id=2, title="B"))
    dag.add_edge(1, 2)
    # 现在 2 依赖 1
    assert dag.in_degree[2] == 1
    assert 2 in dag.adjacency[1]
    assert dag.ready_tasks() == [1]
    # 重复加边幂等
    dag.add_edge(1, 2)
    assert dag.in_degree[2] == 1


def test_cancel_removes_edges():
    dag = _make_dag()
    dag.cancel(2)
    # 2 被移除
    assert 2 not in dag.nodes
    # 4 的入度应减 1(原本 2 → 4 的边)
    assert dag.in_degree[4] == 1  # 还剩 3 → 4
    # 4 的 depends_on 应不含 2
    assert 2 not in dag.nodes[4].depends_on


def test_has_cycle_no_cycle():
    dag = _make_dag()
    assert dag.has_cycle() is False


def test_has_cycle_detected():
    dag = TaskDAG(session_id=1)
    dag.add_node(TaskNode(task_id=1, title="A"))
    dag.add_node(TaskNode(task_id=2, title="B", depends_on=[1]))
    # 手动制造环:2 → 1
    dag.add_edge(2, 1)
    assert dag.has_cycle() is True


def test_empty_dag():
    dag = TaskDAG(session_id=1)
    assert dag.topological_order() == []
    assert dag.parallel_layers() == []
    assert dag.ready_tasks() == []
    assert dag.has_cycle() is False
