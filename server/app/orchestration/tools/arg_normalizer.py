"""工具参数归一化（plan-20261020-pts）。

执行器在调用任何工具（含 MCP 工具）之前，按该工具的 JSON Schema 把模型给出的参数整理一遍。

为什么需要：真实失败数据里，相当一部分不是「做错了事」，而是「参数写法不对」——
沿用其它工具体系的参数名（file_path / old_string / cmd / query↔keyword）、驼峰与下划线混用、
把布尔 / 数字写成字符串、把数组整个写成 JSON 字符串、枚举值大小写不对。
此前只有编辑工具在各自内部兼容了几个别名，其余工具（尤其 MCP 工具）一律原样传入，
报出的往往是「缺少 xxx」「类型错误」之类与真实原因不对应的错误，模型照原样重试。

原则：
- 只做**无歧义**的纠正：别名改名要求 schema 里恰好只有一个候选参数、且调用里还没有它；
  候选不唯一（例如工具同时有 path 与 cwd）就不猜，原样交给工具。
- 只按 schema 声明的类型纠正，schema 没写类型的参数不动。
- 做过的纠正记成简短说明，由执行器附在结果末尾——模型在同一会话里就能学会正确写法。
- 缺少必填参数、或传了本工具不认识的参数时，不在执行前拦截（个别工具对缺参有自己的兜底），
  而是在工具失败时把完整的参数清单附到错误里，而不是只留一个含糊的报错。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

# 同义参数组：组内任一名字出现在调用里、而本工具 schema 恰好只认组内的另一个名字时，改成那个名字。
# 组与组可以重叠（如 dir 既可能是 path 也可能是 cwd），由「schema 里唯一候选」消解歧义。
ALIAS_GROUPS: tuple[tuple[str, ...], ...] = (
    ("path", "file_path", "filepath", "file", "filename", "file_name", "target_file", "target_path",
     "absolute_path", "relative_path", "dir_path", "dirpath", "directory", "dir", "folder", "folder_path"),
    ("cwd", "working_directory", "workdir", "work_dir", "working_dir", "directory", "dir", "folder"),
    ("old_text", "old_string", "old_str", "oldstring", "search", "find", "original", "original_text", "old"),
    ("new_text", "new_string", "new_str", "newstring", "replace", "replacement", "updated_text", "new"),
    ("command", "cmd", "command_line", "commandline", "shell_command", "shell"),
    ("pattern", "regex", "regexp", "search_pattern", "grep_pattern", "query"),
    ("query", "keyword", "keywords", "q", "search_query", "search_term", "term"),
    ("content", "contents", "file_content", "file_contents", "data", "body"),
    ("offset", "start_line", "start_line_one_indexed", "line_offset", "from_line", "line_start"),
    ("limit", "max_lines", "line_count", "num_lines", "max_results_count"),
    ("url", "uri", "link", "href", "web_url"),
    ("include", "glob", "file_pattern", "file_glob", "glob_pattern", "files_pattern"),
    ("todos", "todo_list", "todo_items", "tasks"),
    ("edits", "changes", "replacements", "file_edits"),
    ("recursive", "recurse"),
    ("max_depth", "depth"),
    ("timeout", "timeout_sec", "timeout_seconds", "timeout_s"),
    ("replace_all", "all", "global", "replace_all_occurrences"),
    ("case_sensitive", "match_case", "casesensitive"),
    ("context_lines", "context", "context_line_count"),
    ("summary", "result", "conclusion"),
    ("script", "expression", "js", "code", "javascript"),
)

# 结束行（含）：配合 offset 换算成 limit（Cursor 系模型惯用 start/end 行号区间）
_END_LINE_KEYS = ("end_line", "end_line_one_indexed_inclusive", "line_end", "to_line")

# 枚举同义值：只在规范值确实出现在该参数的 enum 里时才换算
ENUM_SYNONYMS: dict[str, tuple[str, ...]] = {
    "completed": ("done", "complete", "finished", "finish", "success", "succeeded", "resolved", "closed"),
    "in_progress": ("inprogress", "doing", "active", "running", "started", "ongoing", "working", "current", "wip"),
    "pending": ("todo", "notstarted", "open", "waiting", "queued", "planned", "new"),
}

_TRUE = frozenset({"true", "1", "yes", "y", "on"})
_FALSE = frozenset({"false", "0", "no", "n", "off", "none", "null", ""})
_INT_RE = re.compile(r"^[+-]?\d+$")
_NUM_RE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")
# 这些字符串参数允许把「字符串数组」拼回来（逐行内容 / 命令片段）
_JOIN_LINES = frozenset({"content", "old_text", "new_text", "text"})
_JOIN_SPACE = frozenset({"command"})


def _form(name: str) -> str:
    """参数名的比较形态：小写、去掉下划线与连字符（oldText / old_text / old-text 视为同一个）。"""
    return re.sub(r"[_\-\s]", "", str(name)).lower()


_GROUPS_BY_FORM: dict[str, list[tuple[str, ...]]] = {}
for _g in ALIAS_GROUPS:
    for _n in _g:
        _GROUPS_BY_FORM.setdefault(_form(_n), []).append(_g)


@dataclass
class NormalizeResult:
    args: dict[str, Any]
    notes: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


def _schema_of(tool: Any) -> dict:
    try:
        fn = tool.function_schema().get("function") or {}
        params = fn.get("parameters") or {}
        return params if isinstance(params, dict) else {}
    except Exception:  # noqa: BLE001 —— schema 取不到时不做任何归一化
        return {}


def _types_of(spec: dict) -> set[str]:
    t = spec.get("type")
    if isinstance(t, str):
        return {t}
    if isinstance(t, list):
        return {x for x in t if isinstance(x, str)}
    for key in ("anyOf", "oneOf"):
        alts = spec.get(key)
        if isinstance(alts, list):
            out: set[str] = set()
            for a in alts:
                if isinstance(a, dict):
                    out |= _types_of(a)
            return out
    return set()


def _coerce(name: str, value: Any, spec: dict, notes: list[str], where: str) -> Any:
    """按 schema 类型纠正单个值；纠正不了就原样返回（交给工具自己报错）。"""
    if value is None or not isinstance(spec, dict):
        return value
    types = _types_of(spec)
    if not types:
        return value
    label = f"{where}{name}"

    if "boolean" in types and not isinstance(value, bool):
        if isinstance(value, str) and value.strip().lower() in _TRUE | _FALSE:
            notes.append(f"{label} 由字符串改为布尔值")
            return value.strip().lower() in _TRUE
        if isinstance(value, (int, float)) and value in (0, 1):
            notes.append(f"{label} 由数字改为布尔值")
            return bool(value)
        return value

    if types & {"integer", "number"} and not isinstance(value, bool):
        if isinstance(value, str) and value.strip():
            s = value.strip()
            if "integer" in types and _INT_RE.match(s):
                notes.append(f"{label} 由字符串改为整数")
                return int(s)
            if _NUM_RE.match(s):
                f = float(s)
                if "integer" in types and f.is_integer():
                    notes.append(f"{label} 由字符串改为整数")
                    return int(f)
                if "number" in types:
                    notes.append(f"{label} 由字符串改为数字")
                    return f
            return value
        if isinstance(value, float) and "integer" in types and "number" not in types and value.is_integer():
            return int(value)
        return value

    if "array" in types and not isinstance(value, list):
        items = spec.get("items") if isinstance(spec.get("items"), dict) else {}
        if isinstance(value, str):
            s = value.strip()
            if s.startswith("["):
                try:
                    parsed = json.loads(s)
                except (ValueError, TypeError):
                    parsed = None
                if isinstance(parsed, list):
                    notes.append(f"{label} 由 JSON 字符串改为数组")
                    return parsed
            if "string" in _types_of(items) and s:
                notes.append(f"{label} 由单个字符串包成数组")
                return [value]
            return value
        if isinstance(value, dict) and "object" in _types_of(items):
            notes.append(f"{label} 由单个对象包成数组")
            return [value]
        return value

    if "object" in types and isinstance(value, str):
        s = value.strip()
        if s.startswith("{"):
            try:
                parsed = json.loads(s)
            except (ValueError, TypeError):
                return value
            if isinstance(parsed, dict):
                notes.append(f"{label} 由 JSON 字符串改为对象")
                return parsed
        return value

    if "string" in types and not isinstance(value, str):
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, list) and value and all(isinstance(x, str) for x in value):
            if name in _JOIN_LINES:
                notes.append(f"{label} 由字符串数组按行拼接")
                return "\n".join(value)
            if name in _JOIN_SPACE:
                notes.append(f"{label} 由字符串数组按空格拼接")
                return " ".join(value)
        return value

    return value


def _canon_enum(name: str, value: Any, spec: dict, notes: list[str], where: str) -> Any:
    enum = spec.get("enum") if isinstance(spec, dict) else None
    if not isinstance(enum, list) or not isinstance(value, str) or value in enum:
        return value
    f = _form(value)
    for opt in enum:
        if isinstance(opt, str) and _form(opt) == f:
            notes.append(f"{where}{name} 取值 {value!r} 视为 {opt!r}")
            return opt
    for canon, syns in ENUM_SYNONYMS.items():
        if canon in enum and f in {_form(s) for s in syns}:
            notes.append(f"{where}{name} 取值 {value!r} 视为 {canon!r}")
            return canon
    return value


def _normalize_object(args: dict, schema: dict, notes: list[str], unknown: list[str], where: str) -> dict:
    props = schema.get("properties")
    if not isinstance(props, dict) or not props:
        return args
    out = dict(args)
    by_form = {_form(p): p for p in props}

    for key in list(out.keys()):
        if key in props or key.startswith("_"):
            continue
        target = by_form.get(_form(key))
        if target is None:
            cands: list[str] = []
            for g in _GROUPS_BY_FORM.get(_form(key), []):
                for member in g:
                    if member in props and member not in cands:
                        cands.append(member)
            cands = [c for c in cands if c not in out]
            target = cands[0] if len(cands) == 1 else None
        if target is not None and target not in out:
            out[target] = out.pop(key)
            notes.append(f"{where}{key} 视为 {target}")

    # 起止行号 → offset + limit（只在工具确有这两个参数、且调用没给 limit 时换算）
    if "limit" in props and "limit" not in out:
        for k in _END_LINE_KEYS:
            if k in out and k not in props:
                try:
                    end = int(str(out[k]).strip())
                    start = int(str(out.get("offset") or 1).strip())
                except (TypeError, ValueError):
                    break
                if end >= start:
                    out.pop(k)
                    out["limit"] = end - start + 1
                    notes.append(f"{where}{k} 换算为 limit={end - start + 1}")
                break

    for key, value in list(out.items()):
        spec = props.get(key)
        if not isinstance(spec, dict):
            if key not in props and not key.startswith("_"):
                unknown.append(f"{where}{key}")
            continue
        value = _coerce(key, value, spec, notes, where)
        value = _canon_enum(key, value, spec, notes, where)
        items = spec.get("items")
        if isinstance(value, list) and isinstance(items, dict) and isinstance(items.get("properties"), dict):
            value = [
                _normalize_object(v, items, notes, unknown, f"{where}{key}[{i}].") if isinstance(v, dict) else v
                for i, v in enumerate(value)
            ]
        elif isinstance(value, dict) and isinstance(spec.get("properties"), dict):
            value = _normalize_object(value, spec, notes, unknown, f"{where}{key}.")
        out[key] = value
    return out


def normalize_args(tool: Any, args: Any) -> NormalizeResult:
    """按工具 schema 归一化参数。不抛异常；schema 缺失时原样返回。"""
    if not isinstance(args, dict):
        return NormalizeResult(args=args if isinstance(args, dict) else {})
    schema = _schema_of(tool)
    if not schema:
        return NormalizeResult(args=dict(args))
    notes: list[str] = []
    unknown: list[str] = []
    out = _normalize_object(args, schema, notes, unknown, "")
    required = schema.get("required") if isinstance(schema.get("required"), list) else []
    missing = [r for r in required if isinstance(r, str) and (r not in out or out[r] is None)]
    # 允许任意附加参数的 schema（additionalProperties 为真）不把未声明参数当作写错
    if schema.get("additionalProperties") is True:
        unknown = []
    return NormalizeResult(args=out, notes=notes, unknown=unknown, missing=missing)


def describe_params(tool: Any) -> str:
    """参数清单（给模型看的一行）：名称、是否必填、类型与说明摘要。"""
    schema = _schema_of(tool)
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = set(schema.get("required") or [])
    parts: list[str] = []
    for name, spec in props.items():
        spec = spec if isinstance(spec, dict) else {}
        types = "/".join(sorted(_types_of(spec))) or "任意"
        desc = str(spec.get("description") or "").strip().replace("\n", " ")
        if len(desc) > 40:
            desc = desc[:40] + "…"
        flag = "必填" if name in required else "可选"
        parts.append(f"{name}（{flag}，{types}{'：' + desc if desc else ''}）")
    return "；".join(parts)


def missing_args_error(tool: Any, missing: list[str]) -> str:
    return (
        f"缺少必填参数：{', '.join(missing)}，本次调用未执行。"
        f"{getattr(tool, 'name', '')} 的参数：{describe_params(tool)}。请按参数名补全后重新调用。"
    )


def notes_suffix(notes: list[str]) -> str:
    """成功结果末尾的纠正说明（去重、最多列 4 条）。"""
    uniq = list(dict.fromkeys(notes))
    if not uniq:
        return ""
    more = f" 等 {len(uniq)} 处" if len(uniq) > 4 else ""
    return f"\n（参数已自动纠正：{'；'.join(uniq[:4])}{more}。下次请直接使用工具定义里的参数名与类型。）"


def unknown_suffix(tool: Any, unknown: list[str]) -> str:
    """失败结果末尾的提示：调用里有本工具不认识的参数。"""
    uniq = list(dict.fromkeys(unknown))
    if not uniq:
        return ""
    return (
        f"\n注意：参数 {', '.join(uniq[:6])} 不是 {getattr(tool, 'name', '')} 的参数，已被忽略。"
        f"本工具参数：{describe_params(tool)}。"
    )
