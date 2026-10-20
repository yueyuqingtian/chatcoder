"""browser_evaluate 的脚本包装（plan-20261020-pts）。

两个宿主（桌面内置浏览器的 executeJavaScript、后台浏览器的 page.evaluate）都把脚本当作
**普通脚本**执行、返回最后一个表达式的值。模型写的脚本常见三种形态，原样执行全部会失败：

1. 顶层 `return` / `await`（把脚本当函数体写）→ 语法错误；
2. 顶层 `const list = …`——同一页面第二次执行时报「重复声明」（脚本级 const 落在全局词法作用域）；
3. 运行时出错——宿主只回一句固定英文（Script failed to execute），被翻译成「页面可能正在跳转」，
   模型据此反复重试同一段脚本。真实库里近期 browser_evaluate 失败 10/121 全是这三类。

包装规则：
- 顶层没有 return / await：包进 `try { … } catch`。try 语句的值就是块内最后一个表达式的值，
  块内的 const / let 不会泄漏到全局，运行时错误被捕获成 {__ccEvalError: "类型: 信息"} 带回；
- 顶层有 return / await：包成异步函数体执行；只有 await 没有 return 时，给最后一个表达式补 return；
- 整段就是一个函数表达式（`() => …` / `function () {…}`）：先调用它。
包装本身不经过 eval / new Function，不受页面内容安全策略（CSP）限制。
"""
from __future__ import annotations

import json
import re

ERROR_KEY = "__ccEvalError"

_FUNC_EXPR = re.compile(r"^\s*(?:async\s+)?(?:function\b|\([^()]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>)")
_NO_RETURN_LEAD = re.compile(
    r"^(?:const|let|var|if|for|while|do|switch|try|function|class|return|throw|break|continue|import|export)\b|^[{}]"
)
_CONTINUATION_END = ("=", ",", "(", "[", "{", "+", "-", "*", "/", "%", "&&", "||", "??", "?", ":", "=>", ".", "!")
_CONTINUATION_START = (".", "?.", ")", "]", "}", ",", "+", "-", "*", "/", "%", "&&", "||", "??", "?", ":")


def _scan(script: str) -> tuple[bool, bool, bool, list[int]]:
    """扫描 return / await 关键字与顶层（括号层级 0）语句分隔位置。

    返回 (出现 return, 出现 await, 有顶层 return, 顶层语句起点候选)。
    「出现」不分层级：写在顶层 if / for 块里的 return 同样不能出现在脚本顶层，必须按函数体执行。
    只做到够用的词法：识别引号字符串、模板字符串（含 ${} 嵌套）、行 / 块注释，不识别正则字面量。
    """
    any_return = any_await = top_return = False
    starts: list[int] = [0]
    depth = 0
    i, n = 0, len(script)
    tmpl: list[int] = []  # 模板字符串里 ${ 所在的括号层级
    while i < n:
        ch = script[i]
        nxt = script[i + 1] if i + 1 < n else ""
        if ch == "/" and nxt == "/":
            j = script.find("\n", i)
            i = n if j < 0 else j
            continue
        if ch == "/" and nxt == "*":
            j = script.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if ch in ("'", '"'):
            j = i + 1
            while j < n and script[j] != ch:
                j += 2 if script[j] == "\\" else 1
            i = j + 1
            continue
        if ch == "`" or (ch == "}" and tmpl and tmpl[-1] == depth):
            if ch == "}":
                tmpl.pop()
            j = i + 1
            while j < n:
                if script[j] == "\\":
                    j += 2
                    continue
                if script[j] == "`":
                    break
                if script[j] == "$" and j + 1 < n and script[j + 1] == "{":
                    tmpl.append(depth)
                    j += 1
                    break
                j += 1
            i = j + 1
            continue
        if ch in "({[":
            depth += 1
        elif ch in ")}]":
            depth = max(0, depth - 1)
            if depth == 0 and ch == "}":
                starts.append(i + 1)
        elif depth == 0 and ch in ";\n":
            starts.append(i + 1)
        elif ch.isalpha() or ch in "_$":
            j = i
            while j < n and (script[j].isalnum() or script[j] in "_$"):
                j += 1
            word = script[i:j]
            prev = script[:i].rstrip()[-1:] if script[:i].strip() else ""
            if prev != ".":
                if word == "return":
                    any_return = True
                    top_return = top_return or depth == 0
                elif word == "await":
                    any_await = True
            i = j
            continue
        i += 1
    return any_return, any_await, top_return, starts


def _with_trailing_return(script: str, starts: list[int]) -> str | None:
    """给最后一条顶层语句补 return；拿不准（声明、控制语句、跨行续写）时返回 None。"""
    body = script.rstrip().rstrip(";").rstrip()
    for pos in sorted(set(starts), reverse=True):
        if pos >= len(body):
            continue
        tail = body[pos:].strip()
        if not tail:
            continue
        head = body[:pos].rstrip()
        if tail.startswith(_CONTINUATION_START) or (head and head.endswith(_CONTINUATION_END)):
            continue  # 这一行是上一行的续写，继续往前找语句起点
        if _NO_RETURN_LEAD.match(tail):
            return None
        lead = body[:pos] + body[pos:].replace(tail, "return " + tail, 1)
        return lead
    return None


def wrap_script(script: str) -> tuple[str, str]:
    """返回 (包装后的脚本, 说明)。说明为空表示按原样语义执行（只加了错误捕获）。"""
    src = script.strip()
    note = ""
    if _FUNC_EXPR.match(src) and not re.search(r";\s*\S", src.rstrip(";")):
        src = f"({src})()"
        note = "函数表达式已自动调用"
    any_return, any_await, top_return, starts = _scan(src)
    err = (
        "(__cc_e && __cc_e.name ? __cc_e.name + ': ' : '') + "
        "(__cc_e && __cc_e.message ? __cc_e.message : String(__cc_e))"
    )
    if any_return or any_await:
        body = src
        if not top_return:
            # 没有顶层 return（return 只在内层函数或块里、或只有 await）：给最后一条语句补 return，
            # 结果与「返回最后一个表达式的值」一致
            patched = _with_trailing_return(src, starts)
            if patched is not None:
                body = patched
            elif not any_return:
                note = (note + "；" if note else "") + "脚本没有 return，结果为空；需要取值请用 return 返回"
        wrapped = (
            "(async () => {\n"
            f"try {{\n{body}\n}} catch (__cc_e) {{ return {{ {ERROR_KEY}: {err} }}; }}\n"
            "})()"
        )
        return wrapped, note
    wrapped = f"try {{\n{src}\n}} catch (__cc_e) {{ ({{ {ERROR_KEY}: {err} }}); }}"
    return wrapped, note


def eval_error_of(value: object) -> str | None:
    """宿主返回值里带回的运行时错误（包装捕获）；没有错误时返回 None。"""
    if isinstance(value, dict) and ERROR_KEY in value:
        return str(value.get(ERROR_KEY) or "未知错误")
    return None


def describe(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)
