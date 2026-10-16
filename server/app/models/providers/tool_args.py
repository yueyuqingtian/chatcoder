"""工具调用参数解析（plan-20261016-ux 问题 3）。

各供应商把模型返回的 arguments 字符串解析成 dict 时统一走 `parse_tool_args`。

为什么需要它：模型经常在 JSON 字符串里直接输出制表符 / 换行（Tab 缩进的代码最常见），
或写出 `\\'`、`\\_` 这类 JSON 不认的转义。此前各处用严格的 `json.loads`，解析失败后整份参数
变成 `{"_raw": ...}`，编辑工具拿不到 path / old_text，报出与真实原因无关的错误，
模型照原样重试 —— 用户看到的就是「同一处改动反复失败，换个工具才成功」。

解析顺序（每一步都只在前一步失败时才尝试，合法 JSON 的结果与以前完全一致）：
1. 严格 JSON；
2. 放宽控制字符（`strict=False`：字符串里的原始 Tab / 换行按字面接受）；
3. 去掉包裹的 Markdown 代码围栏；
4. 把非法转义的反斜杠本身转义（`\\'` → `\\\\'`），保留合法转义原样。
仍失败时返回带 `_parse_error` 的标记，由执行器直接回给模型一条可操作的错误（不进工具）。
"""
from __future__ import annotations

import json
import re
from typing import Any

# 解析失败标记：_raw 保存原文（便于诊断与历史回放），_parse_error 是给模型看的原因
RAW_KEY = "_raw"
PARSE_ERROR_KEY = "_parse_error"

_FENCE = re.compile(r"^\s*```(?:json|JSON)?\s*\n(.*?)\n\s*```\s*$", re.S)
# JSON 合法转义字符（反斜杠之后）
_VALID_ESCAPES = frozenset('"\\/bfnrtu')


def _fix_invalid_escapes(text: str) -> str:
    """把非法转义里的反斜杠本身转义，合法转义成对跳过（不会误伤 `\\\\'` 这类组合）。"""
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            nxt = text[i + 1]
            if nxt in _VALID_ESCAPES:
                out.append(ch)
                out.append(nxt)
                i += 2
                continue
            out.append("\\\\")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _candidates(text: str):
    yield text
    m = _FENCE.match(text)
    if m:
        text = m.group(1).strip()
        yield text
    fixed = _fix_invalid_escapes(text)
    if fixed != text:
        yield fixed


def _describe(err: Exception | None, text: str) -> str:
    if isinstance(err, json.JSONDecodeError):
        if err.pos >= len(text.rstrip()) - 1 or "Unterminated string" in err.msg:
            return "参数 JSON 不完整（输出可能被截断）"
        return f"参数不是合法 JSON（{err.msg}，第 {err.pos} 个字符附近）"
    return "参数不是合法 JSON"


def parse_tool_args(raw: Any) -> dict:
    """模型返回的工具参数 → dict。解析失败时返回 {_raw, _parse_error}，绝不抛异常。"""
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return {RAW_KEY: str(raw), PARSE_ERROR_KEY: "参数必须是 JSON 对象"}
    text = raw.strip()
    if not text:
        return {}
    last_err: Exception | None = None
    for candidate in _candidates(text):
        for strict in (True, False):
            try:
                val = json.loads(candidate, strict=strict)
            except (json.JSONDecodeError, ValueError) as e:
                last_err = e
                continue
            if isinstance(val, dict):
                return val
            return {RAW_KEY: raw, PARSE_ERROR_KEY: "参数必须是 JSON 对象"}
    return {RAW_KEY: raw, PARSE_ERROR_KEY: _describe(last_err, text)}


def args_parse_error(args: Any) -> str | None:
    """若参数是解析失败的标记，返回原因；否则 None。"""
    if isinstance(args, dict) and RAW_KEY in args and PARSE_ERROR_KEY in args:
        return str(args.get(PARSE_ERROR_KEY) or "参数不是合法 JSON")
    return None
