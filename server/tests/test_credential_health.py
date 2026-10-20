"""plan-20261020-retry-cred-perf §二 单测：凭据健康状态与取用排序（纯逻辑，不连库）。

口径（与用户确认）：
- 粘性 = 最近一次成功的优先（不是「成功过」的布尔值）；
- 优先级 = 严格按 (priority, id)，失败者进入指数退避暂缓窗口，窗口内排到队尾；
- 暂缓是软降级：绝不从列表剔除，其余都不可用时回头仍会试到它；
- 单凭据永不受影响；ta3 仍走旧的 available_credentials（冷却硬过滤）。
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.services import credential_service as cs


class _Cred:
    """轻量凭据替身（只覆盖排序/健康判定所需字段）。"""

    def __init__(self, cid, priority=0, is_active=True, status="ok",
                 last_ok_at=None, cooldown_until=None, fail_count=0, last_error=None):
        self.id = cid
        self.priority = priority
        self.is_active = is_active
        self.status = status
        self.last_ok_at = last_ok_at
        self.cooldown_until = cooldown_until
        self.fail_count = fail_count
        self.last_error = last_error


@pytest.fixture(autouse=True)
def _clean_state():
    """进程内状态是模块级的，用例之间必须隔离。"""
    cs._state.clear()
    cs._pending_flush.clear()
    yield
    cs._state.clear()
    cs._pending_flush.clear()


def _now():
    return datetime.now(timezone.utc)


def test_demote_window_backs_off_exponentially():
    assert cs.demote_window_seconds(1) == 30
    assert cs.demote_window_seconds(2) == 60
    assert cs.demote_window_seconds(3) == 120
    assert cs.demote_window_seconds(6) == 960
    # 上限 30 分钟：再多失败也不继续翻倍
    assert cs.demote_window_seconds(20) == cs.DEMOTE_MAX_SECONDS


def test_demote_window_uses_quota_reset_time_with_cap():
    now = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
    assert cs.demote_window_seconds(2, reset_at=now + timedelta(hours=2), now=now) == 7200
    # 额度重置时间过远时仍夹到 6 小时
    assert cs.demote_window_seconds(
        2, reset_at=now + timedelta(days=3), now=now) == cs.DEMOTE_CAP_SECONDS
    assert cs.demote_window_seconds(2, reset_at=now - timedelta(seconds=5), now=now) == 0


def test_sticky_prefers_most_recent_success():
    a, b = _Cred(1, priority=0), _Cred(2, priority=9)
    now = _now()
    # 2 号优先级更低，但它是最近一次成功的 → 粘性模式应排第一
    cs._state[1] = cs._Health(last_ok_at=now - timedelta(minutes=5))
    cs._state[2] = cs._Health(last_ok_at=now)
    assert [c.id for c in cs.rank_credentials([a, b], "sticky")] == [2, 1]


def test_sticky_never_succeeded_sorted_by_priority():
    a, b = _Cred(1, priority=2), _Cred(2, priority=1)
    assert [c.id for c in cs.rank_credentials([a, b], "sticky")] == [2, 1]


def test_priority_is_strict_and_does_not_rotate():
    a, b = _Cred(1, priority=0), _Cred(2, priority=1)
    # 连续多次取用顺序不变（旧实现会推进轮转游标）
    for _ in range(3):
        assert [c.id for c in cs.rank_credentials([a, b], "round_robin")] == [1, 2]


def test_failure_demotes_to_tail_but_is_never_excluded():
    a, b = _Cred(1, priority=0), _Cred(2, priority=1)
    cs.report_failure(1, "429 rate limit")
    assert cs.is_demoted(a)
    assert [c.id for c in cs.rank_credentials([a, b], "round_robin")] == [2, 1]
    # 单凭据：仍然留在列表里（其余都不可用时能回头试它）
    assert [c.id for c in cs.rank_credentials([a], "round_robin")] == [1]


def test_demoted_sorted_by_earliest_expiry():
    a, b = _Cred(1, priority=0), _Cred(2, priority=1)
    now = _now()
    # 1 号窗口更早到期 → 排在 2 号暂缓者前面
    cs._state[1] = cs._Health(fail_count=1, demoted_until=now + timedelta(seconds=30))
    cs._state[2] = cs._Health(fail_count=2, demoted_until=now + timedelta(seconds=60))
    assert [c.id for c in cs.rank_credentials([a, b], "round_robin")] == [1, 2]


def test_success_clears_demotion_and_restores_order():
    a, b = _Cred(1, priority=0), _Cred(2, priority=1)
    cs.report_failure(1, "503 upstream")
    cs.report_success(1)
    assert not cs.is_demoted(a)
    assert [c.id for c in cs.rank_credentials([a, b], "round_robin")] == [1, 2]
    assert cs._state[1].fail_count == 0


def test_client_errors_do_not_count_toward_health():
    a = _Cred(1)
    cs.report_failure(1, "context length exceeded", counted=False)
    assert not cs.is_demoted(a)
    assert cs._state[1].fail_count == 0
    # 仍留下错误痕迹，便于界面排查
    assert "context length" in (cs._state[1].last_error or "")


def test_demote_window_expires_automatically():
    a, b = _Cred(1, priority=0), _Cred(2, priority=1)
    now = _now()
    cs._state[1] = cs._Health(fail_count=1, demoted_until=now - timedelta(seconds=1))
    assert not cs.is_demoted(a, now)
    assert [c.id for c in cs.rank_credentials([a, b], "round_robin", now=now)] == [1, 2]


def test_cooldown_from_db_restored_as_demoted():
    future = (_now() + timedelta(minutes=2)).isoformat()
    a = _Cred(1, priority=0, status="cooldown", cooldown_until=future)
    b = _Cred(2, priority=1)
    assert [c.id for c in cs.rank_credentials([a, b], "round_robin")] == [2, 1]


def test_inactive_credentials_are_excluded():
    creds = [_Cred(1, is_active=False), _Cred(2, priority=1)]
    assert [c.id for c in cs.rank_credentials(creds, "round_robin")] == [2]


def test_legacy_path_still_filters_cooldown_for_ta3():
    """ta3 走 available_credentials：冷却仍是硬过滤，既有行为不变。"""
    future = (_now() + timedelta(minutes=5)).isoformat()
    creds = [
        _Cred(1, priority=0, status="cooldown", cooldown_until=future),
        _Cred(2, priority=1),
    ]
    assert [c.id for c in cs.available_credentials(creds)] == [2]


def test_forget_drops_in_process_state():
    a, b = _Cred(1, priority=0), _Cred(2, priority=1)
    cs.report_failure(1, "503")
    assert cs.is_demoted(a)
    cs.forget(1)
    # 状态清掉后回到库里的默认值（该替身 status=ok）→ 重新参与正常排序
    assert not cs.is_demoted(a)
    assert [c.id for c in cs.rank_credentials([a, b], "round_robin")] == [1, 2]
