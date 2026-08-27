"""压缩/摘要提示词。

v30：COMPACTION_PROMPT 升级为结构化 checkpoint 格式，参照 deepseek-harness
compaction-basic 的 COMPACTION_INSTRUCTION（9 段结构 + <compacted-summary> 帧）。
结构化分段让接手 LLM 按固定槽位检索信息，避免自由文本摘要遗漏关键决策/文件路径。
"""

COMPACTION_PROMPT = """You are now acting as a compaction engine for this AI coding assistant. Condense the conversation ABOVE into a structured checkpoint that lets another model resume the work with no loss of essential context.

Output EXACTLY the Markdown structure below: keep every section, in order. Use terse bullets, not prose paragraphs. Write "(none)" for an empty section — never drop a section.

## Primary Request and Intent
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
- [decisions and their rationale, constraints, user preferences, open questions, data needed to continue]

Rules:
- Write concise Chinese engineering prose when the conversation is Chinese; preserve exact file paths, commands, error strings, identifiers, numeric values, function signatures, and syntax fragments verbatim.
- Capture user feedback and explicit instructions faithfully, especially corrections.
- Do NOT mention this summarization request or that the context was compacted.
- Output only the checkpoint text: do not call any tool or take any other action.
- If the conversation already contains a <compacted-summary> block, it is a PRIOR checkpoint. Do not copy it forward verbatim: preserve still-true facts, drop stale ones, and merge newer information into a single consolidated summary under the same structure."""

SUMMARY_PREFIX = """Another language model started to solve this problem and produced a summary of its thinking process. You also have access to the state of the tools that were used by that language model. Use this to build on the work that has already been done and avoid duplicating work.

Here is the summary produced by the other language model:"""

# v30: 压缩 checkpoint 的前言（告诉接手模型把压缩内容视为既定背景，不要复述或怀疑）
CHECKPOINT_PREAMBLE = (
    "This is an automatically generated checkpoint condensing an earlier span of the "
    "conversation to free up context. Treat the captured context as established background "
    "and build on it without restating it. Continue the task directly from the messages that "
    "follow, without acknowledging this checkpoint."
)

# v30: checkpoint 内容帧标签（前端据此识别压缩卡片；后续压缩遇到旧帧时合并而非复制）
SUMMARY_OPEN_TAG = "<compacted-summary>"
SUMMARY_CLOSE_TAG = "</compacted-summary>"
