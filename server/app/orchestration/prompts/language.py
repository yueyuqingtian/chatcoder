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


_FENCE_RE = re.compile(r"```[\s\S]*?```")
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
_URL_RE = re.compile(r"https?://\S+|www\.\S+")
_PATH_RE = re.compile(r"(?:[A-Za-z]:[\\/]|[\\/])?(?:[\w.-]+[\\/])+[\w.-]+")
# 只去掉标识符形态（snake_case / camelCase / 点分名），保留普通英文单词。
_IDENT_RE = re.compile(
    r"\b(?:"
    r"[A-Za-z]+_[A-Za-z0-9_]+"
    r"|[a-z]+(?:[A-Z][A-Za-z0-9]+)+"
    r"|[A-Z][a-z0-9]+(?:[A-Z][A-Za-z0-9]+)+"
    r"|[A-Za-z][\w-]*\.[\w.-]+"
    r")\b"
)


def _prose_for_language(text: str) -> str:
    """去掉代码、路径和标识符，只留自然语言，避免英文路径把中文判成英文。"""
    cleaned = _FENCE_RE.sub(" ", text)
    cleaned = _INLINE_CODE_RE.sub(" ", cleaned)
    cleaned = _URL_RE.sub(" ", cleaned)
    cleaned = _PATH_RE.sub(" ", cleaned)
    return _IDENT_RE.sub(" ", cleaned)


def detect_reply_language(text: str | None) -> str:
    """按用户消息文本判定回复语言，返回 "zh" / "en" / "auto"。

    规则（轻量、无依赖）：
    - 先去掉围栏代码、行内代码、路径、URL 和标识符
    - 剩余 CJK >= _MIN_SIGNAL → "zh"
    - 剩余字母 >= _MIN_SIGNAL 且没有足够 CJK → "en"
    - 其余（纯数字/符号/空/纯代码）→ "auto"
    """
    if not text:
        return LANG_AUTO
    prose = _prose_for_language(text)
    cjk = len(_CJK_RE.findall(prose))
    latin = len(_LATIN_RE.findall(prose))
    if cjk >= _MIN_SIGNAL:
        return LANG_ZH
    if latin >= _MIN_SIGNAL and cjk < _MIN_SIGNAL:
        return LANG_EN
    return LANG_AUTO


_RULE_LANG_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(必须|强制|一律|始终|全局).{0,12}(简体中文|中文回复|用中文|使用中文)"), LANG_ZH),
    (re.compile(r"(全局使用中文|回复必须使用简体中文|回复用中文)"), LANG_ZH),
    (re.compile(r"(always|must|reply|respond).{0,24}(in Chinese|in 中文)", re.I), LANG_ZH),
    (re.compile(r"(必须|强制|一律|始终).{0,16}(英文回复|用英文|使用英文|reply in English)", re.I), LANG_EN),
    (re.compile(r"(always|must).{0,24}(reply|respond).{0,12}in English", re.I), LANG_EN),
)


def extract_rule_language(text: str | None) -> str:
    """从规则正文提取明确语言要求。普通英文正文不算语言要求。

    同一段出现互相矛盾的明确要求时，取最后一次出现的要求。
    """
    if not text:
        return LANG_AUTO
    found = LANG_AUTO
    for line in text.splitlines():
        for pattern, lang in _RULE_LANG_PATTERNS:
            if pattern.search(line):
                found = lang
    return found


