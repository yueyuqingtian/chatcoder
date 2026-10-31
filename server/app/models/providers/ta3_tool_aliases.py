"""工具名伪装映射（ta3 模式专用，plan-598-2482 重写，plan-601 参数还原对齐）。

目标：ta3 会话上行的工具面与官方客户端（Ta+3 牛码 0.7.5）保持同一体系。
官方在普通 agent 会话里只下发内置工具 + MCP 外部工具（见
`ta3_official_tools.TA3_OFFICIAL_BUILTIN_TOOLS`），因此映射拆成两张表：

- `TO_TA3`：本项目真实名 → **官方内置工具名**（逐字一致，含 snake_case 的那些）；
- `TO_TA3_EXTENSION`：本项目独有工具 → 拓展 MCP 工具名 `mcp__{serverId}__{真实名}`。

参数换算分两层：
1. `ARGS_TO_TA3` / `ARGS_FROM_TA3`：纯键名改写（`tool_ref` 等只需键名的场景也用它）；
2. `disguise_args` / `restore_args`：在键名改写之上做**值级还原**——官方参数面与真实工具
   能力不一一对应（ask_user 的对象数组选项、generate_project_memory 的 memories 数组、
   Diff 无参数却要完整 diff……），plan-601 要求模型按官方 schema 传参时真实工具照样能执行，
   历史回放时真实参数也能换算回官方形态，与当轮 schema 一致。
"""
from __future__ import annotations

import copy

from app.models.providers.ta3_mcp import EXTENSION_SERVER_ID, ta3_mcp_name
from app.models.providers.ta3_official_tools import TA3_OFFICIAL_BUILTIN_TOOLS

# ─────────────────── 一、官方内置工具映射 ───────────────────
# 真实执行名 → 官方 function.name。
# 键必须落在 TA3_OFFICIAL_BUILTIN_TOOLS 里，由 tests 守护（漏配/多配都会失败）。
TO_TA3: dict[str, str] = {
    # 文件与检索
    "fs_read": "Read",                       # path→filepath
    "fs_list": "List",                       # path→dirPath
    "fs_grep": "Search",                     # pattern→query
    "git_diff": "Diff",                      # 官方无参数；入站补 stat_only=false
    "fs_write": "Write",                     # path→filepath，mode 透传
    "editor_apply_diff": "Edit",             # path/old_text/new_text → filepath/oldString/newString
    "outline": "get_file_outline",           # path→filepath
    # 项目记忆
    "memory_search": "get_project_memory",   # keyword→query（'all' → 全量列表）
    "memory_write": "generate_project_memory",  # text/kind/scope ↔ memories[]
    # 网络与终端
    "web_search": "WebSearch",               # engine/max_results ↔ searchEngine/maxResults
    "terminal_exec": "Bash",                 # 入站丢 purpose；出站补 purpose
    # 任务与交互
    "todo_write": "TodoWrite",
    "ask_user_question": "ask_user",         # options 字符串 ↔ {label,value,description}
    # 技能
    "skill_view": "ReadSkill",               # name→skillName
    # 子代理
    "spawn_subagent": "SubAgent",            # task_title/task_description ↔ description/prompt
}

# 入站额外别名：官方名字 → 本项目真实名。
# 覆盖「出站不用但模型可能按官方习惯调用」的名字，以及历史会话回放。
FROM_TA3_EXTRA: dict[str, str] = {
    # 官方的单文件精确替换工具，语义等价于本项目的 editor_apply_diff
    "single_find_and_replace": "editor_apply_diff",
    # 官方 SubAgent 之外的旧回放名（历史会话里可能残留）
    "SubAgentAsync": "spawn_subagent",
    # 官方 TaskQuery / TaskCancel 与本项目子代理查询/取消同义（历史回放）
    "TaskQuery": "collect_results",
    "TaskCancel": "cancel_subagent",
}

