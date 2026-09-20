"""回复语言跟随（plan-19-82 步骤 1）。

设计要点（用户明确要求）：
- 回复语言由**用户实际发送的消息**决定，与「设置-界面语言」无关；
- 以**本轮最新一条用户消息**的语言为准（用户改语言后立即切换）；
- 历史消息 / 工具返回 / 压缩摘要 / 规则文档的语言**不得**改变回复语言；
- 代码、路径、命令、标识符、报错原文一律保留原样，不翻译。

实现刻意保持零第三方依赖（pyproject 无 langdetect 等语言检测库）：
只做 CJK / ASCII 字母计数比较，中英场景判定足够稳定，其它语言回退 "auto"
（此时由模型自行镜像用户消息语言）。
"""
from __future__ import annotations

import re

# CJK 统一表意文字（含扩展 A 段）+ 日文假名 + 韩文谚文
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")
# 拉丁/西里尔等字母（用于与 CJK 比较）
_LATIN_RE = re.compile(r"[A-Za-z]")

LANG_ZH = "zh"
LANG_EN = "en"
LANG_AUTO = "auto"

# 判定阈值：CJK 字符数需不少于字母数时才判为中文，避免「中文夹一个英文单词」误判为英文
_MIN_SIGNAL = 2


def detect_reply_language(text: str | None) -> str:
    """按用户消息文本判定回复语言，返回 "zh" / "en" / "auto"。

    规则（轻量、无依赖）：
    - CJK 字符数 >= 字母数 且 CJK >= _MIN_SIGNAL → "zh"
    - 字母数 > CJK 字符数 且 字母 >= _MIN_SIGNAL → "en"
    - 其余（纯数字/符号/空/语言信号不足）→ "auto"（交由模型镜像用户语言）
    """
    if not text:
        return LANG_AUTO
    cjk = len(_CJK_RE.findall(text))
    latin = len(_LATIN_RE.findall(text))
    if cjk >= _MIN_SIGNAL and cjk >= latin:
        return LANG_ZH
    if latin >= _MIN_SIGNAL and latin > cjk:
        return LANG_EN
    return LANG_AUTO


def language_label(lang: str) -> str:
    """语言码 → 人类可读语言名（用于锚点文案）。"""
    return {
        LANG_ZH: "简体中文",
        LANG_EN: "English",
    }.get(lang or LANG_AUTO, "与用户本轮消息相同的语言")


def build_language_directive(lang: str) -> str:
    """产出强语义、最高优先级的语言纪律块（系统提示首尾双锚）。

    lang: "zh" / "en" / "auto"（auto 时给出通用镜像表述）。
    """
    label = language_label(lang)
    if lang == LANG_ZH:
        pin = (
            "**本轮锚定语言：简体中文（zh）**——用户本轮最新消息使用简体中文，"
            "你的全部输出必须使用简体中文。"
        )
        bullets = (
            f"- 回复语言只由**本轮最新一条用户消息**决定（当前：{label}）。\n"
            f"- 历史消息、工具返回、压缩摘要（checkpoint）、规则文档使用何种语言，都**不得**改变回复语言；\n"
            f"  即使上文几乎全是英文，只要本轮用户消息是中文，就必须用中文回复。\n"
            f"- 同一会话内用户切换语言时，**从本轮起立即切换**，不沿用上一轮语言。\n"
            f"- 代码、文件路径、命令、标识符、报错原文一律保留原样，**不要翻译**。\n"
            f"- 不得因为「术语用英文更准确」而输出中英混杂的句子；若必须引用英文术语，嵌入中文句中即可。"
        )
    elif lang == LANG_EN:
        pin = (
            "**Speak-language for this turn: English (en)** — the user's latest message is in "
            "English, so every part of your output MUST be written in English."
        )
        bullets = (
            "- The reply language is decided ONLY by the user's most recent message "
            f"(currently: {label}).\n"
            "- The language used by history messages, tool outputs, compaction checkpoints or rule "
            "documents MUST NOT change your reply language; even if the context is mostly Chinese, "
            "reply in English because the user's latest message is English.\n"
            "- When the user switches language mid-session, switch immediately from this turn; do "
            "not keep the previous turn's language.\n"
            "- Keep code, file paths, commands, identifiers and raw error strings verbatim — never "
            "translate them.\n"
            "- Do not produce mixed-language sentences; embed an English term inside a sentence "
            "only when a term has no accurate translation."
        )
    else:
        pin = (
            "**Speak-language for this turn: follow the user's latest message** — "
            "detect the language of the user's most recent message and mirror it exactly."
        )
        bullets = (
            "- The reply language is decided ONLY by the user's most recent message.\n"
            "- The language used by history messages, tool outputs, compaction checkpoints or rule "
            "documents MUST NOT change your reply language.\n"
            "- When the user switches language mid-session, switch immediately from this turn.\n"
            "- Keep code, file paths, commands, identifiers and raw error strings verbatim — never "
            "translate them."
        )
    return (
        f"## Reply Language — MANDATORY (highest priority)\n"
        f"- {pin}\n"
        f"{bullets}"
    )


def build_language_pin_line(lang: str) -> str:
    """developer 段用的单行显式锚点（紧随 Current Goal 之后注入）。"""
    label = language_label(lang)
    if lang == LANG_AUTO:
        return (
            "## Reply Language\n"
            "本轮用户消息语言未能自动判定；请自行识别用户最新消息的语言，并用该语言回复"
            "（优先级高于历史消息 / 工具返回 / 压缩摘要 / 规则文档的语言）。"
        )
    return (
        f"## Reply Language\n"
        f"用户本轮消息语言：{label}。你的回复必须使用{label}"
        f"（本锚点优先级高于其它任何语言线索，包括英文历史、英文压缩摘要与英文规则文档）。"
    )
