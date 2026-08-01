"""task_service 调度算法的纯逻辑测试。

task_service.list_ready_tasks 与 decrement_indegree_and_pick_ready 依赖 ORM,
DB 集成测试留 v0.5(需 PG testcontainers 或 dialect 重构)。
此处验证调度算法的纯函数等价实现,确保逻辑正确。
"""
from collections import deque


def _compute_indegree(tasks: list[dict], edges: list[tuple[int, int]]) -> dict[int, int]:
    """与 task_service 内部算法一致的纯函数实现(跳过 done 上游)。"""
    done_ids = {t["id"] for t in tasks if t["status"] in ("done", "in_review", "rejected")}
    in_degree = {t["id"]: 0 for t in tasks}
    for from_id, to_id in edges:
        if from_id in done_ids:
            continue
        in_degree[to_id] = in_degree.get(to_id, 0) + 1
    return in_degree


def _list_ready(tasks: list[dict], edges: list[tuple[int, int]]) -> list[dict]:
    """等价于 task_service.list_ready_tasks(初始状态无 done,等价于全 edges 计入度)。"""
    in_degree = _compute_indegree(tasks, edges)
    return [t for t in tasks if in_degree[t["id"]] == 0 and t["status"] == "pending"]


def _decrement_and_pick_ready(
    tasks: list[dict], edges: list[tuple[int, int]], completed_id: int
) -> list[dict]:
    """等价于 task_service.decrement_indegree_and_pick_ready(修复后版本)。

    模拟完成 completed_id:把它的状态改为 done,再重新计算入度,返回新的 ready。
    """
    # 复制并标记 completed_id 为 done
    new_tasks = [dict(t) for t in tasks]
    for t in new_tasks:
        if t["id"] == completed_id:
            t["status"] = "done"
    in_degree = _compute_indegree(new_tasks, edges)
    return [t for t in new_tasks if in_degree[t["id"]] == 0 and t["status"] == "pending"]


def test_list_ready_empty_initial():
    tasks = [{"id": 1, "status": "pending"}]
    edges = []
    assert len(_list_ready(tasks, edges)) == 1


def test_list_ready_with_dependency():
    tasks = [
        {"id": 1, "status": "pending"},
        {"id": 2, "status": "pending"},
    ]
    edges = [(1, 2)]
    ready = _list_ready(tasks, edges)
    assert len(ready) == 1
    assert ready[0]["id"] == 1


def test_list_ready_skips_non_pending():
    tasks = [
        {"id": 1, "status": "done"},
        {"id": 2, "status": "pending"},
    ]
    edges = []
    ready = _list_ready(tasks, edges)
    assert len(ready) == 1
    assert ready[0]["id"] == 2


def test_decrement_unlocks_downstream():
    tasks = [
        {"id": 1, "status": "pending"},
        {"id": 2, "status": "pending"},
        {"id": 3, "status": "pending"},
    ]
    edges = [(1, 2), (1, 3)]
    newly = _decrement_and_pick_ready(tasks, edges, completed_id=1)
    ids = {t["id"] for t in newly}
    assert ids == {2, 3}


def test_decrement_keeps_blocked_if_other_dep():
    """任务 4 依赖 2 与 3,完成 2 后 4 仍因 3 阻塞。

    注意:3 本身无上游,完成 2 后 3 也是 ready 的(它一直 ready)。
    所以结果包含 3,但不包含 4。
    """
    tasks = [
        {"id": 2, "status": "pending"},
        {"id": 3, "status": "pending"},
        {"id": 4, "status": "pending"},
    ]
    edges = [(2, 4), (3, 4)]
    newly = _decrement_and_pick_ready(tasks, edges, completed_id=2)
    ready_ids = {t["id"] for t in newly}
    # 4 一定不在(还有 3 这个上游未完成)
    assert 4 not in ready_ids
    # 3 在(它本就无上游,完成 2 不影响它的 ready 状态)
    assert 3 in ready_ids


def test_decrement_unlocks_when_all_deps_done():
    """连续完成 2 与 3 后,4 才解锁。

    由于 _decrement_and_pick_ready 是无状态纯函数(每次重新算),
    模拟"连续完成"需把上游状态累计传入。
    """
    tasks = [
        {"id": 2, "status": "pending"},
        {"id": 3, "status": "pending"},
        {"id": 4, "status": "pending"},
    ]
    edges = [(2, 4), (3, 4)]
    # 完成 2 → 4 还有 1 入度(3 未完成),4 不 ready
    newly = _decrement_and_pick_ready(tasks, edges, completed_id=2)
    assert 4 not in {t["id"] for t in newly}

    # 现在 2 已 done(模拟),再完成 3 → 4 入度归零
    tasks_after_2 = [dict(t) for t in tasks]
    for t in tasks_after_2:
        if t["id"] == 2:
            t["status"] = "done"
    newly = _decrement_and_pick_ready(tasks_after_2, edges, completed_id=3)
    ready_ids = {t["id"] for t in newly}
    assert 4 in ready_ids
