"""BudgetTracker 单测(v0.3 重构后的发言权调度器)。"""
from app.orchestration.turn_scheduler import BudgetTracker, get_scheduler


def test_initial_state():
    bt = BudgetTracker(session_id=1)
    assert bt.frozen is False
    assert bt.total_tokens == 0


def test_consume_within_budget():
    bt = BudgetTracker(session_id=1, token_budget=1000)
    assert bt.consume_tokens(300) is True
    assert bt.consume_tokens(500) is True
    assert bt.total_tokens == 800
    assert bt.frozen is False


def test_consume_exact_threshold_freezes():
    bt = BudgetTracker(session_id=1, token_budget=1000)
    assert bt.consume_tokens(1000) is False  # 达到阈值
    assert bt.frozen is True
    assert bt.total_tokens == 1000


def test_consume_over_budget_freezes():
    bt = BudgetTracker(session_id=1, token_budget=1000)
    assert bt.consume_tokens(1500) is False
    assert bt.frozen is True
    assert bt.total_tokens == 1500  # 仍累计


def test_consume_after_freeze_always_false():
    bt = BudgetTracker(session_id=1, token_budget=100)
    bt.consume_tokens(200)
    assert bt.frozen is True
    # 熔断后任何 consume 都返 False
    assert bt.consume_tokens(10) is False


def test_get_scheduler_singleton_per_session():
    s1 = get_scheduler(100)
    s2 = get_scheduler(100)
    s3 = get_scheduler(200)
    assert s1 is s2  # 同 session 返同实例
    assert s1 is not s3  # 不同 session 不同实例
    assert s1.session_id == 100
