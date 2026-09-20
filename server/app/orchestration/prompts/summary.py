"""压缩/摘要提示词。

v30：COMPACTION_PROMPT 升级为结构化 checkpoint 格式，参照 deepseek-harness
compaction-basic 的 COMPACTION_INSTRUCTION（9 段结构 + <compacted-summary> 帧）。
结构化分段让接手 LLM 按固定槽位检索信息，避免自由文本摘要遗漏关键决策/文件路径。

plan-19-82：压缩产物是上下文的一部分，其语言会直接影响接手模型的回复语言
（英文 checkpoint 主导上下文 → 模型改说英文）。因此这里把语言规则由「条件式」
改为**按目标语言无条件执行**，并提供中英双版 preamble/prefix 供按轮选择。
"""

# 结构骨架（语言无关，提供给两种语言版本复用）
_SUMMARY_STRUCTURE = """## Primary Request and Intent
- [the user's original and evolving goals; quote verbatim where the exact wording matters]

## Key Technical Concepts
- [technologies, frameworks, patterns, and conventions in play]

## Files and Code
- [exact path: why it matters, key changes or snippets]

## Errors and Fixes
- [error: how it was resolved, plus any related user feedback]

## Pending Jobs
- [explicitly requested work not yet completed]

## Current Work
- [precisely what was in progress at this checkpoint]

## Next Step
- [the single next action, directly in line with the most recent request, or "(none)"]

## Critical Context
- [decisions and their rationale, constraints, user preferences, open questions, data needed to continue]"""

COMPACTION_PROMPT = f"""You are now acting as a compaction engine for this AI coding assistant. Condense the conversation ABOVE into a structured checkpoint that lets another model resume the work with no loss of essential context.

Output EXACTLY the Markdown structure below: keep every section, in order. Use terse bullets, not prose paragraphs. Write "(none)" for an empty section — never drop a section.

{_SUMMARY_STRUCTURE}

Rules:
- Write concise Chinese engineering prose when the conversation is Chinese; preserve exact file paths, commands, error strings, identifiers, numeric values, function signatures, and syntax fragments verbatim.
- Capture user feedback and explicit instructions faithfully, especially corrections.
- Do NOT mention this summarization request or that the context was compacted.
- Output only the checkpoint text: do not call any tool or take any other action.
- If the conversation already contains a <compacted-summary> block, it is a PRIOR checkpoint. Do not copy it forward verbatim: preserve still-true facts, drop stale ones, and merge newer information into a single consolidated summary under the same structure."""


def build_compaction_prompt(lang: str = "auto") -> str:
    """按目标语言生成压缩提示词（plan-19-82：语言规则无条件执行，不再条件判断）。

    lang: "zh" → 强制中文摘要；"en" → 强制英文摘要；其它 → 跟随被压缩内容的语言。
    """
    if lang == "zh":
        lang_rule = (
            "- **摘要语言：简体中文（强制）**。无论被压缩的对话、工具输出使用何种语言，"
            "本 checkpoint 必须用简体中文工程语言撰写；"
            "文件路径、命令、报错原文、标识符、数值、函数签名、代码片段一律保留原样。"
        )
    elif lang == "en":
        lang_rule = (
            "- **Summary language: English (mandatory)**. Write this checkpoint in English "
            "regardless of the language used by the compacted conversation or tool outputs; "
            "preserve file paths, commands, error strings, identifiers, numbers, function "
            "signatures and code snippets verbatim."
        )
    else:
        lang_rule = (
            "- **Summary language: mirror the compacted conversation.** Write the checkpoint in "
            "the dominant language of the compacted span; preserve file paths, commands, error "
            "strings, identifiers, numbers and code snippets verbatim."
        )
    body = COMPACTION_PROMPT.replace(
        "- Write concise Chinese engineering prose when the conversation is Chinese; preserve exact "
        "file paths, commands, error strings, identifiers, numeric values, function signatures, and "
        "syntax fragments verbatim.",
        lang_rule,
    )
    return body


# v30 前缀（英文原版，保持向后兼容导出）
SUMMARY_PREFIX = """Another language model started to solve this problem and produced a summary of its thinking process. You also have access to the state of the tools that were used by that language model. Use this to build on the work that has already been done and avoid duplicating work.

Here is the summary produced by the other language model:"""

SUMMARY_PREFIX_ZH = """另一个语言模型此前已着手解决该问题，并产出了它的思考摘要；你也可以访问它使用过的工具状态。请在此基础上继续，不要重复已完成的工作。

以下是该语言模型产出的摘要："""

# v30: 压缩 checkpoint 的前言（告诉接手模型把压缩内容视为既定背景，不要复述或怀疑）
CHECKPOINT_PREAMBLE = (
    "This is an automatically generated checkpoint condensing an earlier span of the "
    "conversation to free up context. Treat the captured context as established background "
    "and build on it without restating it. Continue the task directly from the messages that "
    "follow, without acknowledging this checkpoint."
)

CHECKPOINT_PREAMBLE_ZH = (
    "这是自动生成的检查点，用于压缩较早的一段对话以释放上下文。"
    "请把其中记录的内容当作既定背景，直接在其上继续工作、不要复述，"
    "并从随后的消息直接继续任务，无需回应本检查点。"
)


def get_summary_prefix(lang: str = "auto") -> str:
    """按语言取压缩摘要前言（plan-19-82：中文会话用中文前言，避免英文污染回复语言）。"""
    return SUMMARY_PREFIX_ZH if lang == "zh" else SUMMARY_PREFIX


def get_checkpoint_preamble(lang: str = "auto") -> str:
    """按语言取 checkpoint 前言。"""
    return CHECKPOINT_PREAMBLE_ZH if lang == "zh" else CHECKPOINT_PREAMBLE


# v30: checkpoint 内容帧标签（前端据此识别压缩卡片；后续压缩遇到旧帧时合并而非复制）
SUMMARY_OPEN_TAG = "<compacted-summary>"
SUMMARY_CLOSE_TAG = "</compacted-summary>"
