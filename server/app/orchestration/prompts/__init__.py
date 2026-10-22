"""提示词分层包（v2，英文）。统一导出，便于集中维护。"""

# plan-522-2209：提示词版本号。
# 此前全库没有任何版本标识，提示词一旦改动就无从追溯——线上出现行为差异时，只能靠翻 git
# 历史猜是哪次改动生效的，也无法把 CHANGELOG 条目与提示词行为变更对应起来。
# 改动本包内任何提示词文本时同步提升此值，并把该值随 turn 记录落库（见 engine/agent_loop 的
# turn 元数据），排查时能直接确认「这一轮用的是哪一版提示词」。
PROMPT_VERSION = "2026.10.22-1"

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
    build_language_reminder,
    build_plan_exit_reminder,
    build_rules_anchor,
    build_rules_reminder,
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
    # plan-19-82 增强（对齐 ZCode runtime-reminders）：周期性重申 + 边界提醒
    "build_language_reminder",
    "build_plan_exit_reminder",
    "build_rules_reminder",
    # 每轮用户消息随行下发的规则锚点（含本轮实际加载的项目规则文档清单）
    "build_rules_anchor",
]
