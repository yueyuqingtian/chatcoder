"""系统提示词 section 注册与条件删节单测（plan-548-2277）。

守护点：改造前条件删节靠段落标题字符串 `index/split`，标题文案一改就静默失效，
且无任何测试能发现。这里按 section id 断言，并用「替换标题后仍生效」反向验证
不再依赖散落的字面量。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.orchestration.prompts.main import _MODE_NOTES, MAIN_SYSTEM_PROMPT, build_main_system_prompt
from app.orchestration.prompts.sections import (
    SECTION_IDS,
    SECTIONS,
    cut_section,
    cut_section_by_id,
    has_section,
    section_by_id,
)


def _build(**kw):
    return build_main_system_prompt(**kw)


# ── 1. 建议卡段：按开关删节 ──
def test_suggestion_section_toggled():
    off = _build(suggestion_enabled=False)
    on = _build(suggestion_enabled=True)
    assert "## Wrap-up — offer the next step as a card" not in off
    assert "## Core Principles" in off  # 边界行必须保留
    assert "## Wrap-up — offer the next step as a card" in on


# ── 2. 计划模式工作流：非计划模式被替换为 Mode adherence ──
def test_plan_workflow_section_toggled():
    off = _build(plan_flow_enabled=False)
    on = _build(plan_flow_enabled=True)
    assert "### Plan-mode workflow" not in off
    assert "### Mode adherence" in off
    assert "### Decide before you start" in off  # 边界行必须保留
    assert "### Plan-mode workflow" in on
    assert "### Mode adherence" not in on


# ── 3. 工作流章节：仅工作流模式保留 ──
def test_workflow_section_toggled():
    off = _build(workflow_mode=False)
    on = _build(workflow_mode=True)
    assert "## Workflow runs" not in off
    assert "## Planning — decide for yourself" in off
    assert "## Workflow runs" in on


# ── 4. 子代理决策段：关闭子代理时整段移除 ──
def test_subagent_decision_section_toggled():
    off = _build(enable_subagents=False)
    on = _build(enable_subagents=True)
    assert "## Decision Guide — when to spawn subagents" not in off
    assert "## Planning — decide for yourself" in off
    assert "## Decision Guide — when to spawn subagents" in on
    # 关闭子代理时三条委派纪律也应消失
    assert "Never spawn for trivia" not in off


# ── 5. 记忆纪律：来自 memory_rules 唯一事实源 ──
def test_memory_section_comes_from_memory_rules():
    off = _build(memory_enabled=False)
    on = _build(memory_enabled=True)
    for marker in ("## Persistent Memory", "## Which scope", "30 seconds"):
        assert marker not in off
        assert marker in on
    # 三层判据必须都在（global / project / session）
    assert "**global**" in on and "**project**" in on and "**session**" in on


# ── 6. 新增引导章节都在主提示里 ──
def test_new_guidance_sections_present():
    p = _build()
    assert "## Before you start — requirement triage" in p
    assert "## Tool selection map" in p
    assert "## Toolbox check" in p
    # plan-549-2281：子会话已下线，主提示必须说清「spawn_subagent 是唯一并行通道」，
    # 并且明确 mode=agent 可以委派实现（旧句 implementation is never delegated 是矛盾根源）
    assert "spawn_subagent" in p
    assert "no separate" in p and "sub-session" in p
    assert "implementation is never delegated" not in p
    assert 'mode="agent"' in p


# ── 7. cut_section 的健壮性 ──
def test_cut_section_is_safe():
    text = "A\n## START\nmiddle\n## END\nB"
    # 正常切：保留 end_heading 及其后内容
    assert cut_section(text, "## START", "## END") == "A\n## END\nB"
    # 带替换：替换串插在 end_heading 之前
    assert cut_section(text, "## START", "## END", "R\n") == "A\nR\n## END\nB"
    # 起始标题不存在 → 原样返回
    assert cut_section(text, "## NOPE", "## END") == text
    # 结束标题不存在 → 原样返回（宁可多留一段也不切坏）
    assert cut_section(text, "## START", "## NOPE") == text
    # 边界顺序颠倒 → 原样返回
    assert cut_section("A\n## END\nmid\n## START\nB", "## START", "## END") == "A\n## END\nmid\n## START\nB"
    # 空文本 / 空标题
    assert cut_section("", "## START", "## END") == ""
    assert cut_section(text, "", "## END") == text


def test_cut_section_by_id_unknown_id_is_noop():
    text = MAIN_SYSTEM_PROMPT
    assert cut_section_by_id(text, "no_such_section") == text
    assert section_by_id("no_such_section") is None


# ── 7.1 备选边界回退：候选按顺序生效，全缺失才原样返回（A22 的机制）──
def test_cut_section_falls_back_to_alt_boundary():
    text = "A\n## START\nmid\n## FIRST\nX\n## SECOND\nB"
    # 首选边界在：用首选
    assert cut_section(text, "## START", ("## FIRST", "## SECOND")) == "A\n## FIRST\nX\n## SECOND\nB"
    # 首选边界已被先前的删节带走：回退到次选边界
    after_first_cut = cut_section(text, "## FIRST", "## SECOND")
    assert "## FIRST" not in after_first_cut
    assert cut_section(after_first_cut, "## START", ("## FIRST", "## SECOND")) == "A\n## SECOND\nB"
    # 单字符串仍按原语义工作（向后兼容）
    assert cut_section(text, "## START", "## SECOND") == "A\n## SECOND\nB"
    # 所有候选都缺失：原样返回
    assert cut_section(text, "## START", ("## NOPE", "## ALSO_NOPE")) == text
    # 候选里的空串被跳过，不影响有效候选
    assert cut_section(text, "## START", ("", "## SECOND")) == "A\n## SECOND\nB"


# ── 7.2 A22：四个开关组合下「决策指南 / 恢复子代理 / 工作流 / 计划」的存留 ──
# plan-562-2318 A22：子代理开关与工作流模式互不耦合。此前 subagent_decision 的
# end_heading 直指 `## Planning`，把整段 `## Workflow runs` 圈进了删节区间——
# 「工作流模式 + 关闭子代理」会连坐删掉工作流规范。
_DECISION_GUIDE = "## Decision Guide — when to spawn subagents"
_RESUMING = "## Resuming interrupted subagents"
_WORKFLOW_RUNS = "## Workflow runs"
_PLANNING = "## Planning — decide for yourself"


@pytest.mark.parametrize(
    "workflow_mode, enable_subagents, decision, resuming, workflow, planning",
    [
        (True, True, True, True, True, True),      # 全开：四段都在
        (True, False, False, False, True, True),   # A22 目标：保留 Workflow runs
        (False, True, True, True, False, True),    # 非工作流：仅删 Workflow runs
        (False, False, False, False, False, True),  # 回退到 Planning 边界，与改造前等价
    ],
)
def test_subagent_and_workflow_switch_combinations(
    workflow_mode, enable_subagents, decision, resuming, workflow, planning
):
    p = _build(workflow_mode=workflow_mode, enable_subagents=enable_subagents)
    assert (_DECISION_GUIDE in p) is decision
    assert (_RESUMING in p) is resuming
    assert (_WORKFLOW_RUNS in p) is workflow
    assert (_PLANNING in p) is planning


# ── 10. A24：只读/计划执行模式优先于完全访问权限模式（ta3 流程规范）──
def test_readonly_execution_mode_wins_over_full_access_approval():
    """plan-562-2318 A24：两条轴正交，执行模式的硬边界优先。

    此前先判 approval_mode == "full"，「只读 + 完全访问」拿到完全访问版规范
    （写着写操作与命令执行无需审批、直接生效），与审批层对只读模式的硬拒绝冲突。
    """
    from app.orchestration.prompts.ta3_fusion import (
        CHATCODER_ADDENDUM,
        CHATCODER_ADDENDUM_FULL_ACCESS,
        CHATCODER_ADDENDUM_READONLY,
        _build_addendum,
    )

    readonly_full = _build_addendum("readonly", "full")
    assert readonly_full.startswith("## 流程规范（本环境附加）")
    # 只读模板不含「无需审批」与「直接生效」——这两句正与硬拒绝冲突
    assert "无需审批" not in readonly_full
    assert "直接生效" not in readonly_full
    assert "【只读模式】" in readonly_full
    # 计划模式同档
    assert "【只读模式】" in _build_addendum("plan", "full")
    # 可写模式不受影响：完全访问仍拿免审批版
    assert "无需审批" in _build_addendum("agent", "full")
    assert "无需审批" not in _build_addendum("agent", "ask")
    # 三个模板本身都还在（避免删条后某模板变成空串）
    for tpl in (CHATCODER_ADDENDUM, CHATCODER_ADDENDUM_FULL_ACCESS, CHATCODER_ADDENDUM_READONLY):
        assert tpl.strip()


# ── 11. A23：只读流程规范不得包含指向写入/命令动作的条款 ──
def test_readonly_addendum_has_no_write_or_process_clauses():
    """plan-562-2318 A23：只读会话不写文件、不跑命令，规范里不能再出现
    「改完复查关键变更」「交付前自查可运行性」「后台进程启动/停止」「交付前统一验证」。"""
    from app.orchestration.prompts.ta3_fusion import CHATCODER_ADDENDUM_READONLY

    for marker in ("修改文件后", "交付前自查", "按需验证", "BashKill", "后台模式", "长驻进程"):
        assert marker not in CHATCODER_ADDENDUM_READONLY, f"只读规范不该出现 {marker}"
    # 保留的三条：平台托管、能力边界、汇报要求
    assert "由 chatcoder 工作台托管" in CHATCODER_ADDENDUM_READONLY
    assert "禁止写入文件、运行命令、应用补丁" in CHATCODER_ADDENDUM_READONLY
    assert "最终结论在主窗口以清晰摘要汇报" in CHATCODER_ADDENDUM_READONLY
    # {lang_label} 占位符必须保留（否则 format 会丢失语言句）
    assert "{lang_label}" in CHATCODER_ADDENDUM_READONLY


# ── 8. 标题改动能被测试抓到：按 id 驱动，不依赖调用方字面量 ──
def test_section_titles_are_declared_once():
    """每个 section 的标题在 sections.py 里有唯一声明，且真实存在于主提示或已被删节。"""
    assert set(SECTION_IDS) == {s.id for s in SECTIONS}
    assert len(SECTION_IDS) == len(set(SECTION_IDS))  # id 不重复
    for sec in SECTIONS:
        assert sec.heading.startswith("#"), f"{sec.id} 的 heading 必须是标题行"
        assert sec.end_heading.startswith("#"), f"{sec.id} 的 end_heading 必须是标题行"
    # 除 plan_mode_workflow（是 ### 子段）外的 ## 段必须真实存在于基础提示
    base = MAIN_SYSTEM_PROMPT
    for sec in SECTIONS:
        if sec.heading.startswith("## "):
            assert sec.heading in base, f"{sec.id} 的标题在 MAIN_SYSTEM_PROMPT 中不存在（标题漂移）"
    assert "### Plan-mode workflow" in base

    # 反向验证：标题漂移后 has_section 立即为 False——这正是旧实现静默失效的地方，
    # 现在有断言能抓到。
    assert has_section(base, "suggestion_wrapup") is True
    assert has_section(base, "unknown_section") is False


# ── 9. 计划模式：可派只读子代理；子会话已下线，提示词不得再提它 ──
def test_plan_mode_allows_subagent_but_not_subsession():
    plan_note = _MODE_NOTES["plan"]
    assert "READ-ONLY subagents" in plan_note
    assert 'mode="readonly"' in plan_note
    # plan-549-2281：子会话能力已下线，计划模式说明里不得再出现 create_subsession / sub-session
    assert "create_subsession" not in plan_note
    assert "sub-session" not in plan_note
    # 模式能力段在 execution_mode=plan 时被拼进提示词
    p = _build(execution_mode="plan")
    assert "READ-ONLY subagents" in p
    assert "create_subsession" not in p
