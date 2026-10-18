"""执行模式 → 子代理 / 子会话能力矩阵（plan-156-619 S4）。

【为什么单独一层】
改造前这条边界散在两处、口径还互相矛盾：
  · `engine.py` 注释写「四种权限模式都支持子代理」，于是只读模式也下发子代理工具；
  · `permission_profile_service._SUBAGENT` 把 6 个子代理工具塞进 READONLY_TOOLS 白名单。
用户要的口径是按模式收紧：

  ┌──────────┬──────────────┬──────────┐
  │ 执行模式 │ 探索子代理   │ 子会话   │
  ├──────────┼──────────────┼──────────┤
  │ readonly │ 不下发       │ 不下发   │
  │ plan     │ 下发（只读） │ 不下发   │
  │ agent    │ 下发（只读） │ 下发     │
  │ workflow │ 下发（只读） │ 不下发   │
  └──────────┴──────────────┴──────────┘

  语义来源：
  · 只读模式不产生任何派发行为（连探索也不建，避免"审阅时偷偷开子代理"）；
  · 计划 / 智能体模式在**探索阶段**用只读子代理（右侧面板展示，不建子会话）；
  · 执行阶段只有智能体模式建子会话；工作流模式的执行统一走工作流节点，
    主代理不再自己建子会话（否则同一份工作会被两套机制重复派发）；
  · 只读子代理在计划 / 智能体 / 工作流三种模式下都可用——探索不属于执行。

自定义模式（plan-230-1144 M2 允许任意模式名）按 `kind` 归入四档，
避免自定义模式变成"什么都能用"的无边界状态。
"""
from __future__ import annotations

from app.orchestration.approval_policy import (
    MODE_AGENT,
    MODE_PLAN,
    MODE_READONLY,
    MODE_WORKFLOW,
    normalize_execution_mode,
)

# ── 能力标识（字符串常量，避免各处写裸字面量）──
CAP_EXPLORE_AGENT = "explore_agent"
CAP_SUBSESSION = "subsession"

# ── 内置矩阵：唯一事实源 ──
CAPABILITIES: dict[str, frozenset[str]] = {
    MODE_READONLY: frozenset(),
    MODE_PLAN: frozenset({CAP_EXPLORE_AGENT}),
    MODE_AGENT: frozenset({CAP_EXPLORE_AGENT, CAP_SUBSESSION}),
    MODE_WORKFLOW: frozenset({CAP_EXPLORE_AGENT}),
}

# ── 自定义模式按 kind 归一（kind 由 permission_profile_service 声明）──
_KIND_TO_MODE: dict[str, str] = {
    "readonly": MODE_READONLY,
    "plan": MODE_PLAN,
    "full": MODE_AGENT,
}
# 无法判定 kind 时的保守落点：只给只读探索，不给子会话（宁可少派发，不可越权）
_UNKNOWN_KIND_MODE = MODE_PLAN


def _mode_for_custom(profile_name: str) -> str | None:
    """自定义模式名 → 内置等价模式；非自定义模式返回 None。"""
    try:
        from app.services.permission_profile_service import get_profile
    except Exception:  # noqa: BLE001 — 配置层不可用时按未知处理，不阻断 turn
        return _UNKNOWN_KIND_MODE
    profile = get_profile(profile_name)
    if profile is None:
        return _UNKNOWN_KIND_MODE
    if profile.get("builtin"):
        # 内置模式已在 CAPABILITIES 里，无需归一
        return None
    kind = str(profile.get("kind") or "")
    return _KIND_TO_MODE.get(kind, _UNKNOWN_KIND_MODE)


def effective_mode(mode: str | None) -> str:
    """把任意模式名（含自定义）归一为 CAPABILITIES 的四个键之一。"""
    name = normalize_execution_mode(mode)
    if name in CAPABILITIES:
        return name
    mapped = _mode_for_custom(name)
    return mapped if mapped in CAPABILITIES else MODE_PLAN


def capabilities_for(mode: str | None) -> frozenset[str]:
    """当前执行模式允许的子代理 / 子会话能力集合。"""
    return CAPABILITIES[effective_mode(mode)]


def allows_explore_agent(mode: str | None) -> bool:
    """是否应下发「只读探索子代理」工具组。"""
    return CAP_EXPLORE_AGENT in capabilities_for(mode)


def allows_subsession(mode: str | None) -> bool:
    """是否应下发「执行子会话」工具组。"""
    return CAP_SUBSESSION in capabilities_for(mode)