def resolve_reply_language(
    user_lang: str,
    *,
    global_rules: str = "",
    project_rules: str = "",
    workdir_rules: str = "",
) -> tuple[str, str]:
    """裁决最终回复语言。

    返回 (lang, source)。source 为 global / project / user。
    全局规则 > 项目规则文档与设置中心项目规则 > 用户本轮消息。
    """
    global_lang = extract_rule_language(global_rules)
    if global_lang != LANG_AUTO:
        return global_lang, "global"
    project_lang = extract_rule_language(project_rules)
    workdir_lang = extract_rule_language(workdir_rules)
    if project_lang != LANG_AUTO and workdir_lang != LANG_AUTO and project_lang != workdir_lang:
        import logging
        logging.getLogger(__name__).info(
            "[language] 项目规则语言冲突：文档=%s，设置项目规则=%s，取设置项目规则",
            project_lang, workdir_lang,
        )
        return workdir_lang, "project"
    if workdir_lang != LANG_AUTO:
        return workdir_lang, "project"
    if project_lang != LANG_AUTO:
        return project_lang, "project"
    return user_lang or LANG_AUTO, "user"


def language_label(lang: str) -> str:
    """语言码 → 人类可读语言名（用于锚点文案）。"""
    return {
        LANG_ZH: "简体中文",
        LANG_EN: "English",
    }.get(lang or LANG_AUTO, "与用户本轮消息相同的语言")


def build_language_directive(lang: str, source: str = "user") -> str:
    """产出语言纪律块（系统提示首尾双锚）。

    source=global/project 时规则语言高于用户消息；source=user 时只跟随用户消息。
    """
    label = language_label(lang)
    ruled = source in ("global", "project")
    if lang == LANG_ZH:
        pin = (
            "**本轮锚定语言：简体中文（zh）**——你的全部输出必须使用简体中文。"
        )
        who = (
            "设置中心全局规则明确要求简体中文，高于用户本轮消息语言。"
            if source == "global" else
            "项目规则明确要求简体中文，高于用户本轮消息语言。"
            if source == "project" else
            "本轮没有规则语言要求，回复语言由用户最新一条消息决定。"
        )
        bullets = (
            f"- {who}当前回复语言：{label}。\n"
            f"- 历史消息、工具返回、压缩摘要（checkpoint）、规则文档的书写语言，都**不得**改变回复语言。\n"
            f"- {'用户改用英文也不能覆盖这条规则语言。' if ruled else '同一会话内用户切换语言时，从本轮起立即切换，不沿用上一轮语言。'}\n"
            f"- 从第一条输出起（含工具调用前的说明文字）就必须使用{label}，每一步、每一句话都不例外。\n"
            f"- 语言纪律是**内部约束**，不是回复内容：不得在正文里提及、声明、标注或复述语言要求"
            f"（「（{label}）」「保持{label}」这类标注一律不准出现在回复中），直接输出实质内容。\n"
            f"- 代码、文件路径、命令、标识符、报错原文一律保留原样，**不要翻译**。\n"
            f"- 不得因为「术语用英文更准确」而输出中英混杂的句子；若必须引用英文术语，嵌入中文句中即可。"
        )
    elif lang == LANG_EN:
        pin = (
            "**Speak-language for this turn: English (en)** — every part of your output "
            "MUST be written in English."
        )
        who = (
            "The settings-center global rules explicitly require English, above the user's message."
            if source == "global" else
            "Project rules explicitly require English, above the user's message."
            if source == "project" else
            "No rule sets a reply language, so it follows the user's latest message."
        )
        bullets = (
            f"- {who} Current reply language: {label}.\n"
            "- History, tool outputs, compaction checkpoints and the writing language of rule "
            "documents MUST NOT change the reply language.\n"
            + ("- A user message in another language does not override this rule language.\n"
               if ruled else
               "- When the user switches language, switch immediately from this turn.\n")
            + "- From your very first output (including any preamble before a tool call) write in "
            "English — every step and every sentence.\n"
            + "- This language rule is internal, not reply content: never mention, restate or tag it "
            "in your reply (no \"(English)\", no \"keeping English\") — just answer.\n"
            + "- Keep code, file paths, commands, identifiers and raw error strings verbatim — never "
            "translate them.\n"
            + "- Do not produce mixed-language sentences; embed an English term inside a sentence "
            "only when a term has no accurate translation."
        )
    else:
        pin = (
            "**Speak-language for this turn: follow an explicit language rule if present; "
            "otherwise follow the user's latest message.**"
        )
        bullets = (
            "- An explicit language requirement in settings-center global rules takes priority, "
            "followed by project rules; otherwise mirror the user's latest message.\n"
            "- The writing language of history, tools, summaries or rule documents does not itself "
            "set the reply language.\n"
            "- When the user switches language and no rule overrides it, switch from this turn.\n"
            "- From your first output on (including any preamble before a tool call), use that language "
            "consistently — every step.\n"
            "- The language rule is internal, not reply content: never mention, restate or tag it in "
            "your reply — just answer.\n"
            "- Keep code, file paths, commands, identifiers and raw error strings verbatim — never "
            "translate them."
        )
    return (
        f"## Reply Language — MANDATORY (highest priority)\n"
        f"- {pin}\n"
        f"{bullets}"
    )


