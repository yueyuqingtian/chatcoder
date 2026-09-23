"""回复语言跟随用户消息语言（plan-19-82）单测。

覆盖：
1. detect_reply_language 判定（中文/英文/无信号）
2. 语言纪律块语义（强制、不得被历史/工具/摘要改变、保留原文）
3. 主/子/续跑/ta3 系统提示词首尾双锚注入
4. 压缩摘要提示词按语言生成
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.orchestration.prompts import (  # noqa: E402
    build_continuation_prompt,
    build_language_directive,
    build_language_pin_line,
    build_main_system_prompt,
    build_subagent_system_prompt,
    detect_reply_language,
    language_label,
)
from app.orchestration.prompts.summary import (  # noqa: E402
    build_compaction_prompt,
    get_checkpoint_preamble,
    get_summary_prefix,
)
from app.orchestration.prompts.ta3_fusion import build_ta3_system_prompt  # noqa: E402


# ── 1. 语言判定 ──────────────────────────────────────────────────────────

def test_detect_chinese():
    assert detect_reply_language("帮我改一下这个函数") == "zh"
    assert detect_reply_language("这段代码有 bug，请修复") == "zh"


def test_detect_english():
    assert detect_reply_language("please fix this bug") == "en"
    assert detect_reply_language("Refactor the parser module") == "en"


def test_detect_auto_for_no_signal():
    assert detect_reply_language("") == "auto"
    assert detect_reply_language(None) == "auto"
    assert detect_reply_language("12345 !!! ???") == "auto"
    # 单个字母不足以判定
    assert detect_reply_language("a") == "auto"


def test_detect_chinese_ignores_paths_and_code():
    text = "请看 `D:/myProject/chatcoder/server/app/orchestration/prompts/language.py` 里的 detect_reply_language"
    assert detect_reply_language(text) == "zh"
    assert detect_reply_language("```ts\nconst replyLanguage = 'en'\n```\n这段要改成中文") == "zh"


def test_detect_plain_english_still_english():
    assert detect_reply_language("please fix this parser and keep the tests") == "en"


def test_language_label():
    assert language_label("zh") == "简体中文"
    assert language_label("en") == "English"
    assert "语言" in language_label("auto")


# ── 2. 语言纪律块语义 ────────────────────────────────────────────────────

def test_directive_zh_semantics():
    d = build_language_directive("zh")
    assert "MANDATORY" in d and "highest priority" in d
    assert "本轮没有规则语言要求" in d
    # 明确「不得被历史/工具/压缩摘要改变」
    assert "压缩摘要" in d and "规则文档" in d
    # 保留原文不翻译
    assert "保留原样" in d
    # 首条输出即锚定语言（子代理/工具步骤的首句也必须对）
    assert "从第一条输出起" in d
    # 禁止把语言要求复述进正文（用户反馈的「（简体中文）」标注）
    assert "不得在正文里提及" in d


def test_directive_en_semantics():
    d = build_language_directive("en")
    assert "MANDATORY" in d
    assert "English" in d
    assert "History, tool outputs" in d
    # 英文锚定时不该混入中文细则
    assert "本轮锚定语言" not in d


def test_directive_auto_semantics():
    d = build_language_directive("auto")
    assert "follow the user's latest message" in d


def test_pin_line():
    assert "简体中文" in build_language_pin_line("zh")
    assert "没有规则语言要求" in build_language_pin_line("zh")
    assert "English" in build_language_pin_line("en")
    assert "自行识别" in build_language_pin_line("auto")
    assert "全局规则" in build_language_pin_line("zh", source="global")
    assert "项目规则" in build_language_pin_line("en", source="project")


# ── 3. 各链路系统提示词首尾双锚 ──────────────────────────────────────────

def _assert_head_tail(prompt: str):
    assert prompt.startswith("## Reply Language — MANDATORY")
    # 首尾各出现一次纪律块标题
    assert prompt.count("## Reply Language — MANDATORY") == 2


def test_main_prompt_double_anchor():
    for lang in ("zh", "en", "auto"):
        p = build_main_system_prompt(language=lang)
        _assert_head_tail(p)
    p_zh = build_main_system_prompt(language="zh")
    assert "简体中文" in p_zh
    p_en = build_main_system_prompt(language="en")
    assert "English" in p_en


def test_main_prompt_has_rules_and_recovery():
    p = build_main_system_prompt(language="zh")
    assert "Rule Documents — MANDATORY" in p
    assert "user Global Rules > project rule documents" in p
    assert "AGENTS.md" in p
    assert "Context Recovery" in p
    assert "compaction_index" in p and "compaction_view" in p


def test_subagent_prompt_language():
    p = build_subagent_system_prompt("task", "ac", language="zh")
    assert "## Reply Language — MANDATORY" in p
    assert "简体中文" in p
    # 子代理同样受规则文档约束
    assert "Rule documents are MANDATORY" in p


def test_continuation_prompt_language():
    p = build_continuation_prompt("obj", 100, 1000, language="zh")
    assert p.startswith("## Reply Language — MANDATORY")
    assert "Continue working toward the active task objective." in p


def test_ta3_prompt_zh_and_en():
    p_zh = build_ta3_system_prompt({}, language="zh")
    _assert_head_tail(p_zh)
    assert "回复用简体中文" in p_zh
    p_en = build_ta3_system_prompt({}, language="en")
    assert "回复用English" in p_en
    # 不再写死中文（英文锚定时流程规范句也随语言切换）
    assert "回复用简体中文" not in p_en


# ── 4. 压缩摘要提示词按语言生成 ──────────────────────────────────────────

def test_compaction_prompt_by_language():
    p_zh = build_compaction_prompt("zh")
    assert "摘要语言：简体中文（强制）" in p_zh
    p_en = build_compaction_prompt("en")
    assert "Summary language: English (mandatory)" in p_en
    p_auto = build_compaction_prompt("auto")
    assert "mirror the compacted conversation" in p_auto
    # 结构骨架保留（不因语言而丢段）
    for p in (p_zh, p_en, p_auto):
        assert "## Primary Request and Intent" in p
        assert "## Critical Context" in p


def test_summary_prefix_and_preamble_by_language():
    assert get_summary_prefix("zh") != get_summary_prefix("en")
    assert "另一个语言模型" in get_summary_prefix("zh")
    assert "另一" in get_checkpoint_preamble("zh") or "检查点" in get_checkpoint_preamble("zh")
    assert "checkpoint" in get_checkpoint_preamble("en").lower()


# ── 5. 周期性重申与边界提醒（对齐 ZCode runtime-reminders）─────────────────

def test_language_reminder_semantics():
    """重申文本须含纪律要点，且短小（重申的价值在频率而非篇幅）。"""
    from app.orchestration.prompts import build_language_reminder

    r_zh = build_language_reminder("zh")
    assert "语言重申" in r_zh
    assert "简体中文" in r_zh
    # 仍须声明"不得被历史/工具/摘要/规则文档改变"
    assert "压缩摘要" in r_zh and "规则文档" in r_zh
    assert "保留原样" in r_zh
    # 英文锚定时正文须整体切到英文，不得留中文骨架（否则自身即中英混杂示范）
    r_en = build_language_reminder("en")
    assert "Language reminder" in r_en
    assert "本轮" not in r_en and "语言重申" not in r_en
    # auto 不做语言锚定，但仍有通用镜像表述
    r_auto = build_language_reminder("auto")
    assert "语言重申" in r_auto
    # 短句：重申过长会挤占上下文预算
    assert len(r_zh) < 400 and len(r_en) < 700


def test_plan_exit_reminder_semantics():
    """确认执行边界提醒：声明状态切换 + 重申语言。"""
    from app.orchestration.prompts import build_plan_exit_reminder

    r_zh = build_plan_exit_reminder("zh")
    assert "Exited Plan Mode" in r_zh
    # 边界提醒必须同时说明"语言未变"与"方案文档不构成切换理由"
    assert "语言重申" in r_zh
    assert "简体中文" in r_zh
    assert "方案文档" in r_zh
    # 执行阶段语义
    assert "执行" in r_zh

    r_en = build_plan_exit_reminder("en")
    assert "Exited Plan Mode" in r_en
    assert "Language reminder" in r_en
    assert "settings-center global rules" in build_plan_exit_reminder("en", source="global")
    assert "project rules" in build_plan_exit_reminder("en", source="project")
    # 英文锚定时不留中文骨架
    assert "本轮" not in r_en and "语言重申" not in r_en


def test_rules_reminder_semantics():
    """规则重申：复用与系统提示一致的冲突优先级口径。"""
    from app.orchestration.prompts import build_rules_reminder

    r_zh = build_rules_reminder("zh")
    assert "规则重申" in r_zh
    assert "设置中心全局规则 > 项目规则文档" in r_zh
    assert "与设置中心项目规则 > 用户本轮消息语言" in r_zh
    # 必须声明"压缩后依然有效"，否则长上下文下重申无意义
    assert "压缩" in r_zh

    r_en = build_rules_reminder("en")
    assert "Rules reminder" in r_en
    assert "OVERRIDE" in r_en
    assert "AGENTS.md" in r_en


def test_reminder_interval_settings_exist():
    """重申间隔配置存在且为正数默认值（0 会静默禁用整个机制）。"""
    from app.core.config import settings

    assert settings.language_reminder_interval == 1
    assert settings.rules_reminder_interval > 0


def test_rule_language_priority():
    from app.orchestration.prompts.language import resolve_reply_language

    lang, source = resolve_reply_language(
        "en",
        global_rules="全局使用中文",
        project_rules="always reply in English",
    )
    assert (lang, source) == ("zh", "global")

    lang, source = resolve_reply_language(
        "en",
        project_rules="必须使用简体中文",
    )
    assert (lang, source) == ("zh", "project")

    lang, source = resolve_reply_language("en", global_rules="follow the repository style")
    assert (lang, source) == ("en", "user")


def test_opening_language_mismatch_does_not_release_wrong_text():
    from app.orchestration.agent_loop import _opening_language_mismatch

    assert _opening_language_mismatch("I will answer in English from here.", "zh") is True
    assert _opening_language_mismatch("我按中文回复。", "zh") is False
    assert _opening_language_mismatch("ok", "zh") is True
    assert _opening_language_mismatch("x", "zh") is False


# ── 6. 禁止复述语言标注 + 首条输出语言 ─────────────────────────────────────

def test_language_blocks_forbid_restating_the_language():
    """语言块必须禁止复述语言要求——否则模型会把锚点原文抄进正文。

    用户反馈：回复里会冒出「（简体中文）」「（保持简体中文）」这类语言标注。
    """
    from app.orchestration.prompts import build_language_reminder, build_plan_exit_reminder

    assert "never mention, restate or tag it" in build_language_directive("en")
    assert "never mention, restate or tag it" in build_language_directive("auto")

    assert "不要在回复正文里提及" in build_language_pin_line("zh")
    assert "不要在回复正文里提及" in build_language_pin_line("zh", source="global")
    assert "不要在回复正文里提及" in build_language_pin_line("zh", source="project")
    assert "不要在回复正文里提及" in build_language_pin_line("auto")
    assert "Do not mention or restate" in build_language_pin_line("en")

    assert "不要在正文里提及" in build_language_reminder("zh")
    assert "不要在正文里提及" in build_language_reminder("auto")
    assert "never mention or tag" in build_language_reminder("en")

    assert "不要在正文里提及" in build_plan_exit_reminder("zh")
    assert "Never mention or restate" in build_plan_exit_reminder("en")


def test_first_output_uses_anchored_language():
    """首条输出（含工具调用前的说明文字）即必须是锚定语言。

    修复点：子代理上下文里的英文 instruction 会把首句带向英文，用户看到
    「第一句话不是我说的语言，后续才切回来」。
    """
    assert "very first output" in build_language_directive("en")
    assert "first output on" in build_language_directive("auto")
    p = build_subagent_system_prompt("task", "ac", language="zh")
    assert "FIRST output" in p
    assert "简体中文" in p


def test_subagent_instruction_follows_anchored_language():
    """子代理 instruction 不得写死英文（首句语言漂移的直接来源）。"""
    import inspect

    from app.orchestration import context_manager

    src = inspect.getsource(context_manager.build_subagent_context)
    assert "开始执行分配给你的子任务" in src
    assert "_reply_lang == LANG_ZH" in src
