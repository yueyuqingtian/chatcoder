"""工具名伪装映射（ta3 模式专用）。

当前项目真实执行名（snake_case）→ ta3 工具名（PascalCase / 原生命名）。
映射原则（方案 §5.4）：
- 语义完全一致 → 直接映射（参数名一致或做适配层转换）
- ta3 无对应工具 → 剔除（不发给模型），由系统提示词引导用替代工具
- 参数差异 → ARGS_* 适配表在 Ta3Provider 内做键名双向转换

参数适配说明：
- editor_apply_diff(path/old_text/new_text) → Edit(filepath/oldString/newString)
- spawn_subagent(task_title/task_description/acceptance_criteria/explore)
    → SubAgent(description/prompt/subagent_type)，explore 强制 True（同步拿结果，
    因为当前项目后台子代理需要 collect_results 轮询，而 ta3 侧无对应工具）
"""
from __future__ import annotations

# 真实执行名 → ta3 伪装名
TO_TA3: dict[str, str] = {
    "fs_read": "Read",                 # filepath ✓
    "fs_list": "List",                 # dirPath/recursive ✓
    "fs_grep": "Search",               # query/path ✓
    "git_diff": "Diff",                # 无参数 ✓
    "fs_write": "Write",               # filepath/content ✓
    "editor_apply_diff": "Edit",       # 参数适配 path→filepath / old_text→oldString / new_text→newString
    "terminal_exec": "Bash",           # command/waitForCompletion ✓
    "web_search": "WebSearch",         # query ✓
    "todo_write": "TodoWrite",         # todos ✓
    "spawn_subagent": "SubAgent",      # 参数适配 + 强制同步探索
    "memory_search": "get_project_memory",  # query ✓
    # plan-147-674: 附件/图片读取工具——ta3 参考项目无对应工具，但缺失会导致
    # 多模态图片只能用 Read 读二进制、docx/pdf 附件完全无法解析，故补充伪装名
    # （PascalCase 对齐 ta3 原生命名风格）；两工具参数均为 path，与真实工具一致
    "read_attachment": "ReadAttachment",   # path ✓（参数名一致，无需适配）
    "view_image": "ViewImage",             # path ✓（参数名一致，无需适配）
    # plan-153-705: 后台进程管理（配合 Bash waitForCompletion=false）——
    # 参考项目无对应工具，伪装名对齐 ta3 PascalCase 风格（ReadAttachment 先例）；
    # shell_id → shellId 键名适配见 ARGS_* 表
    "terminal_bg_status": "BashStatus",    # shell_id→shellId / offset ✓
    "terminal_bg_kill": "BashKill",        # shell_id→shellId
}

# 伪装名 → 真实执行名（反查）
FROM_TA3: dict[str, str] = {v: k for k, v in TO_TA3.items()}

# 出站参数键名适配：真实键 → ta3 键（None = 丢弃该键）
ARGS_TO_TA3: dict[str, dict[str, str | None]] = {
    # 键名不一致的工具做出站转换；其余参数名一致的键保留原名
    "fs_read": {"path": "filepath"},
    "fs_list": {"path": "dirPath"},
    "fs_grep": {"pattern": "query"},
    "fs_write": {"path": "filepath"},
    "memory_search": {"keyword": "query"},
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
    "spawn_subagent": {
        "task_title": "description",
        "task_description": "prompt",
        "acceptance_criteria": None,   # ta3 无对应
        "explore": None,               # 强制 True（见模块 docstring）
    },
    # plan-153-705: 后台进程工具键名适配（offset 键名一致免映射）
    "terminal_bg_status": {"shell_id": "shellId"},
    "terminal_bg_kill": {"shell_id": "shellId"},
}

# 入站参数键名适配：ta3 键 → 真实键（None = 丢弃该键）
ARGS_FROM_TA3: dict[str, dict[str, str | None]] = {
    "Read": {"filepath": "path"},
    "List": {"dirPath": "path"},
    "Search": {"query": "pattern"},
    "Write": {"filepath": "path"},
    "get_project_memory": {"query": "keyword"},
    "Edit": {
        "filepath": "path",
        "oldString": "old_text",
        "newString": "new_text",
        "replaceAll": "replace_all",
    },
    "WebSearch": {
        "searchEngine": "engine",
        "maxResults": "max_results",
    },
    "SubAgent": {
        "prompt": "task_description",
        "description": "task_title",
        "subagent_type": None,         # 强制同步探索（explore=True）
    },
    # plan-153-705: 后台进程工具入站适配（BashStatus/BashKill）
    "BashStatus": {"shellId": "shell_id"},
    "BashKill": {"shellId": "shell_id"},
}

# SubAgent 伪装后的固定附加参数（模型调用 SubAgent 时强制同步只读探索）
SPAWN_FORCED_ARGS: dict[str, object] = {"explore": True}


def disguise_args(real_name: str, args: dict) -> dict:
    """出站：真实参数 → ta3 参数。"""
    mapping = ARGS_TO_TA3.get(real_name)
    if not mapping:
        return dict(args)
    out: dict = {}
    for k, v in args.items():
        target = mapping.get(k, k)
        if target is None:
            continue
        out[target] = v
    return out


def restore_args(ta3_name: str, args: dict) -> dict:
    """入站：ta3 参数 → 真实参数。"""
    mapping = ARGS_FROM_TA3.get(ta3_name)
    out: dict = {}
    for k, v in args.items():
        target = (mapping or {}).get(k, k)
        if target is None:
            continue
        out[target] = v
    if ta3_name == "SubAgent":
        out.update(SPAWN_FORCED_ARGS)
    return out