def build_language_pin_line(lang: str, source: str = "user") -> str:
    """developer 段用的单行显式锚点（紧随 Current Goal 之后注入）。"""
    label = language_label(lang)
    # 语言要求是内部纪律，必须显式禁止复述——否则模型会把锚点原文抄进正文
    # （用户反馈：回复里会冒出「（简体中文）」「保持简体中文」这类语言标注）。
    ban = (
        "Do not mention or restate this language requirement in your reply."
        if lang == LANG_EN else
        "不要在回复正文里提及、标注或复述语言要求。"
    )
    if source == "global":
        return (
            f"## Reply Language\n"
            f"本轮回复语言由设置中心全局规则决定：{label}。"
            f"用户消息、历史、工具结果和规则文档的书写语言都不得覆盖它。"
            f"{ban}"
        )
    if source == "project":
        return (
            f"## Reply Language\n"
            f"本轮回复语言由项目规则决定：{label}。"
            f"用户消息、历史、工具结果和规则文档的书写语言都不得覆盖它。"
            f"{ban}"
        )
    if lang == LANG_AUTO:
        return (
            "## Reply Language\n"
            "本轮没有规则语言要求，用户消息语言也未能自动判定；"
            "请自行识别用户最新消息的语言，并用该语言回复。"
            "不要在回复正文里提及或标注语言要求。"
        )
    return (
        f"## Reply Language\n"
        f"本轮没有规则语言要求。用户本轮消息语言：{label}。你的回复必须使用{label}。"
        f"历史、工具结果、压缩摘要和规则文档的书写语言不得改变它。"
        f"{ban}"
    )


# ---------------------------------------------------------------------------
# 周期性重申（对齐 ZCode runtime-reminders 的 sparse / full 双层提醒）
#
# ZCode 不依赖"系统提示里的一次性大锚"，而是每 N 个轮次重新注入一次运行态提醒
# （TURNS_BETWEEN_ATTACHMENTS=5，每 5 次给一次全量版）。本项目此前语言纪律只在
# 上下文构建时静态注入一次，长 turn 内锚点随步骤稀释后不再重申 —— 这是计划模式
# 确认执行后语言漂移的直接原因。以下工厂产出**贴近动作点**的轻量重申文本。
# ---------------------------------------------------------------------------

