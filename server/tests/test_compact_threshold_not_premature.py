# -*- coding: utf-8 -*-
"""压缩触发阈值不得被「输出预留」提前拉低（plan-598-2517）。

真实事故（会话 605 / turn=2520 / deepseek-flash，但**与供应商无关**，公式在
agent_loop 的通用路径上）：
    agent_context_window=512000，压缩阈值日志写 90%（460800），
    实际却在 step=46 的 334931（65.4%）就触发压缩——
    「未达到阈值就开始压缩」，提前了 25 个百分点。

根因（修复前的公式）：
    _out_reserve = min(settings.agent_max_output_tokens or 8192, agent_window // 4)
    _pre_threshold = max(agent_window * 0.5,
                         agent_window * ratio - _out_reserve)

两个缺陷：
1. 输出配置未设（0）时兜底 8192，但请求根本**不带 max_tokens**，这笔预留凭空存在；
2. 输出配置很大时（本机 128000），预留吃满 window//4 = 128000，
   512K 窗口下阈值 460800 - 128000 = 332800（65%），远低于配置的 90%。

本文件锁死三条约定：
1. 未配置输出上限 → 预留为 0，触发点严格等于配置阈值；
2. 配置了输出上限 → 预留不超过「阈值以上的窗口余量」，也不超过窗口 15%；
3. 任何情况下触发点都不得低于配置阈值减去窗口 15%。
"""
import pytest

from app.orchestration.agent_loop import resolve_compact_thresholds


def _pre_threshold(agent_window: int, ratio: float, max_out: int) -> tuple[int, int]:
    """直接调用生产实现，返回 (前置触发点, 输出预留)。

    过去这里复刻了一份公式，结果修了生产代码测试仍会绿（测的是副本）。
    现在只做字段取值，逻辑全归 `resolve_compact_thresholds`。
    """
    d = resolve_compact_thresholds(agent_window, ratio, max_out)
    return d["pre_threshold"], d["out_reserve"]


# ─────────────────────── 一、核心回归：不得提前压缩 ───────────────────────

def test_incident_case_no_longer_triggers_at_65_percent():
    """事故现场：512K 窗口 + 128K 输出配置，触发点必须显著高于 65%。"""
    window, ratio, max_out = 512000, 0.90, 128000
    threshold, reserve = _pre_threshold(window, ratio, max_out)

    # 修复前是 332800（65.0%）；修复后不得再回到这个值
    assert threshold > 332800, f"触发点仍然是 {threshold}，未修复"
    # 实测事故触发点 334931 必须落在阈值之下（不再触发）
    assert 334931 < threshold, \
        f"事故现场 334931 tokens 仍会触发压缩（阈值 {threshold}）"
    # 且不能补偿过度——预留仍应保留一部分输出余量
    assert reserve > 0, "配置了输出上限就应保留一定输出余量"


def test_threshold_stays_near_configured_ratio():
    """触发点与配置比例的偏离不得超过「窗口 15%」这一上限。"""
    window, ratio, max_out = 512000, 0.90, 128000
    threshold, _ = _pre_threshold(window, ratio, max_out)
    configured = int(window * ratio)
    assert configured - threshold <= int(window * 0.15), \
        f"偏离 {configured - threshold} 超过窗口 15%（{int(window * 0.15)}）"
    assert threshold / window >= 0.80, \
        f"触发点 {threshold / window:.1%} 仍明显低于配置的 90%"


# ─────────────────────── 二、未配置输出上限 → 不凭空预留 ───────────────────────

def test_no_output_limit_means_no_reserve():
    """输出配置为 0（请求不带 max_tokens）时，不得凭空预留 8192。"""
    window, ratio = 512000, 0.90
    threshold, reserve = _pre_threshold(window, ratio, 0)
    assert reserve == 0, f"未配置输出上限却预留了 {reserve}"
    assert threshold == int(window * ratio), \
        f"触发点应严格等于配置阈值 {int(window * ratio)}，实际 {threshold}"


def test_default_output_limit_would_have_made_threshold_lower():
    """对照：旧实现的 8192 兜底会把阈值压低，证明这条兜底确实有副作用。"""
    window, ratio = 512000, 0.90
    old_threshold = int(window * ratio) - min(8192, window // 4)
    new_threshold, _ = _pre_threshold(window, ratio, 0)
    assert new_threshold > old_threshold


# ─────────────────────── 三、边界与极端场景 ───────────────────────

@pytest.mark.parametrize("window,max_out", [
    (1000000, 128000),
    (200000, 128000),
    (512000, 512000),   # 输出上限 = 窗口：极端配置
    (512000, 10_000_000),  # 输出上限远大于窗口
])
def test_reserve_never_exceeds_window_fifteen_percent(window, max_out):
    """预留永远是「合理小量」，不能吃掉大块输入预算。"""
    threshold, reserve = _pre_threshold(window, 0.90, max_out)
    assert reserve <= int(window * 0.15), f"预留 {reserve} 超过窗口 15%"
    assert threshold >= int(window * 0.80), \
        f"触发点 {threshold / window:.1%} 低于窗口 80%"


def test_reserve_never_exceeds_headroom_above_threshold():
    """预留不得超过「阈值以上的窗口余量」——否则等同于二次扣减。"""
    for window, ratio, max_out in [
        (512000, 0.90, 128000),
        (1000000, 0.90, 200000),
        (300000, 0.95, 100000),
    ]:
        headroom = max(0, window - int(window * ratio))
        _, reserve = _pre_threshold(window, ratio, max_out)
        assert reserve <= headroom, \
            f"window={window} ratio={ratio} 预留 {reserve} 超过阈值以上余量 {headroom}"


def test_half_window_floor_still_applies():
    """窗口下限保护（窗口 50%）不能被绕过。"""
    threshold, _ = _pre_threshold(512000, 0.90, 0)
    assert threshold >= int(512000 * 0.5)


def test_small_window_falls_back_to_half_floor():
    """窗口远小于输出配置时，阈值下限（窗口 50%）必须生效。"""
    threshold, _ = _pre_threshold(16000, 0.90, 128000)
    assert threshold >= 8000, f"小窗口应回落到半窗下限，实际 {threshold}"
