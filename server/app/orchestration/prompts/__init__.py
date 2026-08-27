"""提示词分层包（v2，英文）。统一导出，便于集中维护。"""
from app.orchestration.prompts.base import (
    WORKFLOW_COMMON,
    build_default_agent_prompt,
    get_core_role_prompt,
)
from app.orchestration.prompts.continuation import build_continuation_prompt
from app.orchestration.prompts.main import MAIN_SYSTEM_PROMPT, build_main_system_prompt
from app.orchestration.prompts.subagent import (
    SUBAGENT_SYSTEM_PROMPT,
    build_subagent_system_prompt,
)
from app.orchestration.prompts.summary import (
    CHECKPOINT_PREAMBLE,
    COMPACTION_PROMPT,
    SUMMARY_CLOSE_TAG,
    SUMMARY_OPEN_TAG,
    SUMMARY_PREFIX,
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
    "SUMMARY_PREFIX",
    "CHECKPOINT_PREAMBLE",
    "SUMMARY_OPEN_TAG",
    "SUMMARY_CLOSE_TAG",
]
