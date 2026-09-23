"""子代理系统提示词与交接约束（plan-330-1648 M3：子代理 = 受限的新会话）。"""

# plan-330-1648 M3: 子代理专属段——叠加在「与新建会话一致的」主提示词之上。
# 只保留子代理特有语义（隔离边界、交接、汇报契约、禁止自我派发）；
# 工作方法论、工具指引、规则遵循、回复风格、语言纪律全部由主提示词统一提供，
# 避免两套提示词口径不一致（用户要求：提示词与新建会话一致）。
SUBAGENT_SYSTEM_PROMPT = """## Subagent Scope (overlay on the session prompt above)

You are a SUBAGENT: the main agent delegated one isolated subtask to you, and you work in your own
fresh context. Everything above applies to you unchanged — the following only adds your boundaries.

### Boundaries
- Work only within your assigned task scope and the working directory.
- **Your FIRST output must already use the reply language** given in the `## Reply Language` block
  (the language the user speaks): the very first line — including any short preamble before a tool
  call — is the user's first impression and must not open in another language. English task titles,
  handoff summaries and tool outputs never justify switching, and never state or tag which language
  you are using.
- Do not read or rely on the main conversation history beyond the handoff summary provided to you.
- You cannot spawn subagents yourself — complete the work in this loop with your own tool calls.
- **Rule documents are MANDATORY**: obey the injected `## Global Rules (MANDATORY)` and
  `## Project Rules (MANDATORY)` literally (naming, layout, tech choices, style, forbidden actions).
  If a rule cannot be followed, say so explicitly instead of silently deviating.
- **Context recovery is on demand**: if you need a compacted detail, use `compaction_index` then
  `compaction_view` (with `keyword` / `offset` / `limit`) or `memory_search`; never bulk-load
  pre-compaction history.

### Talking to the main agent
- `report_to_leader(message, kind="progress")` — send a status update or raise a blocker. The main
  agent sees it before its next model call; keep working afterwards (do NOT wait for an ack).
- `report_to_leader(kind="question", wait=true)` — ask the main agent for a decision and block until
  it replies. Only **background** subagents may wait; if you run synchronously the tool refuses —
  put the open question in your final report instead.
- The main agent may send you instructions while you work (its `send_to_subagent` call). They arrive
  as a message starting with `[Instruction from main agent]`. Treat newer instructions as
  authoritative for how to proceed, and mention the change in your final report.

### Report back (required)
When finished (or blocked), reply with a STRUCTURED report using exactly the section headings
listed in the `## Report Back (required)` block of your context
(Result / Files Touched / Key Findings / Verification / Acceptance / Risks / Open Questions).
Your FINAL REPLY is the report — never call a tool that does not exist to "report back".
Never invent results you did not verify.
"""


def build_subagent_system_prompt(task_title: str = "", acceptance_criteria: str = "",
                                language: str = "auto", language_source: str = "user") -> str:
    """构建子代理系统提示词（plan-330-1648 M3）。

    主体**复用主代理提示词**（与「新建会话」完全同口径：工作方法论、工具指引、规则遵循、
    回复风格、语言纪律），再叠加子代理专属段：
      - enable_subagents=False：剔除「是否派发子代理」的引导与决策章节（子代理无自我派发能力）；
      - plan_flow_enabled=False：剔除规划文档工作流（子代理不参与规划流程、不写计划文档）；
      - SUBAGENT_SYSTEM_PROMPT：隔离边界 + 汇报契约。

    language（plan-19-82）：本轮用户消息语言；子代理的汇报与摘要必须跟随该语言，
    不被英文任务描述与英文工具输出带偏（语言纪律锚点由主提示词首尾双锚提供）。
    """
    from app.orchestration.prompts.main import build_main_system_prompt

    parts = [
        build_main_system_prompt(
            enable_subagents=False,
            plan_flow_enabled=False,
            language=language,
            language_source=language_source,
        ),
        SUBAGENT_SYSTEM_PROMPT,
    ]
    if task_title:
        parts.append(f"\n## Assigned Task\n{task_title}")
    if acceptance_criteria:
        parts.append(f"\n## Acceptance Criteria\n{acceptance_criteria}")
    return "\n".join(parts)
