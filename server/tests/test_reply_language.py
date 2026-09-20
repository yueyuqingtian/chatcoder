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


def test_language_label():
    assert language_label("zh") == "简体中文"
    assert language_label("en") == "English"
    assert "语言" in language_label("auto")


# ── 2. 语言纪律块语义 ────────────────────────────────────────────────────

def test_directive_zh_semantics():
    d = build_language_directive("zh")
    assert "MANDATORY" in d and "highest priority" in d
    assert "本轮最新一条用户消息" in d
    # 明确「不得被历史/工具/压缩摘要改变」
    assert "压缩摘要" in d and "规则文档" in d
    # 保留原文不翻译
    assert "保留原样" in d


def test_directive_en_semantics():
    d = build_language_directive("en")
    assert "MANDATORY" in d
    assert "English" in d
    assert "history messages" in d
    # 英文锚定时不该混入中文细则
    assert "本轮锚定语言" not in d


def test_directive_auto_semantics():
    d = build_language_directive("auto")
    assert "follow the user's latest message" in d


def test_pin_line():
    assert "简体中文" in build_language_pin_line("zh")
    assert "English" in build_language_pin_line("en")
    assert "自行识别" in build_language_pin_line("auto")


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