# ─────────────────── 二、拓展工具映射（走 MCP 通道） ───────────────────
_EXTENSION_REAL_NAMES: tuple[str, ...] = (
    # 浏览器套件
    "browser_navigate", "browser_screenshot", "browser_click", "browser_type",
    "browser_snapshot", "browser_evaluate", "browser_tabs", "browser_get_text",
    "browser_press", "browser_scroll", "browser_hover", "browser_drag",
    "browser_fill", "browser_clipboard", "browser_wait", "browser_logs",
    "browser_batch", "browser_downloads", "browser_recipe",
    # 电脑操控
    "desktop_windows", "desktop_snapshot", "desktop_screenshot", "desktop_click",
    "desktop_type", "desktop_keys", "desktop_scroll", "desktop_batch",
    "desktop_focus", "desktop_apps", "desktop_recipe",
    # 附件与图片
    "read_attachment", "view_image",
    # 后台进程管理（Bash 的配套能力，官方无独立工具）
    "terminal_bg_status", "terminal_bg_kill",
    # 编辑与检索增强
    "multi_file_edit", "symbol_search", "codebase_search", "feature_lookup",
    "symbol_index_enable",
    # 网络与命令
    "web_fetch", "ci_run", "git",
    # 上下文与记忆
    "compaction_index", "compaction_view",
    # 子代理管理
    "collect_results", "cancel_subagent", "subagent_inspect",
    "send_to_subagent", "resume_subagent", "report_to_leader",
    # 会话目标
    "goal_complete",
    # 工作流
    "plan_workflow", "start_workflow", "workflow_status", "resume_workflow",
    "cancel_workflow", "amend_workflow", "decide_workflow",
    # 后续任务建议
    "suggest_next_task",
)

TO_TA3_EXTENSION: dict[str, str] = {
    name: ta3_mcp_name(EXTENSION_SERVER_ID, name) for name in _EXTENSION_REAL_NAMES
}

# 伪装名 → 真实执行名（反查）。
FROM_TA3: dict[str, str] = {v: k for k, v in TO_TA3.items()}
FROM_TA3.update({v: k for k, v in TO_TA3_EXTENSION.items()})
FROM_TA3.update(FROM_TA3_EXTRA)

# 出站参数键名适配：真实键 → ta3 键（None = 丢弃该键）
ARGS_TO_TA3: dict[str, dict[str, str | None]] = {
    "fs_read": {"path": "filepath"},
    "fs_list": {"path": "dirPath"},
    "fs_grep": {"pattern": "query"},
    "fs_write": {"path": "filepath"},
    "memory_search": {"keyword": "query"},
    "read_file_range": {"path": "filepath"},
    "outline": {"path": "filepath"},
    "editor_apply_diff": {
        "path": "filepath",
        "old_text": "oldString",
        "new_text": "newString",
        "replace_all": "replaceAll",
    },
    "web_search": {
        "engine": "searchEngine",
        "max_results": "maxResults",
    },
    # 官方 SubAgent 的参数面：description + prompt + subagent_type + inherit_context。
    # explore / mode 由 subagent_type 表达，acceptance_criteria 并入 prompt（见 disguise_args）。
    "spawn_subagent": {
        "task_title": "description",
        "task_description": "prompt",
        "acceptance_criteria": None,
        "explore": None,
        "mode": None,
        "purpose": None,
    },
    "skill_view": {"name": "skillName"},
}

# 入站参数键名适配：ta3 键 → 真实键（None = 丢弃该键）
ARGS_FROM_TA3: dict[str, dict[str, str | None]] = {
    "Read": {"filepath": "path", "document": None, "docx": None},
    "List": {"dirPath": "path"},
    "Search": {"query": "pattern"},
    "Write": {"filepath": "path"},
    "Edit": {
        "filepath": "path",
        "oldString": "old_text",
        "newString": "new_text",
        "replaceAll": "replace_all",
    },
    "Bash": {"purpose": None},
    "read_file_range": {"filepath": "path"},
    "get_file_outline": {"filepath": "path"},
    "get_project_memory": {"query": "keyword"},
    "WebSearch": {
        "searchEngine": "engine",
        "maxResults": "max_results",
    },
    "ReadSkill": {"skillName": "name"},
    # 官方子代理派发：subagent_type 丢弃前先用于判定只读（见 restore_args）
    "SubAgent": {
        "prompt": "task_description",
        "description": "task_title",
        "subagent_type": None,
    },
    "SubAgentAsync": {
        "prompt": "task_description",
        "description": "task_title",
        "subagent_type": None,
    },
    # 历史回放用的查询/取消别名
    "TaskQuery": {"taskId": "agent_id", "wait": "wait", "reportOffset": "report_offset"},
    "TaskCancel": {"taskId": "agent_id"},
    # 官方单文件精确替换 → editor_apply_diff
    "single_find_and_replace": {
        "filepath": "path",
        "old_string": "old_text",
        "new_string": "new_text",
        "replace_all": "replace_all",
    },
}


