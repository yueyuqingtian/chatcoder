"""子代理执行模式与工具边界的守护测试（plan-549-2281）。

背景：子会话下线后 `spawn_subagent` 成为唯一执行体，它的能力边界从「只读探索」升级为
「readonly / agent 两种执行模式」。本文件锁死三条不可回退的约定：

1. `mode` 参数存在且只有两个取值，`explore` 仅作为兼容别名；
2. 子代理永远拿不到工作流工具、子代理工具、子会话工具、`ask_user_question`；
3. 主代理处于只读 / 计划模式时，子代理被强制降为只读。

第 3 条是运行时行为；plan-549-2281 复盘修复后 spawn 分支改调
`agent_loop.resolve_subagent_readonly` 纯函数，测试直接测它（不再手工复刻等价实现），
前两条仍以提示词与 schema 的静态断言覆盖。
"""
from __future__ import annotations

import pytest

from app.orchestration.subagent_tools import (
    SPAWN_SUBAGENT_SCHEMA,
    SUBAGENT_FORBIDDEN,
    SUBAGENT_OWN_TOOLS,
    SUBAGENT_TOOL_NAMES,
    strip_for_subagent,
)


# ── 1. mode 参数：两个取值、explore 是兼容别名 ──
def test_spawn_schema_exposes_mode_with_two_values():
    props = SPAWN_SUBAGENT_SCHEMA["function"]["parameters"]["properties"]
    assert "mode" in props, "plan-549-2281：spawn_subagent 必须暴露 mode 执行模式"
    assert props["mode"]["enum"] == ["readonly", "agent"]
    # explore 保留为兼容别名（旧会话历史与 ta3 侧 subagent_type 还原都还会产出它）
    assert "explore" in props
    # 描述必须说清「agent 可写」，否则又会回到「实现永不委派」的老矛盾
    desc = SPAWN_SUBAGENT_SCHEMA["function"]["description"]
    assert "mode" in desc
    assert "agent" in desc
    assert "read/write" in desc.lower() or "may write" in desc.lower()


def test_spawn_schema_description_allows_delegating_implementation():
    """旧提示词「implementation is never delegated」是本轮修复的根因，不得回归。"""
    desc = SPAWN_SUBAGENT_SCHEMA["function"]["description"]
    assert "implementation is never delegated" not in desc
    assert "Implementation work" in desc or "implementation work" in desc


# ── 2. 工具边界：工作流 / 子代理 / 子会话 / 直接问用户 一律禁止 ──
_FORBIDDEN_MUST_HAVE = {
    # 子代理不可再派生下级
    "spawn_subagent", "collect_results", "subagent_inspect",
    "cancel_subagent", "send_to_subagent", "resume_subagent",
    # 子代理不可直接打扰用户
    "ask_user_question",
    # 子会话遗留工具名（即使模块已删，白名单里塞回来也必须被剥掉）
    "create_subsession", "send_to_subsession", "get_subsession_progress",
    "interrupt_subsession", "message_main", "message_peer",
    # 工作流工具（plan-549-2281 新增；子代理永远不进工作流）
    "plan_workflow", "start_workflow", "workflow_status", "resume_workflow",
    "cancel_workflow", "amend_workflow", "decide_workflow", "request_write_scope",
}


@pytest.mark.parametrize("name", sorted(_FORBIDDEN_MUST_HAVE))
def test_subagent_cannot_see_forbidden_tool(name):
    assert name in SUBAGENT_FORBIDDEN, f"{name} 必须出现在子代理禁止集合里"


def test_strip_for_subagent_removes_workflow_tools_injected_by_whitelist():
    """白名单 / 自定义权限模式可能把工具名塞回来，strip 必须兜住。"""
    schemas = [
        {"type": "function", "function": {"name": n}}
        for n in ("fs_read", "fs_write", "plan_workflow", "start_workflow",
                  "spawn_subagent", "ask_user_question", "create_subsession")
    ]
    kept = {s["function"]["name"] for s in strip_for_subagent(schemas)}
    assert kept == {"fs_read", "fs_write"}


def test_report_to_leader_is_the_only_own_tool():
    """子代理自查通道只有 report_to_leader：它是「向上抛」的唯一入口。"""
    names = {s["function"]["name"] for s in SUBAGENT_OWN_TOOLS}
    assert names == {"report_to_leader"}


def test_subagent_tool_names_cover_schemas_and_own_tools():
    names = set(SUBAGENT_TOOL_NAMES)
    assert "spawn_subagent" in names and "report_to_leader" in names
    # 注意：SUBAGENT_TOOL_NAMES 是「主代理侧可下发的子代理工具」，与 SUBAGENT_FORBIDDEN
    # （「子代理自身不得拥有」）本就是同一批名字的两侧视图，正常重叠，不能断言不相交。
    # 真正要保证的是：剥完之后子代理一个都拿不到（见 test_strip_for_subagent_*）。
    assert "spawn_subagent" in SUBAGENT_FORBIDDEN
    # report_to_leader 是子代理唯一保留的自有通道，绝不能被列入禁止集合
    assert "report_to_leader" not in SUBAGENT_FORBIDDEN


# ── 3. mode 解析：直接测 agent_loop 的真实函数（plan-549-2281 复盘修复） ──
# 此前这里是手工复刻的等价实现——实现漂移时测试照样绿；现在 spawn 分支改调
# resolve_subagent_readonly 纯函数，测试与实现共用同一份代码。
from app.orchestration.agent_loop import resolve_subagent_readonly as _resolve_readonly


def test_mode_readonly_wins_over_explore_false():
    assert _resolve_readonly("readonly", False, "agent") is True


def test_mode_agent_overrides_explore_true():
    assert _resolve_readonly("agent", True, "agent") is False


def test_explore_is_legacy_alias():
    assert _resolve_readonly(None, True, "agent") is True
    assert _resolve_readonly(None, False, "agent") is False


def test_master_readonly_or_plan_forces_readonly_subagent():
    assert _resolve_readonly("agent", False, "readonly") is True
    assert _resolve_readonly("agent", False, "plan") is True


def test_invalid_mode_falls_back_to_explore():
    assert _resolve_readonly("bogus", True, "agent") is True
    assert _resolve_readonly("bogus", False, "agent") is False
