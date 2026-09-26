"""工具名伪装映射（ta3 模式专用）。

当前项目真实执行名（snake_case）→ ta3 工具名（PascalCase / 原生命名）。
映射原则（方案 §5.4）：
- 语义完全一致 → 直接映射（参数名一致或做适配层转换）
- ta3 无对应工具 → 剔除（不发给模型），由系统提示词引导用替代工具
- 参数差异 → ARGS_* 适配表在 Ta3Provider 内做键名双向转换

参数适配说明：
- editor_apply_diff(path/old_text/new_text) → Edit(filepath/oldString/newString)
- spawn_subagent(task_title/task_description/acceptance_criteria/explore/background)
    → SubAgent(description/prompt/subagent_type)
- v36 (plan-321-1600 M3): 接入 ta3 侧异步子代理三件套——
    SubAgentAsync → spawn_subagent(background=True)（后台派发，完成时自动推送）
    TaskQuery     → collect_results(taskId→agent_id)
    TaskCancel    → cancel_subagent(taskId→agent_id)
  并**取消**此前 SubAgent 的 explore 强制同步：改按 subagent_type 决定
  （Explore → 只读同步探索；其余/缺省 → 后台异步），使 ta3 模型真正能用上并行子代理。
- plan-73-322: SubAgentAsync 入站补透传只读语义（subagent_type=Explore → explore=True
  + background=True，即「只读 + 异步」）；配套在 disguise_tools 侧把 SubAgentAsync 真正
  下发给模型——此前一对一出站映射使它从未暴露，「只读调研不阻塞」在模型侧无法表达。
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
    "spawn_subagent": "SubAgent",      # 参数适配（explore/background 按 subagent_type 还原）
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
    # 会话目标管理工具
    "goal_complete": "CompleteGoal",       # summary ✓
    # 通用提问工具（需求澄清）——四种模式均可用；参数 questions 键名一致，无需 ARGS 适配
    "ask_user_question": "AskUser",        # questions/allow_custom ✓
    # plan-238-1191: 多文件批量编辑（当前项目核心写工具，此前缺映射被伪装层剔除——
    # ta3 会话里模型既拿不到该工具、历史调用还被降级成"结果已略"提示，导致批量
    # 编辑工作流中断）。伪装名对齐自造 PascalCase 先例（ReadAttachment 等）；
    # edits[{path,old_text,new_text}] 键名与真实工具一致，无需 ARGS 适配。
    "multi_file_edit": "MultiFileEdit",    # edits ✓
    # plan-248-1258 M3.3: 符号索引检索/文件骨架（此前缺映射被伪装层剔除——系统提示词
    # 仍在引导模型"优先用 symbol_search/outline"（context_manager._symbol_index_hint），
    # 但 ta3 会话里模型根本拿不到这两个工具，引导落空且索引能力完全闲置）。
    # outline → 参考项目原生 get_file_outline（语义一致：文件结构骨架+行号区间），
    # 仅键名适配 path→filepath；symbol_search 参考项目无对应工具，伪装名对齐自造
    # PascalCase 先例（ReadAttachment/BashStatus 等），query/kind/file_glob/limit
    # 键名与真实工具一致，无需 ARGS 适配。
    "symbol_search": "SymbolSearch",       # query/kind/file_glob/limit ✓
    "outline": "get_file_outline",         # path→filepath（见 ARGS_* 表）
    # ── v43: 全量补齐（此前缺映射的工具被伪装层整体剔除）──
    # 背景：disguise_tools 对无映射工具直接丢弃——这些工具在 ta3 会话里模型既看不到、
    # 也调不动，历史调用还会被降级成"当前模型下不可用"文本。现按真实注册表补全。
    "web_fetch": "WebFetch",                 # url ✓
    "ci_run": "CiRun",                       # check ✓
    "git": "Git",                            # command/cwd/message/files/... ✓
    "codebase_search": "CodebaseSearch",     # query/top_k/file_glob ✓
    "compaction_index": "CompactionIndex",   # 无参 ✓
    "compaction_view": "CompactionView",     # index/compaction_id/keyword/offset/limit/full ✓
    "skill_view": "SkillView",               # name ✓
    "memory_write": "MemoryWrite",           # text/kind/scope ✓
    "browser_navigate": "BrowserNavigate",       # url/wait_until ✓
    "browser_screenshot": "BrowserScreenshot",   # url/full_page ✓
    "browser_click": "BrowserClick",             # selector/x/y ✓
    "browser_type": "BrowserType",               # selector/text/press_enter/clear_before ✓
    "browser_snapshot": "BrowserSnapshot",       # max_depth ✓
    "browser_evaluate": "BrowserEvaluate",       # script ✓
    # v43: 子代理管理工具——v36 只补了入站三条（SubAgentAsync/TaskQuery/TaskCancel），
    # 出站 TO_TA3 缺失 → ta3 会话里 collect_results 等历史调用被降级、模型侧拿不到
    # 查询/取消/检视/指令能力（只能单向派发）。现补齐，键名适配见 ARGS_TO_TA3。
    "collect_results": "TaskQuery",          # agent_id→taskId
    "cancel_subagent": "TaskCancel",         # agent_id→taskId
    "subagent_inspect": "SubAgentInspect",   # agent_id/section ✓
    "send_to_subagent": "SendToSubagent",    # agent_id/message ✓
    # v43: 子代理侧上报工具（只在子代理线程暴露）——子代理同样可能运行在 ta3 模型上，
    # 缺映射会让子代理失去向主代理上报/求助的通道。
    "report_to_leader": "ReportToLeader",    # message/kind/wait/timeout_s ✓
}

# 伪装名 → 真实执行名（反查）
FROM_TA3: dict[str, str] = {v: k for k, v in TO_TA3.items()}
# v36 (plan-321-1600 M3): ta3 异步子代理工具 → 本项目工具（多对一，TO_TA3 反查得不到）
FROM_TA3.update({
    "SubAgentAsync": "spawn_subagent",
    "TaskQuery": "collect_results",
    "TaskCancel": "cancel_subagent",
})

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
        "explore": None,               # 出站丢弃：ta3 侧由 subagent_type 表达
        "background": None,            # v36: 同上（ta3 侧由 SubAgentAsync 表达）
    },
    # plan-153-705: 后台进程工具键名适配（offset 键名一致免映射）
    "terminal_bg_status": {"shell_id": "shellId"},
    "terminal_bg_kill": {"shell_id": "shellId"},
    # plan-248-1258 M3.3: 文件骨架键名适配（参考项目原生用 filepath）
    "outline": {"path": "filepath"},
    # v43: 子代理管理工具出站键名适配（ta3 侧查询/取消按 taskId 定位）
    # wait 无对应键（ta3 TaskQuery 为即时查询语义），出站丢弃该键
    "collect_results": {"agent_id": "taskId", "wait": None},
    "cancel_subagent": {"agent_id": "taskId"},
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
        "subagent_type": None,         # v36: 丢弃前先用于判定 explore/background（见 restore_args）
    },
    # v36 (plan-321-1600 M3): ta3 异步子代理三件套
    "SubAgentAsync": {
        "prompt": "task_description",
        "description": "task_title",
        "subagent_type": None,
    },
    "TaskQuery": {"taskId": "agent_id"},
    "TaskCancel": {"taskId": "agent_id"},
    # plan-153-705: 后台进程工具入站适配（BashStatus/BashKill）
    "BashStatus": {"shellId": "shell_id"},
    "BashKill": {"shellId": "shell_id"},
    # plan-248-1258 M3.3: 文件骨架入站适配（filepath → path）
    "get_file_outline": {"filepath": "path"},
}

# v36 (plan-321-1600 M3): 原 SPAWN_FORCED_ARGS（强制 explore=True）已删除——
# 它让 ta3 侧调用永远同步阻塞，模型无法并行派发子代理。现按 subagent_type 判定。


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
    if ta3_name == "SubAgentAsync":
        # ta3 异步派发 → 本项目后台子代理（立即返回，完成时自动推送）
        out["background"] = True
        # plan-73-322: 补透传只读语义——subagent_type=Explore 表示只读调研，须同时置 explore，
        # 否则会被派发成可写子代理（全工具），与「Explore=只读」约定不符。
        if str(args.get("subagent_type") or "").strip().lower() == "explore":
            out["explore"] = True
    elif ta3_name == "SubAgent":
        # v36: 按 ta3 给出的 subagent_type 决定同步/异步——
        # Explore → 只读同步探索（主代理直接拿结论）；其余/缺省 → 后台异步并行。
        _st = str(args.get("subagent_type") or "").strip().lower()
        if _st == "explore":
            out["explore"] = True
        else:
            out["background"] = True
    return out