def visible_tool_name(real_name: str, ta3: bool) -> str:
    """模型实际看到的工具名（提示词引用工具名时必须用它）。非 ta3 或无映射时原样返回。"""
    if not ta3:
        return real_name
    alias = TO_TA3.get(real_name)
    if alias is not None:
        return alias
    return TO_TA3_EXTENSION.get(real_name, real_name)


def _rename(args: dict, mapping: dict[str, str | None] | None) -> dict:
    out: dict = {}
    for k, v in args.items():
        target = (mapping or {}).get(k, k)
        if target is None:
            continue
        out[target] = v
    return out


def _as_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "1", "yes", "y", "on"):
            return True
        if low in ("false", "0", "no", "n", "off", "", "none", "null"):
            return False
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return default


# ─────────────────── 入站值级还原（官方参数 → 真实工具参数） ───────────────────

# 官方 WebSearch 的 searchEngine 是智谱搜索档位；映射到本项目真实搜索源：
# 基础/高阶 → workbuddy（云端，不可用时自动回退 bing），搜狗/夸克（中文垂直源）→ bing 直连
_WEB_ENGINE_FROM_TA3 = {
    "search_std": "workbuddy",
    "search_pro": "workbuddy",
    "search_pro_sogou": "bing",
    "search_pro_quark": "bing",
}
_WEB_ENGINE_TO_TA3 = {
    "workbuddy": "search_std",
    "bing": "search_std",
    "duckduckgo": "search_std",
    "google": "search_pro",
}
# 官方 WebSearch 有、真实工具无对应能力的参数（domain/includeDomains 改写为 site: 前缀保留语义）
_WEB_UNSUPPORTED = ("timeRange", "contentSize", "searchIntent", "requestId", "userId",
                    "domain", "includeDomains")

# generate_project_memory.type → (scope, kind)。官方语义：user/feedback 存全局、project/reference 存项目
_MEMORY_TYPE_MAP = {
    "user": ("global", "fact"),
    "feedback": ("global", "convention"),
    "project": ("project", "decision"),
    "reference": ("project", "fact"),
}

# 官方 SubAgent.subagent_type 中声明为「只读」角色的取值（plugins/tools/subAgent.js:48）
_READONLY_SUBAGENT_TYPES = ("explore", "plan")


def _restore_read(args: dict) -> dict:
    # 官方 Read 无分页参数（整文件读入，上限 220KB）；本项目 fs_read 按窗口分页，
    # 默认窗口由工具决定，长文件按结果页脚给出的 offset 续读（offset/limit 原样透传）。
    # 不在这里注入 limit：注入值会随历史回放成为 schema 里不存在的参数。
    # document / docx 已在键名表里丢弃——ta3.py 只换参数不换工具名，无法改派 read_attachment。
    return args


def _restore_diff(args: dict) -> dict:
    # 官方 Diff 直接返回完整 `git diff -- .`（只含已跟踪文件）；本项目默认只给统计
    args.setdefault("stat_only", False)
    args.setdefault("tracked_only", True)
    return args


def _restore_write(args: dict) -> dict:
    mode = str(args.get("mode") or "").strip().lower()
    if mode in ("", "overwrite"):
        args.pop("mode", None)
    else:
        args["mode"] = mode
    return args


def _restore_option(opt) -> object:
    if not isinstance(opt, dict):
        return opt
    label = str(opt.get("label") or opt.get("value") or "").strip()
    desc = str(opt.get("description") or "").strip()
    if label and desc:
        return f"{label} — {desc}"
    return label or desc or opt


def _restore_ask_user(args: dict) -> dict:
    questions = args.get("questions")
    if not isinstance(questions, list):
        return args
    restored = []
    for q in questions:
        if not isinstance(q, dict):
            restored.append(q)
            continue
        item: dict = {}
        text = str(q.get("question") or "").strip()
        desc = str(q.get("description") or "").strip()
        # 本项目问卷卡只展示 question 正文：背景说明并入正文，用户才能看到
        item["question"] = f"{text}\n{desc}" if text and desc else (text or desc)
        if isinstance(q.get("options"), list):
            item["options"] = [_restore_option(o) for o in q["options"]]
        if "allowCustom" in q or "allow_custom" in q:
            item["allow_custom"] = _as_bool(q.get("allowCustom", q.get("allow_custom")), True)
        restored.append(item)
    args["questions"] = restored
    args.pop("title", None)
    return args


