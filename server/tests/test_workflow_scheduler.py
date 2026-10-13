"""工作流并发池（§5.4）：三级上限、0 = 不限、会话间轮询、释放幂等、排队项移除。"""

import pytest

from app.orchestration.workflow.scheduler import (
    REASON_FLOW_FULL,
    REASON_GLOBAL_FULL,
    REASON_GRANTED,
    REASON_SESSION_FULL,
    PoolLimits,
    Slot,
    WorkflowPool,
)


def _slot(node, session=1, flow="f1"):
    return Slot(node_id=node, session_id=session, flow_key=flow)


def test_global_limit_queues_and_wakes_on_release():
    pool = WorkflowPool(PoolLimits(global_limit=2, per_session_limit=0, per_flow_limit=5))
    assert pool.admit(_slot("a", 1, "f1")).granted
    assert pool.admit(_slot("b", 1, "f2")).granted
    res = pool.admit(_slot("c", 2, "f3"))
    assert not res.granted and res.reason == REASON_GLOBAL_FULL
    assert pool.block_reason("c") == REASON_GLOBAL_FULL

    assert pool.release("a") is not None
    woken = pool.pop_admissible()
    assert [s.node_id for s in woken] == ["c"]
    assert pool.block_reason("c") is None


def test_per_session_limit_only_blocks_the_same_session():
    pool = WorkflowPool(PoolLimits(global_limit=0, per_session_limit=1, per_flow_limit=5))
    assert pool.admit(_slot("a", 1, "f1")).granted
    blocked = pool.admit(_slot("b", 1, "f2"))
    assert not blocked.granted and blocked.reason == REASON_SESSION_FULL
    assert pool.admit(_slot("c", 2, "f3")).granted


def test_zero_means_unlimited_for_global_and_session():
    pool = WorkflowPool(PoolLimits(global_limit=0, per_session_limit=0, per_flow_limit=10))
    for i in range(10):
        assert pool.admit(_slot(f"n{i}", 1, "f1")).granted
    over = pool.admit(_slot("n10", 1, "f1"))
    assert not over.granted and over.reason == REASON_FLOW_FULL  # 子工作流上限仍然生效


def test_flow_limit_blocks_only_the_same_flow():
    pool = WorkflowPool(PoolLimits(global_limit=0, per_session_limit=0, per_flow_limit=2))
    assert pool.admit(_slot("a", 1, "f1")).granted
    assert pool.admit(_slot("b", 1, "f1")).granted
    res = pool.admit(_slot("c", 1, "f1"))
    assert not res.granted and res.reason == REASON_FLOW_FULL
    assert pool.admit(_slot("d", 1, "f2")).granted


def test_waiters_are_served_round_robin_across_sessions():
    pool = WorkflowPool(PoolLimits(global_limit=1, per_session_limit=0, per_flow_limit=5))
    assert pool.admit(_slot("a1", 1, "f1")).granted
    for node, sid in (("a2", 1), ("a3", 1), ("b1", 2), ("b2", 2)):
        assert not pool.admit(_slot(node, sid, f"f{sid}")).granted
    pool.set_limits(PoolLimits(global_limit=3, per_session_limit=0, per_flow_limit=5))
    woken = pool.pop_admissible()
    # 两个会话交替获得名额，而不是会话 1 的两项全部先走
    assert [s.node_id for s in woken] == ["a2", "b1"]


def test_release_is_idempotent_and_lowering_limit_keeps_running_nodes():
    pool = WorkflowPool(PoolLimits(global_limit=2, per_session_limit=0, per_flow_limit=5))
    assert pool.admit(_slot("a")).granted
    assert pool.admit(_slot("b", 2, "f2")).granted
    assert pool.release("a") is not None
    assert pool.release("a") is None  # 重复释放不报错，也不会多释放名额

    pool.set_limits(PoolLimits(global_limit=1, per_session_limit=0, per_flow_limit=5))
    assert pool.snapshot()["global"]["in_use"] == 1  # 调小不撤销运行中的节点
    assert not pool.admit(_slot("c", 3, "f3")).granted


def test_admit_is_idempotent_for_the_same_node():
    pool = WorkflowPool(PoolLimits(global_limit=1, per_session_limit=0, per_flow_limit=5))
    assert pool.admit(_slot("a")).granted
    assert pool.admit(_slot("a")).granted
    assert pool.snapshot()["global"]["in_use"] == 1


def test_drop_waiting_removes_only_matching_slots():
    pool = WorkflowPool(PoolLimits(global_limit=1, per_session_limit=0, per_flow_limit=5))
    assert pool.admit(_slot("running")).granted
    assert not pool.admit(_slot("x", 1, "run5")).granted
    assert not pool.admit(_slot("y", 1, "run6")).granted
    dropped = pool.drop_waiting(lambda s: s.flow_key == "run5")
    assert [s.node_id for s in dropped] == ["x"]
    pool.release("running")
    assert [s.node_id for s in pool.pop_admissible()] == ["y"]


def test_snapshot_reports_usage_for_the_settings_page():
    pool = WorkflowPool(PoolLimits(global_limit=3, per_session_limit=2, per_flow_limit=2))
    pool.admit(_slot("a", 1, "f1"))
    pool.admit(_slot("b", 1, "f2"))
    pool.admit(_slot("c", 1, "f3"))  # 本会话已满，排队
    snap = pool.snapshot()
    assert snap["global"] == {"in_use": 2, "limit": 3}
    assert snap["session"] == {"limit": 2, "in_use": {1: 2}}
    assert snap["queued"] == 1


def test_granted_reason_constant_is_stable():
    assert REASON_GRANTED == "granted"


def test_invalid_limits_are_rejected():
    with pytest.raises(ValueError):
        PoolLimits(per_flow_limit=0)
    with pytest.raises(ValueError):
        PoolLimits(global_limit=-1)
