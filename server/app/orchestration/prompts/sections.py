"""系统提示词的显式 section 注册（plan-548-2277）。

【为什么需要】
改造前 `build_main_system_prompt` 用**段落标题字符串**做条件删节：

```python
if not suggestion_enabled and "## Wrap-up — offer the next step as a card" in prompt:
    _w_start = prompt.index("## Wrap-up — offer the next step as a card")
    _w_end = prompt.index("## Core Principles", _w_start)
    prompt = prompt[:_w_start] + prompt[_w_end:]
```

标题文案一改（译写、加词、改标点），`in` 判断静默为 False，该删的段就留在提示词里；
更糟的是「删 subagent 段」这种按 `## Planning` 收尾的写法，一旦标题漂移会把后文连根切错。
四处同类逻辑散落，没有任何测试能发现它们失效——因为提示词本身仍然"看起来正常"。

本模块把每个可删节段的标题、条件、优先级收敛成声明式数据：标题只在这里出现一次，
`build_main_system_prompt` 不再裸写字符串；测试按 `id` 断言，不再依赖标题字面量。

【约定】
- `heading` 必须是 `MAIN_SYSTEM_PROMPT` 里真实存在的一行（`##` / `###` 开头）；
- `end_heading` 是删节区间的结束边界（不含），必须排在 `heading` 之后；
- `alt_end_headings` 是备选结束边界：前一个边界已被更早的删节带走时依次回退
  （plan-562-2318 A22：`workflow_mode=False` 会先删掉 `## Workflow runs`，只写单一
  边界会让「关子代理」在非工作流模式下静默失效）；
- `cut_section` 所有候选边界都找不到时**原样返回**——宁可多留一段，也不能把提示词切坏。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PromptSection:
    """一个可按条件删节的提示词段落。

    id：稳定标识（测试与日志用它，不用标题）
    heading：段落起始标题行（含 `##`/`###`）
    end_heading：删节区间的结束边界（该行本身保留）
    condition：该段保留所需的条件名（仅作文档与测试断言，真正判定在 build_main_system_prompt）
    priority：内容超预算时的降级优先级（数字越大越先被裁；目前仅声明，未启用裁剪）
    alt_end_headings：备选结束边界，按顺序回退（end_heading 已不在文本里时使用）
    """

    id: str
    heading: str
    end_heading: str
    condition: str
    priority: int = 100
    alt_end_headings: tuple[str, ...] = ()


SECTIONS: tuple[PromptSection, ...] = (
    PromptSection(
        id="suggestion_wrapup",
        heading="## Wrap-up — offer the next step as a card",
        end_heading="## Core Principles",
        condition="suggestion_enabled",
        priority=90,
    ),
    PromptSection(
        id="plan_mode_workflow",
        heading="### Plan-mode workflow",
        end_heading="### Decide before you start",
        condition="plan_flow_enabled",
        # 计划工作流只在计划模式保留；被替换为更短的 ### Mode adherence
        priority=10,
    ),
    PromptSection(
        id="workflow_runs",
        heading="## Workflow runs — 拼积木式并行/串行拆分",
        end_heading="## Planning — decide for yourself, track with todo_write",
        condition="workflow_mode",
        priority=60,
    ),
    PromptSection(
        id="subagent_decision",
        heading="## Decision Guide — when to spawn subagents",
        # plan-562-2318 A22：边界收到 `## Workflow runs` 之前，避免关掉子代理时连坐
        # 删掉整段工作流规范（工作流模式与子代理开关互不耦合，该组合可达）。
        end_heading="## Workflow runs — 拼积木式并行/串行拆分",
        condition="enable_subagents",
        priority=50,
        # 非工作流模式会先删掉 workflow_runs 整段（含该标题行），此时回落到 Planning
        # 边界：决策指南与「恢复被中断的子代理」照旧一并删除，与改造前等价。
        alt_end_headings=("## Planning — decide for yourself, track with todo_write",),
    ),
)

SECTION_IDS: tuple[str, ...] = tuple(s.id for s in SECTIONS)


def section_by_id(section_id: str) -> PromptSection | None:
    """按 id 取声明；未知 id 返回 None（调用方自行决定降级）。"""
    return next((s for s in SECTIONS if s.id == section_id), None)


def cut_section(text: str, start_heading: str,
                end_heading: str | tuple[str, ...],
                replacement: str = "") -> str:
    """删除 [start_heading, end_heading) 区间，保留命中的结束边界行及其后内容。

    - end_heading 可传单个标题或按优先级排列的标题元组：依次尝试，用**第一个**
      在 start_heading 之后出现的边界（plan-562-2318 A22 的备选边界回退）；
    - 所有候选边界都缺失：原样返回（提示词结构变了也不能切坏）；
    - 边界顺序颠倒（end 在 start 之前）：find 从 start 之后起查，同样原样返回；
    - replacement 非空时插入到命中边界之前（用于「删掉 A 段、换成 B 段」）。
    """
    if not text or not start_heading or not end_heading:
        return text
    start = text.find(start_heading)
    if start < 0:
        return text
    candidates = (end_heading,) if isinstance(end_heading, str) else tuple(end_heading)
    end = -1
    for candidate in candidates:
        if not candidate:
            continue
        pos = text.find(candidate, start + len(start_heading))
        if pos >= 0:
            end = pos
            break
    if end < 0:
        return text
    return text[:start] + replacement + text[end:]


def cut_section_by_id(text: str, section_id: str, replacement: str = "") -> str:
    """按 section id 删节；未声明的 id 原样返回。"""
    sec = section_by_id(section_id)
    if sec is None:
        return text
    return cut_section(text, sec.heading, (sec.end_heading, *sec.alt_end_headings),
                       replacement)


def has_section(text: str, section_id: str) -> bool:
    """该段是否仍在文本里（测试用；标题漂移时返回 False 会被测试抓到）。"""
    sec = section_by_id(section_id)
    if sec is None:
        return False
    return sec.heading in text