def _restore_web_search(args: dict) -> dict:
    engine = args.get("engine")
    if isinstance(engine, str) and engine in _WEB_ENGINE_FROM_TA3:
        args["engine"] = _WEB_ENGINE_FROM_TA3[engine]
    domain = args.get("domain")
    if not domain and isinstance(args.get("includeDomains"), list) and args["includeDomains"]:
        domain = args["includeDomains"][0]
    if isinstance(domain, str) and domain.strip() and isinstance(args.get("query"), str):
        args["query"] = f"site:{domain.strip()} {args['query']}"
    for key in _WEB_UNSUPPORTED:
        args.pop(key, None)
    return args


def _memory_item_to_entry(item: dict) -> dict | None:
    if not isinstance(item, dict):
        return None
    name = str(item.get("name") or "").strip()
    desc = str(item.get("description") or "").strip()
    content = str(item.get("content") or "").strip()
    scope, kind = _MEMORY_TYPE_MAP.get(str(item.get("type") or "").strip().lower(), ("project", "fact"))
    head = "：".join(p for p in (name, desc) if p)
    text = f"{head}\n{content}" if head and content else (head or content)
    return {"text": text, "kind": kind, "scope": scope}


def _restore_generate_memory(args: dict) -> dict:
    memories = args.get("memories")
    if isinstance(memories, dict):
        memories = [memories]
    if not isinstance(memories, list):
        # 只给 chatHistory（自动提取）：本项目没有对应能力，交给 memory_write 报缺参
        return {}
    entries = [e for e in (_memory_item_to_entry(m) for m in memories) if e]
    return {"memories": entries}


def _restore_get_memory(args: dict) -> dict:
    keyword = str(args.get("keyword") or "").strip()
    if keyword.lower() in ("all", "*"):
        # 官方 'all' = 返回全部记忆完整内容 → memory_search 的列表模式（只查记忆库），
        # 单页取工具上限，超出部分按结果末尾的 offset 翻页
        args["list_all"] = True
        args.setdefault("limit", 50)
    return args


def _restore_subagent(raw: dict, args: dict) -> dict:
    if "inherit_context" in args:
        args["inherit_context"] = _as_bool(args["inherit_context"], False)
    # 官方 Explore（只读研究）/ Plan（只读出计划）→ 本项目只读子代理；其余缺省为可写
    if str(raw.get("subagent_type") or "").strip().lower() in _READONLY_SUBAGENT_TYPES:
        args["explore"] = True
        args["mode"] = "readonly"
    return args


def restore_args(ta3_name: str, args: dict) -> dict:
    """入站：ta3 参数 → 真实参数（键名改写 + 值级还原）。"""
    raw = args if isinstance(args, dict) else {}
    out = _rename(raw, ARGS_FROM_TA3.get(ta3_name))
    if ta3_name == "Read":
        return _restore_read(out)
    if ta3_name == "Diff":
        return _restore_diff(out)
    if ta3_name == "Write":
        return _restore_write(out)
    if ta3_name == "ask_user":
        return _restore_ask_user(out)
    if ta3_name == "WebSearch":
        return _restore_web_search(out)
    if ta3_name == "generate_project_memory":
        return _restore_generate_memory(out)
    if ta3_name == "get_project_memory":
        return _restore_get_memory(out)
    if ta3_name in ("SubAgent", "SubAgentAsync"):
        return _restore_subagent(raw, out)
    return out


# ─────────────────── 出站值级换算（真实参数 → 官方参数，用于历史回放） ───────────────────

# 历史回放时允许保留的非官方键：fs_read 结果页脚明确指引「用 offset 续读」，
# 去掉它们会让同一 Read(filepath) 在历史里返回不同内容，模型无从理解分页
_HISTORY_EXTRA_KEYS: dict[str, frozenset[str]] = {
    "Read": frozenset({"offset", "limit"}),
}


def _official_props(alias: str) -> set[str] | None:
    from app.models.providers.ta3_tool_schemas import TA3_OFFICIAL_DEFINITIONS

    d = TA3_OFFICIAL_DEFINITIONS.get(alias)
    if d is None:
        return None
    return set((d.get("parameters") or {}).get("properties") or {})


