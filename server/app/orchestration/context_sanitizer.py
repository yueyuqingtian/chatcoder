"""上下文构造统一脱敏模块（plan-20261124-context-sanitize）。

在所有模型出站构造上下文时，对消息正文、思考文本、历史工具调用参数值以及工具描述中
携带的敏感关键词（不区分大小写）左右各自插入零宽字符（U+200B），用于上下文脱敏：

- 内置默认关键词（不区分大小写，用户可在设置中心增删改或恢复默认）：
  zcode, deepseek harness, qoder, workbuddy, codebuddy, claude code, codex,
  opencode, pi agent, pi, deepseek, glm, kimi, xiaomi
- 仅在出站副本生效：不修改持久化历史、前端展示、工具实际执行入参或协议元数据
  （如 model / headers / tool name / enum / 图片 base64 / Anthropic thinking 签名）。
- 确定性且幂等：已用零宽字符包裹的关键词不会重复叠加，保持 Prompt Cache 前缀稳定。
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Any, Iterable

from app.models.schemas import ChatMessage

ZERO_WIDTH_CHAR = "\u200b"

# 单条关键词最大长度与列表上限（防异常超长输入拖慢正则编译/匹配）
MAX_KEYWORD_LENGTH = 80
MAX_KEYWORDS_COUNT = 200

# 默认脱敏关键词清单（按自然语义分组顺序呈现给用户；编译正则时会自动按最长优先排序）
DEFAULT_CONTEXT_SANITIZE_KEYWORDS: tuple[str, ...] = (
    "zcode",
    "deepseek harness",
    "qoder",
    "workbuddy",
    "codebuddy",
    "claude code",
    "codex",
    "opencode",
    "pi agent",
    "pi",
    "deepseek",
    "glm",
    "kimi",
    "xiaomi",
)

# 兼容别名（与旧常量名保持一致）
CONTEXT_SANITIZE_KEYWORDS = DEFAULT_CONTEXT_SANITIZE_KEYWORDS

_ASCII_WORD_RE = re.compile(r"^[A-Za-z]+$")
_MULTI_SEP_RE = re.compile(r"[ \t_-]+")
_RAW_SPLIT_RE = re.compile(r"[,，;；\r\n]+")

# 结构体递归脱敏时必须跳过的非上下文/二进制/签名/标识字段名
_SKIP_STRUCTURE_KEYS: frozenset[str] = frozenset({
    "data",
    "url",
    "image_url",
    "signature",
    "thoughtSignature",
    "thought_signature",
    "media_type",
    "mime_type",
    "mimeType",
    "id",
    "tool_call_id",
    "tool_use_id",
    "toolCallId",
    "toolUseId",
    "type",
    "role",
})

# 整块跳过的媒体/加密内容块类型（防止改坏图片 base64 或 Anthropic 思考签名）
_SKIP_BLOCK_TYPES: frozenset[str] = frozenset({
    "image",
    "image_url",
    "thinking",
    "redacted_thinking",
})


def normalize_context_sanitize_keywords(raw: Any) -> list[str]:
    """归一化用户配置的脱敏关键词列表：去零宽字符/首尾空白、压多余空格、不区分大小写去重。"""
    if raw is None:
        return list(DEFAULT_CONTEXT_SANITIZE_KEYWORDS)
    items: Iterable[Any]
    if isinstance(raw, str):
        items = _RAW_SPLIT_RE.split(raw)
    elif isinstance(raw, (list, tuple, set)):
        items = raw
    else:
        return list(DEFAULT_CONTEXT_SANITIZE_KEYWORDS)

    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, str):
            continue
        cleaned = re.sub(r"[ \t]+", " ", item.replace(ZERO_WIDTH_CHAR, "")).strip()
        if not cleaned:
            continue
        cleaned = cleaned[:MAX_KEYWORD_LENGTH].strip()
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(cleaned)
        if len(result) >= MAX_KEYWORDS_COUNT:
            break
    return result


def _keyword_to_regex(keyword: str) -> str:
    """将单条关键词转为安全正则片段。

    规则：
    - 多词短语（含空格/-/_）各词间用 `[ \\t_-]*` 连接，兼容空格/连字符/下划线/驼峰写法，
      同时不跨行匹配；
    - 短纯英文字母词（长度 <= 3，如 pi / glm / ai / cli）以及以这类短词开头的短语
      （如 pi agent），在英文字母首尾自动加非字母边界 `(?<![A-Za-z])` / `(?![A-Za-z])`，
      防止误伤 FastAPI / pipeline / typing / compile / pip / api agent / glmark 等高频词，
      而中文、数字、下划线、路径分隔符均不算英文字母，可正常命中「使用pi」「GLM4」「.pi/」；
    - 所有片段均经 `re.escape` 转义，用户输入 `.` / `+` / `(` 等字符也不会破坏正则。
    """
    tokens = [tok for tok in _MULTI_SEP_RE.split(keyword) if tok]
    if len(tokens) > 1:
        body = r"[ \t_-]*".join(re.escape(tok) for tok in tokens)
        need_left = bool(_ASCII_WORD_RE.fullmatch(tokens[0]) and len(tokens[0]) <= 3)
        need_right = bool(
            need_left
            and tokens[-1]
            and tokens[-1][-1].isascii()
            and tokens[-1][-1].isalpha()
        )
        prefix = r"(?<![A-Za-z])" if need_left else ""
        suffix = r"(?![A-Za-z])" if need_right else ""
        return f"{prefix}{body}{suffix}"

    body = re.escape(keyword)
    if _ASCII_WORD_RE.fullmatch(keyword) and len(keyword) <= 3:
        return rf"(?<![A-Za-z]){body}(?![A-Za-z])"
    return body


@lru_cache(maxsize=32)
def _compile_keyword_pattern(keywords: tuple[str, ...]) -> re.Pattern[str] | None:
    """按最长优先顺序编译关键词联合正则（带缓存，未改动时零重复编译）。"""
    if not keywords:
        return None
    ordered = sorted(keywords, key=len, reverse=True)
    parts = [_keyword_to_regex(kw) for kw in ordered]
    return re.compile("|".join(parts), re.IGNORECASE)


CONTEXT_KEYWORD_RE = _compile_keyword_pattern(DEFAULT_CONTEXT_SANITIZE_KEYWORDS)


def get_active_context_sanitize_keywords() -> tuple[str, ...]:
    """读取当前生效的脱敏关键词清单（用户未自定义时回落内置默认清单）。"""
    try:
        from app.core.config import settings

        configured = getattr(settings, "context_sanitize_keywords", None)
        if configured is None:
            return DEFAULT_CONTEXT_SANITIZE_KEYWORDS
        return tuple(normalize_context_sanitize_keywords(configured))
    except Exception:  # pragma: no cover - 配置异常时回落默认清单
        return DEFAULT_CONTEXT_SANITIZE_KEYWORDS


def _active_pattern() -> re.Pattern[str] | None:
    return _compile_keyword_pattern(get_active_context_sanitize_keywords())


def is_context_sanitize_enabled() -> bool:
    """读取上下文脱敏总开关（默认开启）。"""
    try:
        from app.core.config import settings

        return bool(getattr(settings, "context_sanitize_enabled", True))
    except Exception:  # pragma: no cover - 配置异常时默认开启脱敏
        return True


def force_sanitize_context_text(text: str, *, keywords: Iterable[str] | None = None) -> str:
    """无条件对文本中的目标关键词左右各自加上零宽字符（幂等、保原大小写）。"""
    if not text or not isinstance(text, str):
        return text
    pattern = (
        _compile_keyword_pattern(tuple(normalize_context_sanitize_keywords(list(keywords))))
        if keywords is not None
        else _active_pattern()
    )
    if pattern is None:
        return text
    matches = list(pattern.finditer(text))
    if not matches:
        return text
    parts: list[str] = []
    cursor = 0
    for match in matches:
        start, end = match.span()
        if start > cursor:
            parts.append(text[cursor:start])
        # 幂等保护：左侧已有零宽字符（原文本自带或紧邻前一个匹配刚追加）则不重复插入
        has_left = bool(parts and parts[-1].endswith(ZERO_WIDTH_CHAR))
        has_right = end < len(text) and text[end] == ZERO_WIDTH_CHAR
        if not has_left:
            parts.append(ZERO_WIDTH_CHAR)
        parts.append(match.group(0))
        if not has_right:
            parts.append(ZERO_WIDTH_CHAR)
        cursor = end
    if cursor < len(text):
        parts.append(text[cursor:])
    return "".join(parts)


def sanitize_context_text(text: str) -> str:
    """按全局开关对上下文文本执行零宽字符脱敏。"""
    if not text or not isinstance(text, str):
        return text
    if not is_context_sanitize_enabled():
        return text
    return force_sanitize_context_text(text)


def has_unsanitized_context_keyword(text: str) -> bool:
    """检测文本中是否存在尚未在左右两侧都加上零宽字符的目标关键词。"""
    if not text or not isinstance(text, str):
        return False
    pattern = _active_pattern()
    if pattern is None:
        return False
    for match in pattern.finditer(text):
        start, end = match.span()
        has_left = start > 0 and text[start - 1] == ZERO_WIDTH_CHAR
        has_right = end < len(text) and text[end] == ZERO_WIDTH_CHAR
        if not (has_left and has_right):
            return True
    return False


def _sanitize_value(val: Any, *, force: bool, skip_keys: frozenset[str] = _SKIP_STRUCTURE_KEYS) -> Any:
    """递归脱敏嵌套结构中的字符串值（不改 dict 键名，跳过媒体/签名/ID 字段）。"""
    if isinstance(val, str):
        return force_sanitize_context_text(val) if force else sanitize_context_text(val)
    if isinstance(val, dict):
        btype = val.get("type")
        if isinstance(btype, str) and btype in _SKIP_BLOCK_TYPES:
            return val
        changed = False
        out: dict[Any, Any] = {}
        for k, v in val.items():
            if isinstance(k, str) and k in skip_keys:
                out[k] = v
                continue
            nv = _sanitize_value(v, force=force, skip_keys=skip_keys)
            if nv is not v:
                changed = True
            out[k] = nv
        return out if changed else val
    if isinstance(val, list):
        changed = False
        out_list: list[Any] = []
        for item in val:
            ni = _sanitize_value(item, force=force, skip_keys=skip_keys)
            if ni is not item:
                changed = True
            out_list.append(ni)
        return out_list if changed else val
    return val


def sanitize_context_structure(val: Any, *, force: bool = False) -> Any:
    """递归脱敏字典/列表/字符串中的上下文值，跳过二进制图片与签名字段。"""
    if not force and not is_context_sanitize_enabled():
        return val
    return _sanitize_value(val, force=force)


def _sanitize_content_blocks(blocks: list[dict[str, Any]], *, force: bool) -> list[dict[str, Any]]:
    """脱敏多模态 content_blocks：仅处理文本内容，跳过 image / image_url 二进制负载。"""
    changed = False
    out: list[dict[str, Any]] = []
    for block in blocks:
        if not isinstance(block, dict):
            out.append(block)
            continue
        btype = block.get("type")
        if btype in _SKIP_BLOCK_TYPES:
            out.append(block)
            continue
        if btype == "text":
            raw = block.get("text")
            if isinstance(raw, str):
                cleaned = force_sanitize_context_text(raw) if force else sanitize_context_text(raw)
                if cleaned != raw:
                    changed = True
                    out.append({**block, "text": cleaned})
                    continue
            out.append(block)
            continue
        nb = _sanitize_value(block, force=force)
        if nb is not block:
            changed = True
        out.append(nb)
    return out if changed else blocks


def _sanitize_tool_calls(tool_calls: list[dict[str, Any]], *, force: bool) -> list[dict[str, Any]]:
    """脱敏历史 tool_calls 的 arguments 字符串值；保持 id/name/type/签名与参数键名不变。"""
    changed = False
    out: list[dict[str, Any]] = []
    for tc in tool_calls:
        if not isinstance(tc, dict):
            out.append(tc)
            continue
        args = tc.get("arguments")
        new_args = args
        if isinstance(args, dict):
            new_args = _sanitize_value(
                args,
                force=force,
                skip_keys=frozenset({"signature", "thoughtSignature", "thought_signature"}),
            )
        elif isinstance(args, str) and args:
            try:
                parsed = json.loads(args)
            except (ValueError, TypeError):
                new_args = force_sanitize_context_text(args) if force else sanitize_context_text(args)
            else:
                sanitized_parsed = _sanitize_value(
                    parsed,
                    force=force,
                    skip_keys=frozenset({"signature", "thoughtSignature", "thought_signature"}),
                )
                if sanitized_parsed is not parsed:
                    new_args = json.dumps(sanitized_parsed, ensure_ascii=False)
        if new_args is not args:
            changed = True
            out.append({**tc, "arguments": new_args})
        else:
            out.append(tc)
    return out if changed else tool_calls


def sanitize_context_message(message: ChatMessage, *, force: bool = False) -> ChatMessage:
    """对单条 ChatMessage 返回脱敏后的出站副本（无变化时直接复用原对象，不原地修改）。"""
    if not force and not is_context_sanitize_enabled():
        return message
    updates: dict[str, Any] = {}
    if isinstance(message.content, str) and message.content:
        cleaned = force_sanitize_context_text(message.content) if force else sanitize_context_text(message.content)
        if cleaned != message.content:
            updates["content"] = cleaned
    if isinstance(message.reasoning_content, str) and message.reasoning_content:
        cleaned_r = (
            force_sanitize_context_text(message.reasoning_content)
            if force
            else sanitize_context_text(message.reasoning_content)
        )
        if cleaned_r != message.reasoning_content:
            updates["reasoning_content"] = cleaned_r
    if message.content_blocks:
        cleaned_blocks = _sanitize_content_blocks(message.content_blocks, force=force)
        if cleaned_blocks is not message.content_blocks:
            updates["content_blocks"] = cleaned_blocks
    if message.tool_calls:
        cleaned_calls = _sanitize_tool_calls(message.tool_calls, force=force)
        if cleaned_calls is not message.tool_calls:
            updates["tool_calls"] = cleaned_calls
    if not updates:
        return message
    return message.model_copy(update=updates)


def sanitize_context_messages(messages: list[ChatMessage], *, force: bool = False) -> list[ChatMessage]:
    """对出站消息列表执行零宽字符脱敏（绝不原地修改输入列表或消息对象）。"""
    if not messages or (not force and not is_context_sanitize_enabled()):
        return messages
    changed = False
    out: list[ChatMessage] = []
    for m in messages:
        nm = sanitize_context_message(m, force=force)
        if nm is not m:
            changed = True
        out.append(nm)
    return out if changed else messages


def _sanitize_schema_descriptions(val: Any, *, force: bool) -> Any:
    """递归脱敏工具定义中的 description 自然语言字段；不动 name/enum/properties 键名等契约字段。"""
    if isinstance(val, dict):
        changed = False
        out: dict[Any, Any] = {}
        for k, v in val.items():
            if k == "description" and isinstance(v, str):
                nv = force_sanitize_context_text(v) if force else sanitize_context_text(v)
            elif k in ("properties", "$defs", "definitions", "patternProperties") and isinstance(v, dict):
                # 这些容器的键名是参数名/定义名（协议契约），保持键名不变，对其每个子 schema 递归
                prop_changed = False
                new_props: dict[Any, Any] = {}
                for pk, pv in v.items():
                    npv = _sanitize_schema_descriptions(pv, force=force)
                    if npv is not pv:
                        prop_changed = True
                    new_props[pk] = npv
                nv = new_props if prop_changed else v
            elif k in ("function", "parameters", "input_schema", "items", "additionalProperties",
                       "anyOf", "oneOf", "allOf"):
                nv = _sanitize_schema_descriptions(v, force=force)
            else:
                nv = v
            if nv is not v:
                changed = True
            out[k] = nv
        return out if changed else val
    if isinstance(val, list):
        changed = False
        out_list: list[Any] = []
        for item in val:
            ni = _sanitize_schema_descriptions(item, force=force)
            if ni is not item:
                changed = True
            out_list.append(ni)
        return out_list if changed else val
    return val


def sanitize_context_tools(
    tools: list[dict[str, Any]] | None,
    *,
    force: bool = False,
) -> list[dict[str, Any]] | None:
    """脱敏工具定义列表中的自然语言 description，保持工具名/参数名/enum 等协议字段不变。"""
    if not tools or (not force and not is_context_sanitize_enabled()):
        return tools
    return _sanitize_schema_descriptions(tools, force=force)