def build_language_reminder(lang: str, source: str = "user") -> str:
    """周期性语言重申正文（注入为 system 消息，贴近当次模型调用）。

    刻意保持短句：重申的价值来自频率而非篇幅，过长会挤占上下文预算。
    正文语言跟随锚定语言——英文会话注入英文提醒，避免中文骨架本身成为
    "中英混杂"的示范（对齐 ZCode 提醒文案随会话语言生成的做法）。
    auto 时不做语言锚定，只给中性的镜像表述。
    """
    if lang == LANG_EN:
        why = (
            "required by the settings-center global rules"
            if source == "global" else
            "required by project rules"
            if source == "project" else
            "the user's latest message, because no rule sets a language"
        )
        return (
            "[Language reminder] This turn's reply language is still English — "
            f"{why}. History, tool outputs, compaction summaries and the writing language of "
            "rule documents must not change it. Keep code, paths and identifiers verbatim. "
            "The requirement is internal: never mention or tag the language in your reply."
        )
    label = language_label(lang)
    if lang == LANG_AUTO:
        return (
            "[语言重申] 当前没有确定的语言信号与规则语言要求；请识别用户最新一条消息的语言并用该语言回复。"
            "历史消息、工具返回、压缩摘要与规则文档的书写语言不得改变回复语言。"
            "本条属内部约束：不要在正文里提及或标注语言要求。"
        )
    why = (
        "由设置中心全局规则决定，高于用户消息"
        if source == "global" else
        "由项目规则决定，高于用户消息"
        if source == "project" else
        "没有规则语言要求，以用户最新一条消息为准"
    )
    return (
        f"[语言重申] 本轮回复语言仍是{label}——{why}。"
        f"历史、工具返回、压缩摘要和规则文档的书写语言不得改变它。"
        f"代码、路径、命令、标识符和报错原文保留原样。"
        f"本条属内部约束：不要在正文里提及、标注或复述语言要求。"
    )


def build_plan_exit_reminder(lang: str, source: str = "user") -> str:
    """计划模式退出（用户确认执行）边界提醒（对齐 ZCode PLAN_MODE_EXIT_REMINDER）。

    这是本会话中"上下文形态切换最剧烈"的一刻：规划阶段的对话被换成整篇方案文档，
    模型最容易在此处沿用英文惯性。因此在边界处显式声明状态切换 + 重申语言。
    """
    if lang == LANG_EN:
        return (
            "## Exited Plan Mode\n\n"
            "The user approved the plan document, so you are now in the **execution phase**: "
            "you may edit files, run commands and commit changes. The research checklist from "
            "the planning phase has been cleared — rebuild an execution checklist from the plan "
            "document and work through it step by step.\n\n"
            "[Language reminder] The current reply language is English. "
            + ("It is required by the settings-center global rules; the user's message cannot override it. "
               if source == "global" else
               "It is required by project rules; the user's message cannot override it. "
               if source == "project" else
               "No rule sets a language, so it follows the user's latest message. ")
            + "The plan document, history messages and tool outputs do not change it. "
            "Never mention or restate the language requirement in your reply."
        )
    label = language_label(lang)
    body = (
        "## Exited Plan Mode\n\n"
        "用户已确认方案文档，你现在处于**执行阶段**：可以编辑文件、运行命令、提交改动。"
        "规划阶段的调研清单已清空，请按方案文档重建执行清单后逐步推进。"
    )
    if lang == LANG_AUTO:
        return body + (
            "\n\n[语言重申] 当前回复语言由明确的语言规则决定；没有语言规则时，跟随用户最新一条消息。"
            "方案文档与系统提示的书写语言不得改变它。不要在正文里提及或标注语言要求。"
        )
    source_text = (
        "由设置中心全局规则指定，用户消息语言不能覆盖。"
        if source == "global" else
        "由项目规则指定，用户消息语言不能覆盖。"
        if source == "project" else
        "没有规则语言要求，跟随用户本轮消息。"
    )
    return body + (
        f"\n\n[语言重申] 本轮回复语言是{label}；{source_text}"
        f"正文、进度汇报和 todo_write 的 content/activeForm 都使用{label}。"
        f"方案文档、历史消息和工具结果的书写语言不构成切换理由。"
        f"语言纪律属内部约束：不要在正文里提及、标注或复述语言要求。"
    )