def _disguise_ask_user(args: dict) -> dict:
    questions = args.get("questions")
    if not isinstance(questions, list):
        return args
    out = []
    for q in questions:
        if not isinstance(q, dict):
            out.append(q)
            continue
        item = {k: v for k, v in q.items() if k not in ("allow_custom", "options")}
        if isinstance(q.get("options"), list):
            item["options"] = [
                o if isinstance(o, dict) else {"label": str(o)} for o in q["options"]
            ]
        if "allow_custom" in q:
            item["allowCustom"] = _as_bool(q.get("allow_custom"), True)
        out.append(item)
    args["questions"] = out
    return args


def _entry_to_memory_item(entry: dict) -> dict:
    text = str(entry.get("text") or "").strip()
    scope = str(entry.get("scope") or "session").strip().lower()
    kind = str(entry.get("kind") or "fact").strip().lower()
    if scope == "global":
        mtype = "feedback" if kind in ("convention", "pitfall") else "user"
    else:
        mtype = "project"
    keywords = entry.get("keywords")
    name = ""
    if isinstance(keywords, list) and keywords:
        name = str(keywords[0])
    elif isinstance(keywords, str) and keywords.strip():
        name = keywords.replace("，", ",").split(",")[0].strip()
    return {"name": name or f"{mtype}_{kind}", "type": mtype, "content": text}


def _disguise_memory_write(args: dict) -> dict:
    memories = args.get("memories")
    if isinstance(memories, list):
        items = [_entry_to_memory_item(m) for m in memories if isinstance(m, dict)]
    else:
        items = [_entry_to_memory_item(args)]
    return {"memories": items}


def _disguise_web_search(args: dict) -> dict:
    engine = args.get("searchEngine")
    if isinstance(engine, str):
        mapped = _WEB_ENGINE_TO_TA3.get(engine)
        if mapped:
            args["searchEngine"] = mapped
        elif engine not in _WEB_ENGINE_FROM_TA3:
            args.pop("searchEngine", None)
    return args


def _disguise_bash(args: dict) -> dict:
    if not str(args.get("purpose") or "").strip():
        command = str(args.get("command") or "").strip().replace("\n", " ")
        if len(command) > 60:
            command = command[:60] + "…"
        args["purpose"] = f"运行命令：{command}" if command else "运行终端命令"
    return args


def _disguise_subagent(real: dict, args: dict) -> dict:
    mode = str(real.get("mode") or "").strip().lower()
    if mode == "readonly" or (not mode and _as_bool(real.get("explore"), False)):
        args["subagent_type"] = "Explore"
    acceptance = str(real.get("acceptance_criteria") or "").strip()
    if acceptance:
        prompt = str(args.get("prompt") or "")
        args["prompt"] = f"{prompt}\n\n验收标准：{acceptance}" if prompt else f"验收标准：{acceptance}"
    if "inherit_context" in args:
        args["inherit_context"] = _as_bool(args["inherit_context"], False)
    return args


def disguise_args(real_name: str, args: dict) -> dict:
    """出站：真实参数 → ta3 参数（键名改写 + 值级换算 + 官方参数面收敛）。

    内置通道的结果只保留官方 schema 里声明的键（例外见 `_HISTORY_EXTRA_KEYS`），
    保证历史回放的调用参数与当轮下发的 schema 一致；拓展/MCP 通道原样透传。
    """
    src = copy.deepcopy(args) if isinstance(args, dict) else {}
    alias = TO_TA3.get(real_name)
    if real_name == "memory_write":
        out = _disguise_memory_write(src)
    else:
        out = _rename(src, ARGS_TO_TA3.get(real_name))
        if real_name == "ask_user_question":
            out = _disguise_ask_user(out)
        elif real_name == "web_search":
            out = _disguise_web_search(out)
        elif real_name == "terminal_exec":
            out = _disguise_bash(out)
        elif real_name == "spawn_subagent":
            out = _disguise_subagent(src, out)
    if alias is None:
        return out
    props = _official_props(alias)
    if props is None:
        return out
    keep = props | _HISTORY_EXTRA_KEYS.get(alias, frozenset())
    return {k: v for k, v in out.items() if k in keep}


# 自检：内置映射的每个目标名都必须落在官方白名单里（漏配/多配在此暴露）
_unmapped = [k for k, v in TO_TA3.items() if v not in TA3_OFFICIAL_BUILTIN_TOOLS]
if _unmapped:  # pragma: no cover - 配置错误时尽早暴露，而不是静默下发错名字
    raise RuntimeError(
        f"TO_TA3 的目标名不在官方内置工具白名单内：{_unmapped}；"
        f"请同步 server/app/models/providers/ta3_official_tools.py"
    )
