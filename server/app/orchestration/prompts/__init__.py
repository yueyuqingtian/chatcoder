"""提示词分层包（v2，英文）。统一导出，便于集中维护。"""
from app.orchestration.prompts.base import (
    WORKFLOW_COMMON,
    build_default_agent_prompt,
    get_core_role_prompt,
)
from app.orchestration.prompts.continuation import build_continuation_prompt
from app.orchestration.prompts.language import (
    LANG_AUTO,
    LANG_EN,
    LANG_ZH,
    build_language_directive,
    build_language_pin_line,
    detect_reply_language,
    language_label,
)
from app.orchestration.prompts.main import MAIN_SYSTEM_PROMPT, build_main_system_prompt
from app.orchestration.prompts.subagent import (
    SUBAGENT_SYSTEM_PROMPT,
    build_subagent_system_prompt,
)
from app.orchestration.prompts.summary import (
    CHECKPOINT_PREAMBLE,
    CHECKPOINT_PREAMBLE_ZH,
    COMPACTION_PROMPT,
    SUMMARY_CLOSE_TAG,
    SUMMARY_OPEN_TAG,
    SUMMARY_PREFIX,
    SUMMARY_PREFIX_ZH,
    build_compaction_prompt,
    get_checkpoint_preamble,
    get_summary_prefix,
)

__all__ = [
    "WORKFLOW_COMMON",
    "build_default_agent_prompt",
    "get_core_role_prompt",
    "build_continuation_prompt",
    "MAIN_SYSTEM_PROMPT",
    "build_main_system_prompt",
    "SUBAGENT_SYSTEM_PROMPT",
    "build_subagent_system_prompt",
    "COMPACTION_PROMPT",
    "build_compaction_prompt",
    "SUMMARY_PREFIX",
    "SUMMARY_PREFIX_ZH",
    "get_summary_prefix",
    "CHECKPOINT_PREAMBLE",
    "CHECKPOINT_PREAMBLE_ZH",
    "get_checkpoint_preamble",
    "SUMMARY_OPEN_TAG",
    "SUMMARY_CLOSE_TAG",
    # plan-19-82: 回复语言跟随（用户消息语言驱动）
    "LANG_ZH",
    "LANG_EN",
    "LANG_AUTO",
    "detect_reply_language",
    "language_label",
    "build_language_directive",
    "build_language_pin_line",
]
