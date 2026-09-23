"""v46 修复回归：ta3 额度「自动重置」勾选不保存 + 每日/每周重置按钮。

现场（2026-09-23）：
1. 模型设置里勾选「超出额度时自动尝试一次透支日额度」后重新进入仍是未勾选 ——
   值其实已经落盘，但 GET /settings/global 构造响应时漏了回填该字段（PUT 的
   响应同样漏了 auto_approve_outside_read），前端读到的永远是类默认值；
2. 每日/每周窗口没有重置按钮，且按钮必须"达到 100% 才可点"。

本文件固化这两处的行为契约。
"""
from __future__ import annotations

import pytest

from app.gateway.routers.settings import (
    GlobalSettingsOut,
    _global_settings_out,
)


# ── 1) 勾选值必须能如实回填（勾选丢失的直接根因）──


def test_global_settings_out_reflects_persisted_quota_switches():
    """持久化数据里的两个额度开关必须如实回填，不能被类默认值覆盖。"""
    data = {
        "auto_overdraft_on_quota_exceeded": True,
        "auto_reset_on_quota_exceeded": True,
    }
    out = _global_settings_out(data)
    assert out.auto_overdraft_on_quota_exceeded is True
    assert out.auto_reset_on_quota_exceeded is True


def test_global_settings_out_reflects_outside_read_switch():
    """v36 的 auto_approve_outside_read 此前只写不回填，同样要如实返回。"""
    out = _global_settings_out({"auto_approve_outside_read": True})
    assert out.auto_approve_outside_read is True


def test_global_settings_out_defaults_false_when_absent():
    """缺字段时回落到 False（默认关闭），不能抛错也不能变 True。"""
    out = _global_settings_out({})
    assert out.auto_overdraft_on_quota_exceeded is False
    assert out.auto_reset_on_quota_exceeded is False
    assert out.auto_approve_outside_read is False


def test_global_settings_response_exposes_both_quota_switches():
    """响应模型必须暴露这两个字段（前端按字段名读取，缺字段即表现为未勾选）。"""
    keys = GlobalSettingsOut().model_dump().keys()
    assert "auto_overdraft_on_quota_exceeded" in keys
    assert "auto_reset_on_quota_exceeded" in keys


# ── 2) 自动重置的选窗逻辑（日窗→透支，周/月窗→重置）──


@pytest.fixture()
def pick():
    from app.auth.ta3.auto_recover import _pick_exhausted_window

    return _pick_exhausted_window


def test_no_trigger_when_no_window_exhausted(pick):
    """两个窗口都未满 100% 时不触发（对应现场 89.7% / 60.9% 的截图状态）。"""
    assert pick([
        {"window": "DAILY", "percentUsed": 89.7, "canOverdraft": False},
        {"window": "WEEKLY", "percentUsed": 60.9, "canReset": False},
    ]) is None


def test_daily_exhausted_picks_daily_for_overdraft(pick):
    """日窗用尽且允许透支时选日窗（上游没有日窗重置，透支才是其恢复手段）。"""
    target = pick([
        {"window": "DAILY", "percentUsed": 104.0, "canOverdraft": True},
        {"window": "WEEKLY", "percentUsed": 50.0},
    ])
    assert target is not None and target["window"] == "DAILY"


def test_weekly_exhausted_picks_weekly_for_reset(pick):
    """日窗未满、周窗用尽 → 选周窗走重置。"""
    target = pick([
        {"window": "DAILY", "percentUsed": 30.0},
        {"window": "WEEKLY", "percentUsed": 100.0, "canReset": True},
    ])
    assert target is not None and target["window"] == "WEEKLY"


def test_falls_back_to_weekly_when_daily_cannot_overdraft(pick):
    """日窗用尽但服务端标记不可透支 → 退到周窗，而不是硬打一个必失败的透支。"""
    target = pick([
        {"window": "DAILY", "percentUsed": 120.0, "canOverdraft": False},
        {"window": "WEEKLY", "percentUsed": 110.0, "canReset": True},
    ])
    assert target is not None and target["window"] == "WEEKLY"


def test_no_action_when_server_forbids_both(pick):
    """服务端明确两个窗口都不可操作时不动（不做无意义请求）。"""
    assert pick([
        {"window": "DAILY", "percentUsed": 120.0, "canOverdraft": False},
        {"window": "WEEKLY", "percentUsed": 110.0, "canReset": False},
    ]) is None


def test_unlimited_window_never_triggers(pick):
    """percentUsed 为 null 表示该窗口不限额度，绝不能当成 0% 或已用尽。"""
    assert pick([
        {"window": "DAILY", "percentUsed": None},
        {"window": "WEEKLY", "percentUsed": None},
    ]) is None


def test_missing_can_reset_falls_back_to_exhausted(pick):
    """canReset 字段缺失时按旧口径放行（交由远端判定），不因缺字段而失效。"""
    target = pick([{"window": "WEEKLY", "percentUsed": 100.0}])
    assert target is not None and target["window"] == "WEEKLY"


# ── 3) 开关判定：新旧键任一开启即生效（旧"自动透支"开关向后兼容）──


def test_config_enabled_by_either_switch(monkeypatch):
    from app.auth.ta3 import auto_recover
    from app.core.config import settings

    monkeypatch.setattr(settings, "auto_reset_on_quota_exceeded", False, raising=False)
    monkeypatch.setattr(settings, "auto_overdraft_on_quota_exceeded", False, raising=False)
    assert auto_recover._config_enabled() is False

    monkeypatch.setattr(settings, "auto_reset_on_quota_exceeded", True, raising=False)
    assert auto_recover._config_enabled() is True

    # 旧键单独开启同样生效
    monkeypatch.setattr(settings, "auto_reset_on_quota_exceeded", False, raising=False)
    monkeypatch.setattr(settings, "auto_overdraft_on_quota_exceeded", True, raising=False)
    assert auto_recover._config_enabled() is True
