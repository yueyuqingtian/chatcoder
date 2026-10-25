"""执行模式 → 子代理能力矩阵（plan-156-619 S4，plan-549-2281 收口，plan-560-2311 C1/C2）。

【为什么单独一层】
改造前这条边界散在两处、口径还互相矛盾：
  · `engine.py` 注释写「四种权限模式都支持子代理」，于是只读模式也下发子代理工具；
  · `permission_profile_service._SUBAGENT` 把 6 个子代理工具塞进 READONLY_TOOLS 白名单。
用户要的口径是按模式收紧：

  ┌──────────┬──────────────────┐
  │ 执行模式 │ 子代理           │
  ├──────────┼──────────────────┤
  │ readonly │ 下发（强制只读） │
  │ plan     │ 下发（强制只读） │
  │ agent    │ 下发（可选可写） │
  │ workflow │ 下发（可选可写） │
  └──────────┴──────────────────┘

  语义来源（plan-549-2281 与提示词 / 测试同口径）：
  · 探索不属于执行——四个模式都可派发只读探索子代理，readonly / plan 在运行时
    强制 explore（agent_loop 的模式语义护栏），提示词相应注入受限版派发引导；
  · 可写子代理（mode="agent"）在 agent 与 workflow 模式会话里都生效——plan-560-2311 C2
    按用户决策确立「workflow 等同 agent」：它的 kind 本就是 full，工具面与审批边界
    与 agent 完全相同，早先「强制只读 explore」的声明与实现不符，已删除。
    工作流节点自身的写入仍受 `write_scope` 约束（`workflow/guard.py`），那是另一层。

plan-549-2281：执行子会话（subsession）已整体下线，并行执行统一由子代理承担，
因此 CAP_SUBSESSION 与 allows_subsession() 已删除；本矩阵只剩「子代理」一列
（原先 agent 模式额外拥有「执行子会话」能力，现由子代理的可写 mode 承担）。

plan-560-2311 C1：自定义模式（plan-230-1144 M2 允许任意模式名）的归一口径统一委托
`permission_profile_service.resolve_mode_kind`——本模块不再自带 `_KIND_TO_MODE` 副本。
此前审批层、运行时层各写一份（或干脆不归一），自定义 kind=readonly/plan 模式因此
绕过了硬边界与 spawn 护栏。
"""
from __future__ import annotations

from app.orchestration.approval_policy import (
    MODE_AGENT,
    MODE_PLAN,
    MODE_READONLY,
    MODE_WORKFLOW,
)

# ── 能力标识（字符串常量，避免各处写裸字面量）──
CAP_EXPLORE_AGENT = "explore_agent"

# ── 内置矩阵：唯一事实源 ──
# 【注意】本矩阵只表达「能否给主代理下发子代理工具组」，**不**含只读 / 可写的执行语义。
# CAP_EXPLORE_AGENT 在四个模式里取值完全相同，正是这一点的体现：四个模式都下发，
# 至于子代理可写与否，由 spawn_subagent 的 mode 参数 + 运行时模式护栏决定
# （见 agent_loop.resolve_subagent_readonly 与 spawn 分支的 operator 拦截）。
# 历史上这里被误读成「workflow 强制只读」，进而写出与实现不符的注释（plan-560-2311 C2）——
# 新增模式时请勿再往这一列塞执行语义。
CAPABILITIES: dict[str, frozenset[str]] = {
    MODE_READONLY: frozenset({CAP_EXPLORE_AGENT}),
    MODE_PLAN: frozenset({CAP_EXPLORE_AGENT}),
    MODE_AGENT: frozenset({CAP_EXPLORE_AGENT}),
    MODE_WORKFLOW: frozenset({CAP_EXPLORE_AGENT}),
}


def effective_mode(mode: str | None) -> str:
    """把任意模式名（含自定义）归一为 CAPABILITIES 的四个键之一。

    plan-560-2311 C1：委托 `permission_profile_service.resolve_mode_kind`（模式归一的
    单一事实源），本模块不再自带映射副本——两份实现漂移正是自定义模式绕过边界的成因。
    返回值固定落在四档之内，可直接当 CAPABILITIES 的键。
    """
    from app.services.permission_profile_service import resolve_mode_kind

    kind = resolve_mode_kind(mode)
    return kind if kind in CAPABILITIES else MODE_AGENT


def capabilities_for(mode: str | None) -> frozenset[str]:
    """当前执行模式允许的子代理 / 子会话能力集合。"""
    return CAPABILITIES[effective_mode(mode)]


def allows_explore_agent(mode: str | None) -> bool:
    """是否应下发「只读探索子代理」工具组。"""
    return CAP_EXPLORE_AGENT in capabilities_for(mode)
