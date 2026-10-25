"""建议卡的模式门禁（plan-562-2318 A26）。

背景：`suggestion_allowed` 原先直接拿**原始模式名**比白名单（`SUGGESTION_MODES`），
自定义模式名永远不相等，于是自定义 `kind=full`（能力等同智能体）的主会话也拿不到
`suggest_next_task` 工具与提示词——与本仓其他判定（审批边界、子代理能力矩阵、
派发提示词、ta3 流程规范）已统一按 `kind` 归一的口径不一致。

本文件钉住归一后的语义边界，重点是两条容易被"顺手放开"的对照：
  · 自定义 `kind=full` → **允许**（本次修复的核心新行为）；
  · 自定义 `kind=readonly` / `kind=plan` → **仍拒绝**（不能因为归一就把只读档放开）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# 让测试能直接 import app.*（与同目录其它用例一致）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402
from app.services import permission_profile_service as pps  # noqa: E402


@pytest.fixture
def profiles(tmp_path, monkeypatch) -> Path:
    """把模式配置指向临时文件，并清掉判定档缓存（否则上一条用例的结果会串过来）。"""
    cfg = tmp_path / "config.json"
    cfg.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(pps, "_CFG_PATH", cfg)
    pps._clear_mode_kind_cache()
    yield cfg
    pps._clear_mode_kind_cache()


@pytest.fixture
def suggestion_on(monkeypatch):
    """打开建议卡总开关（默认关闭时任何模式都不注入，会掩盖模式判定的差异）。"""
    monkeypatch.setattr(settings, "suggestion_enabled", True, raising=False)


def _write_profiles(cfg: Path, *entries: dict) -> None:
    cfg.write_text(
        json.dumps({"permission_profiles": list(entries)}, ensure_ascii=False),
        encoding="utf-8",
    )
    pps._clear_mode_kind_cache()


class _Session:
    """最小会话替身：只带门禁会用到的字段。"""

    def __init__(self, side_parent_id: int | None = None) -> None:
        self.side_parent_id = side_parent_id


def _allowed(mode: str, session: _Session | None = None) -> bool:
    from app.orchestration.tools.suggestion import suggestion_allowed

    return suggestion_allowed(mode, session or _Session())


# ── 内置模式：行为必须与修复前一致 ─────────────────────────────────────────────

@pytest.mark.parametrize("mode", ["agent", "workflow"])
def test_builtin_agent_and_workflow_are_allowed(profiles, suggestion_on, mode):
    """内置智能体 / 工作流档照常注入（回归基线）。"""
    assert _allowed(mode) is True


@pytest.mark.parametrize("mode", ["readonly", "plan"])
def test_builtin_readonly_and_plan_are_rejected(profiles, suggestion_on, mode):
    """内置只读 / 计划档不注入（回归基线）。"""
    assert _allowed(mode) is False


# ── 自定义模式：按 kind 归一 ────────────────────────────────────────────────────

def test_custom_full_kind_is_allowed(profiles, suggestion_on):
    """核心新行为：自定义 kind=full 等价智能体档，应能拿到建议卡。"""
    _write_profiles(profiles, {
        "name": "my_full", "kind": "full", "description": "", "tools": [], "hint": "",
    })
    assert pps.resolve_mode_kind("my_full") == pps.MODE_KIND_AGENT
    assert _allowed("my_full") is True


@pytest.mark.parametrize("kind", ["readonly", "plan"])
def test_custom_readonly_or_plan_kind_is_rejected(profiles, suggestion_on, kind):
    """对照组：自定义只读 / 计划档仍拒绝，避免归一后退化成"自定义就等于放行"。"""
    _write_profiles(profiles, {
        "name": f"my_{kind}", "kind": kind, "description": "", "tools": [], "hint": "",
    })
    assert _allowed(f"my_{kind}") is False


def test_unknown_mode_falls_back_to_agent(profiles, suggestion_on):
    """未登记的模式名归一为 agent（与既有兜底一致），因此允许。"""
    assert _allowed("never_registered_mode") is True


# ── 与模式无关的两个前置条件，必须优先于模式判定 ────────────────────────────────

def test_disabled_switch_wins_over_mode(profiles, monkeypatch):
    """总开关关闭时，即便是智能体档也不注入。"""
    monkeypatch.setattr(settings, "suggestion_enabled", False, raising=False)
    assert _allowed("agent") is False


def test_side_session_is_never_allowed(profiles, suggestion_on):
    """挂靠子会话（side_parent_id 非空）不注入建议卡，与模式无关。"""
    assert _allowed("agent", _Session(side_parent_id=7)) is False
    assert _allowed("workflow", _Session(side_parent_id=7)) is False


# ── 归一异常时的兜底方向 ───────────────────────────────────────────────────────

def test_normalize_failure_falls_back_to_raw_name(profiles, suggestion_on, monkeypatch):
    """配置层异常时必须按原名判定，不能一律放行——否则坏配置会把建议卡注进只读会话。"""
    def _boom(_mode=None):
        raise RuntimeError("config broken")

    monkeypatch.setattr(pps, "resolve_mode_kind", _boom)
    assert _allowed("readonly") is False   # 原名判定仍拦住只读
    assert _allowed("agent") is True       # 原名判定仍放行智能体
