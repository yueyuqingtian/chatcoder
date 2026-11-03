# -*- coding: utf-8 -*-
"""压缩阈值只由「已使用 / 总窗口」决定（plan-599-2520）。

真实事故（本机 450K 窗口 + 单次最大输出 128K + 设置 90%）：
    界面圆环显示已用 344K（76%），却弹出「正在整理上下文 76%」开始压缩。
    日志：`压缩配置阈值=90% (405000)，前置触发点=360000 (80.0%，输出预留 45000)`
    ——阈值被「输出预留」提前拉到 80%，叠加估算偏高（calib=1.246 把 296K 估成 369K），
    76% 就已经越过 80% 的触发点。

更早一版公式的同类问题：
    _out_reserve = min(settings.agent_max_output_tokens or 8192, agent_window // 4)
    _pre_threshold = max(agent_window * 0.5, agent_window * ratio - _out_reserve)
    512K 窗口 + 128K 输出下触发点被压到 332800（65%）。

本文件锁死三条约定：
1. 阈值严格等于「总窗口 × 设置百分比」，单次最大输出不参与任何计算；
2. 前置与后置（真实占用）触发点完全一致，不允许再出现两个口径；
3. 「已使用」判断有真实 prompt_tokens 时以它为基数、只补新增，不被估算系数放大。
"""
import pytest

from app.orchestration.agent_loop import resolve_compact_thresholds, resolve_used_tokens


# ─────────────────────── 一、阈值 = 窗口 × 设置百分比 ───────────────────────

def test_incident_case_threshold_equals_configured_ratio():
    """事故现场：450K 窗口 + 90%，阈值必须是 405000（不是 360000）。"""
    d = resolve_compact_thresholds(450000, 0.90)
    assert d["configured"] == 405000
    assert d["pre_threshold"] == 405000, "前置触发点不得低于配置阈值"
    assert d["post_threshold"] == 405000


def test_ui_76_percent_no_longer_triggers():
    """界面显示 76%（344K/450K）时不得触发压缩。"""
    threshold = resolve_compact_thresholds(450000, 0.90)["pre_threshold"]
    assert 344000 < threshold


def test_output_limit_does_not_affect_threshold():
    """阈值函数不再接受单次最大输出参数——传了也不该改变结果（防回归）。"""
    base = resolve_compact_thresholds(512000, 0.90)
    assert base["configured"] == 460800
    # 现场旧的 65% / 80% 两个错误触发点都必须远低于现在的阈值
    assert base["pre_threshold"] > 332800, "不得回到按 window//4 预留的旧口径"
    assert base["pre_threshold"] > 360000, "不得回到按输出上限预留的旧口径"


@pytest.mark.parametrize("window,ratio", [
    (200000, 0.50),
    (200000, 0.90),
    (450000, 0.75),
    (512000, 0.90),
    (1000000, 0.95),
])
def test_pre_and_post_threshold_identical(window, ratio):
    """前置与后置（真实占用）触发点必须完全一致。"""
    d = resolve_compact_thresholds(window, ratio)
    assert d["pre_threshold"] == d["post_threshold"] == d["configured"]


def test_threshold_is_exact_ratio_of_window():
    """阈值与设置比例的偏离为 0（只允许整数截断误差）。"""
    window, ratio = 450000, 0.90
    threshold = resolve_compact_thresholds(window, ratio)["pre_threshold"]
    assert abs(threshold / window - ratio) < 1e-9


def test_degenerate_inputs_are_safe():
    """窗口 0 / ratio 非法时不得抛异常，也不能给出负阈值。"""
    assert resolve_compact_thresholds(0, 0.90)["pre_threshold"] == 0
    assert resolve_compact_thresholds(450000, 0)["pre_threshold"] == 0
    assert resolve_compact_thresholds(450000, -0.5)["pre_threshold"] == 0


# ─────────────────────── 二、「已使用」口径 ───────────────────────

def test_used_tokens_prefers_real_prompt_plus_delta():
    """有真实 prompt_tokens 时，以它为基数只补新增内容（不被系数放大）。"""
    used = resolve_used_tokens(
        est_raw=296235, calib=1.246, last_est_raw=290000, last_real_prompt=344000,
    )
    # 新增约 6235 × 1.246 ≈ 7769，而不是 296235 × 1.246 ≈ 369060
    assert 344000 <= used <= 344000 + 9000
    assert used < 360000, "前置判断不得再把 76% 估成 80% 以上"


def test_used_tokens_falls_back_to_full_estimate_without_real():
    """没有真实值时退回「全量估算 × 校准系数」。"""
    assert resolve_used_tokens(est_raw=1000, calib=1.2) == 1200


def test_used_tokens_ignores_stale_base_after_compact():
    """压缩后基数归零时不得继续用旧真实值（否则刚压缩就再次触发）。"""
    used = resolve_used_tokens(
        est_raw=50000, calib=1.0, last_est_raw=0, last_real_prompt=0,
    )
    assert used == 50000


def test_used_tokens_never_negative_when_shrunk():
    """上下文变小（压缩/裁剪）后不得算出负数。"""
    used = resolve_used_tokens(
        est_raw=10000, calib=1.5, last_est_raw=200000, last_real_prompt=300000,
    )
    assert used >= 0