def build_rules_reminder(lang: str) -> str:
    """规则遵循重申正文（对齐 ZCode 对 AGENTS.md 的 OVERRIDE 裁决语义）。

    长上下文下规则段会被大量工具结果挤出注意力范围，这里周期性把它拉回视野，
    并复用与系统提示一致的冲突优先级口径，避免模型"临时自创"规则顺序。
    正文语言跟随锚定语言（en → 英文，其余 → 中文）。
    """
    if lang == LANG_EN:
        return (
            "[Rules reminder] The injected user rules still apply this turn, in this priority: "
            "settings-center global rules > project rule documents (AGENTS.md / CLAUDE.md / "
            ".cursorrules, etc.) and settings-center project rules > the user's message language > "
            "the built-in methodology. Those rules OVERRIDE default "
            "behavior and must be followed as written; "
            "if you must deviate, say why and get the user's agreement instead of silently ignoring "
            "them. They remain in force after the context grows or gets compacted — they do not "
            "expire by becoming \"earlier in the context\"."
        )
    return (
        "[规则重申] 本轮仍受已注入的用户规则约束，按此优先级执行："
        "设置中心全局规则 > 项目规则文档（AGENTS.md / CLAUDE.md / .cursorrules 等）"
        "与设置中心项目规则 > 用户本轮消息语言 > 内置方法论。"
        "这些规则覆盖默认行为，必须原样遵守；需要偏离时先说明原因并征得用户同意，"
        "不要静默忽略。上下文变长或压缩后，规则依然有效——它们不会因为"
        "「较早出现在上下文里」而失效。"
    )


def build_rules_anchor(
    lang: str,
    project_docs: list[str] | None = None,
    language_source: str = "user",
) -> str:
    """本轮规则锚点（跟随每条用户消息一起下发）。

    与 `build_rules_reminder` 的分工：
      * `build_rules_reminder` 由 agent_loop 按**步数间隔**注入（默认 20 步一次），
        用于长 turn 中途把规则拉回注意力范围；
      * 本函数在每个**用户消息**上注入一次（位置就在本轮指令之前），保证"用户一开口，
        规则就在眼前"——用户反馈"规则遵循度不够"的直接对症项。
        两者叠加覆盖"轮次边界"与"长 turn 中段"两个注意力洼地。

    project_docs：本轮**实际加载**的项目规则文档相对路径清单（由 rules_loader
    同源产出）。列出真实文件名有两个作用：模型知道去哪个文件核对规则；文件缺失时
    也能看出"没扫到规则"而不是误以为已遵守。

    正文语言跟随锚定语言（en → 英文，其余 → 中文）。
    """
    docs = [d for d in (project_docs or []) if d]
    if lang == LANG_EN:
        head = (
            "[Rules anchor] Before acting on the user message below, re-read the injected rules "
            "and follow them as written this turn, in this priority: settings-center global rules > "
            "project rule documents and settings-center project rules > the user's message language > "
            "the built-in methodology. If the selected language comes from a rule, the user message "
            "cannot override that language. Rules OVERRIDE defaults; if you must deviate, explain why "
            "and get the user's agreement instead of silently ignoring them."
        )
        if docs:
            return head + (
                "\nProject rule documents loaded from the workspace this turn: "
                + ", ".join(docs)
                + ". Read the relevant ones and keep working inside their conventions."
            )
        return head + (
            "\nNo project rule document was detected in this workspace — follow the existing "
            "code style and directory structure instead of inventing new conventions."
        )
    head = (
        "[规则锚点] 处理下方用户消息前，请重新对照已注入的规则并原样遵循，优先级："
        "设置中心全局规则 > 项目规则文档与设置中心项目规则 > 用户本轮消息语言 > 内置方法论。"
        f"本轮语言由{'全局规则' if language_source == 'global' else '项目规则' if language_source == 'project' else '用户本轮消息'}决定；"
        "规则明确指定语言时，用户本轮消息语言不能覆盖。规则覆盖默认行为；需要偏离时先说明原因并征得用户同意，不要静默忽略。"
    )
    if docs:
        return head + (
            "\n本轮已从工作区加载的项目规则文档：" + "、".join(docs)
            + "。请先阅读相关文档，并在其约定内推进工作。"
        )
    return head + (
        "\n本轮未在工作区检测到项目规则文档——请沿用既有代码风格与目录结构，"
        "不要自创新的约定。"
    )
