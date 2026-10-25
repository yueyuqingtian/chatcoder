"""统一权限裁决内核（plan-75-332）。

改造前的裁决散落在多处、彼此优先级硬编码在 `executor._precheck_approval` 一个函数里：
沙箱 read-only 硬边界 → 工具自身钩子 `approval_precheck` → 权限模式 plan/readonly →
模式白名单 → exec_policy 规则 → danger-full-access 免审；而 `shell_policy` 还会
**直接拒绝**危险命令（连审批都不给），`outside_access` 又会为越界读取单独弹一张卡。
同一件事在多个位置各判一次，改一处就得记得另外几处。

本模块把裁决收敛为唯一入口：

    classify()  → 动作类别（含工作区内/外判定）
    decide()    → 三档权限模式下的裁决（allow / ask / deny）

两条**正交**控制轴（plan-75-332 §3.1）：

- 执行模式（能做什么，`readonly` / `plan` / `agent`）：越界即 `deny`，属能力边界，
  不进入审批；这是**唯一**保留的硬拒绝来源。
- 权限模式（要不要问，`ask` / `auto` / `full`）：决定边界内的审批策略。

工具自身不再做任何拦截（本方案的核心诉求）：原命令黑名单降级为「风险标注」
（`exec_risky` 的分类依据），原只读白名单升级为「只读判定」（`exec_readonly` 依据）。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════
# 动作类别
# ══════════════════════════════════════════════════════════
ACTION_READ = "read"
ACTION_READ_OUTSIDE = "read_outside"
ACTION_WRITE = "write"
ACTION_WRITE_OUTSIDE = "write_outside"
ACTION_DELETE = "delete"
ACTION_EXEC_READONLY = "exec_readonly"
ACTION_EXEC_NORMAL = "exec_normal"
ACTION_EXEC_RISKY = "exec_risky"
ACTION_NETWORK = "network"
ACTION_MANAGE = "manage"
# plan-334-1661：电脑操控写操作（点击/输入/按键）——直接作用于用户的真实桌面，
# 语义上既不是文件写也不是命令执行，单独成类才能在审批卡上给出准确措辞。
ACTION_DESKTOP = "desktop"
# plan-20261015-browser-agent：内置浏览器里的写操作（点击/输入/执行脚本）。
# 作用范围是右侧面板里的网页，不碰用户桌面，所以与电脑操控分开；本地页面
# （localhost / 127.0.0.1 / file）单列一类免确认——对齐 Claude Code「本地开发服务器免审批」。
ACTION_BROWSER = "browser"
ACTION_BROWSER_LOCAL = "browser_local"

# 审批卡标题用的动作短语（前端可直接展示，不必再拼工具名）
ACTION_LABELS: dict[str, str] = {
    ACTION_READ: "读取文件",
    ACTION_READ_OUTSIDE: "读取工作区外的文件",
    ACTION_WRITE: "修改文件",
    ACTION_WRITE_OUTSIDE: "修改工作区外的文件",
    ACTION_DELETE: "删除文件",
    ACTION_EXEC_READONLY: "执行只读命令",
    ACTION_EXEC_NORMAL: "执行一个操作",
    ACTION_EXEC_RISKY: "执行风险命令",
    ACTION_NETWORK: "访问网络",
    ACTION_MANAGE: "管理系统进程",
    ACTION_DESKTOP: "操作电脑（鼠标/键盘）",
    ACTION_BROWSER: "操作网页（点击/输入）",
    ACTION_BROWSER_LOCAL: "操作本地网页",
}

# ══════════════════════════════════════════════════════════
# 执行模式（能做什么）
# ══════════════════════════════════════════════════════════
MODE_READONLY = "readonly"
MODE_PLAN = "plan"
MODE_AGENT = "agent"
# plan-workflow-collab：工作流模式——能力边界同智能体，区别只在模式提示词要求以工作流完成任务
MODE_WORKFLOW = "workflow"

EXECUTION_MODES = (MODE_READONLY, MODE_PLAN, MODE_AGENT, MODE_WORKFLOW)

# 旧值归一化：default（原「完全访问」）与 accept_edits（原「计划执行」）都并入 agent。
# plan-75-332：accept_edits 取消——它与 default 的实际能力完全相同（全量工具 + 免审批），
# 单独存在只会让用户在两个等价选项间困惑。
_MODE_ALIASES = {
    "default": MODE_AGENT,
    "accept_edits": MODE_AGENT,
    "full": MODE_AGENT,
    "full_access": MODE_AGENT,
    "full-access": MODE_AGENT,
}


def normalize_execution_mode(mode: str | None) -> str:
    """执行模式归一化。

    plan-75-332 R7 修复：未识别值**不能**一律回落 agent——模式名取值域是
    「内置 3 档 ∪ 用户自定义」（plan-230-1144 M2：自定义模式名是任意字符串）。
    一律回落会让自定义模式在保存（会话 PATCH）与回显时被改写成「智能体模式」，
    用户配置的自定义模式实际不可用（表现为：选了自定义模式、重进会话变回智能体）。

    口径：空值回落 agent（内置默认档）；旧值别名映射到新值；其余原样保留。
    自定义模式的边界约束由工具白名单（permission_profile_service.resolve_tools）承担，
    本条不改变该机制。
    """
    m = (mode or "").strip()
    if not m:
        return MODE_AGENT
    if m in EXECUTION_MODES:
        return m
    return _MODE_ALIASES.get(m, m)


# 模式归一入口的进程内缓存（plan-560-2311 C1）：首次裁决时从服务层取一次函数对象，
# 之后直接调用，避免每次工具裁决都走一次 import 机制。None = 尚未解析。
_RESOLVE_MODE_KIND = None


# ══════════════════════════════════════════════════════════
# 权限模式（要不要问）
# ══════════════════════════════════════════════════════════
APPROVAL_ASK = "ask"
APPROVAL_AUTO = "auto"
APPROVAL_FULL = "full"

APPROVAL_MODES = (APPROVAL_ASK, APPROVAL_AUTO, APPROVAL_FULL)

_APPROVAL_ALIASES = {
    "always": APPROVAL_ASK,
    "never": APPROVAL_FULL,
    "danger-full-access": APPROVAL_FULL,
    "full-access": APPROVAL_FULL,
    "workspace-write": APPROVAL_AUTO,  # 旧沙箱模式的语义近似值
    "read-only": APPROVAL_ASK,
}


def normalize_approval_mode(mode: str | None) -> str:
    """权限模式归一化。缺省为 ask（最保守：每次都要问）。"""
    m = (mode or "").strip()
    if m in APPROVAL_MODES:
        return m
    return _APPROVAL_ALIASES.get(m, APPROVAL_ASK)


# ══════════════════════════════════════════════════════════
# 裁决结果
# ══════════════════════════════════════════════════════════
VERDICT_ALLOW = "allow"
VERDICT_ASK = "ask"
VERDICT_DENY = "deny"

# ══════════════════════════════════════════════════════════
# 策略矩阵（plan-75-332 §3.2）
# 行 = 动作类别，列 = 权限模式 → 裁决
# ══════════════════════════════════════════════════════════
_MATRIX: dict[str, dict[str, str]] = {
    # 纯读取：三档都放行（读取本身不构成风险，用户口径里也只要求"执行命令/编辑/新增/删除"需审批）
    ACTION_READ: {APPROVAL_ASK: VERDICT_ALLOW, APPROVAL_AUTO: VERDICT_ALLOW, APPROVAL_FULL: VERDICT_ALLOW},
    # 读工作区外：询问审批下要问（信息边界），自动审批放行
    ACTION_READ_OUTSIDE: {APPROVAL_ASK: VERDICT_ASK, APPROVAL_AUTO: VERDICT_ALLOW, APPROVAL_FULL: VERDICT_ALLOW},
    # 工作区内写：自动审批放开（新增/编辑文件属常规操作）
    ACTION_WRITE: {APPROVAL_ASK: VERDICT_ASK, APPROVAL_AUTO: VERDICT_ALLOW, APPROVAL_FULL: VERDICT_ALLOW},
    # 写工作区外：自动审批仍要问（越出项目范围）
    ACTION_WRITE_OUTSIDE: {APPROVAL_ASK: VERDICT_ASK, APPROVAL_AUTO: VERDICT_ASK, APPROVAL_FULL: VERDICT_ALLOW},
    # 删除：自动审批仍要问
    ACTION_DELETE: {APPROVAL_ASK: VERDICT_ASK, APPROVAL_AUTO: VERDICT_ASK, APPROVAL_FULL: VERDICT_ALLOW},
    # 只读命令：询问审批下仍逐条问（用户明确"询问审批每次执行命令都需要审批"）
    ACTION_EXEC_READONLY: {APPROVAL_ASK: VERDICT_ASK, APPROVAL_AUTO: VERDICT_ALLOW, APPROVAL_FULL: VERDICT_ALLOW},
    # 常规命令（构建/测试/装包/跑脚本）
    ACTION_EXEC_NORMAL: {APPROVAL_ASK: VERDICT_ASK, APPROVAL_AUTO: VERDICT_ALLOW, APPROVAL_FULL: VERDICT_ALLOW},
    # 风险命令（原黑名单命中项）
    ACTION_EXEC_RISKY: {APPROVAL_ASK: VERDICT_ASK, APPROVAL_AUTO: VERDICT_ASK, APPROVAL_FULL: VERDICT_ALLOW},
    ACTION_NETWORK: {APPROVAL_ASK: VERDICT_ASK, APPROVAL_AUTO: VERDICT_ALLOW, APPROVAL_FULL: VERDICT_ALLOW},
    # 进程/服务管理
    ACTION_MANAGE: {APPROVAL_ASK: VERDICT_ASK, APPROVAL_AUTO: VERDICT_ASK, APPROVAL_FULL: VERDICT_ALLOW},
    # 电脑操控写操作（plan-334-1661）：直接操作真实鼠标键盘，
    # 与「风险命令」同档——自动审批下仍要问（点错窗口成本很高），完全访问才自动放行。
    ACTION_DESKTOP: {APPROVAL_ASK: VERDICT_ASK, APPROVAL_AUTO: VERDICT_ASK, APPROVAL_FULL: VERDICT_ALLOW},
    # 网页写操作：只作用于面板里的网页，自动审批即放行；询问审批下外部网站要确认
    ACTION_BROWSER: {APPROVAL_ASK: VERDICT_ASK, APPROVAL_AUTO: VERDICT_ALLOW, APPROVAL_FULL: VERDICT_ALLOW},
    ACTION_BROWSER_LOCAL: {APPROVAL_ASK: VERDICT_ALLOW, APPROVAL_AUTO: VERDICT_ALLOW, APPROVAL_FULL: VERDICT_ALLOW},
}

# ══════════════════════════════════════════════════════════
# 工具分类表
# ══════════════════════════════════════════════════════════

# 纯读工具（对文件系统 / 索引 / 记忆只读，无外部副作用）
_READ_TOOLS = frozenset({
    "fs_read", "fs_list", "fs_grep", "git_diff", "codebase_search",
    "symbol_search", "outline", "view_image", "read_attachment",
    "memory_search", "compaction_index", "compaction_view",
    "skill_view", "todo_write", "ask_user_question", "goal_complete",
    "terminal_bg_status",
    # plan-549-2281：功能地图查询只读 ai/ 计划文档、项目记忆与符号索引，
    # 只往应用自身的 .chatcoder 缓存落文件（与 codebase_search 的索引缓存同口径），
    # 不触碰用户文件；符号索引的开启同样只是建应用侧索引。
    "feature_lookup", "symbol_index_enable",
})

# 记忆写工具（plan-537-2256）：memory_write 会往记忆库落新行，属改变状态，
# 不能留在只读集合——否则只读/计划模式下的调用会被当成纯读直接放行。
# 复用 ACTION_WRITE：它是本项目「改变应用数据」的既有最小写档位（与 todo_write
# 那类轻量状态工具同口径：询问审批下问、自动审批放行、完全访问放行），
# 不需要再引入新的动作类别去扩散策略矩阵。
_MEMORY_WRITE_TOOLS = frozenset({"memory_write"})

# 带路径参数、需要判定工作区内外的读工具
# 注：read_attachment 不在其中——附件上传目录是用户主动提供内容的白名单根，
# 不属于"工作区外越界"，纳入只会让每次读附件都弹一次审批。
_PATH_READ_TOOLS = frozenset({"fs_read", "fs_list", "fs_grep", "view_image"})

# 写盘工具（路径参数名见下）
_WRITE_TOOLS = frozenset({"fs_write", "editor_apply_diff", "multi_file_edit", "apply_patch"})

# 编辑类写工具（原地改写已有文件，含历史计划文档）：计划模式下没有任何例外。
# 单独成表用于在拒绝文案中给出「改用 fs_write 重写新文档」的明确指引——用户反馈：
# 调整计划时模型反复尝试编辑历史方案文档，原文案（"只能写入计划文档"）会被误读为
# "目标在 ai/ 下即应放行"，模型于是换工具反复重试。
_EDIT_TOOLS = frozenset({"editor_apply_diff", "multi_file_edit", "apply_patch"})

# 读磁盘时可能带上"列目录/读文件"语义的路径参数名
_PATH_KEYS = ("path", "file_path", "filepath", "file")

# 网络：外发或外部读取
_NETWORK_TOOLS = frozenset({"web_fetch", "web_search"})

# 内置浏览器（plan-20261015-browser-agent）：读页面 / 改页面两类；导航按目标地址判定
_BROWSER_READ_TOOLS = frozenset({
    "browser_tabs", "browser_snapshot", "browser_get_text", "browser_screenshot",
    "browser_scroll", "browser_hover", "browser_wait", "browser_logs",
})
_BROWSER_WRITE_TOOLS = frozenset({
    "browser_click", "browser_type", "browser_press", "browser_drag", "browser_fill", "browser_evaluate",
})
# 批量步骤名 → 真实工具名（与 browser driver 的批量动作表一致）
_BROWSER_BATCH_ALIASES = {
    "navigate": "browser_navigate", "open": "browser_navigate", "click": "browser_click", "type": "browser_type",
    "fill": "browser_fill", "press": "browser_press", "key": "browser_press", "scroll": "browser_scroll",
    "hover": "browser_hover", "drag": "browser_drag", "wait": "browser_wait", "clipboard": "browser_clipboard",
    "screenshot": "browser_screenshot", "snapshot": "browser_snapshot", "get_text": "browser_get_text",
    "evaluate": "browser_evaluate", "tabs": "browser_tabs",
}
_BROWSER_SEVERITY = {ACTION_READ: 0, ACTION_BROWSER_LOCAL: 1, ACTION_NETWORK: 2, ACTION_BROWSER: 3}

# 进程 / 服务管理
_MANAGE_TOOLS = frozenset({"terminal_bg_kill"})

# 电脑操控：写操作（真的动鼠标键盘）
# plan-537-2251 L5：desktop_focus 会 SetForegroundWindow 并预热目标窗口的无障碍树，
# 属于改变系统状态（会把用户正在用的窗口压到后面），不是「读屏幕内容」。此前它被归到只读集合，
# 因此 desktop.py 里把它标成 high 风险也不会生效（classify 里集合判断优先于 risk_level）。
_DESKTOP_WRITE_TOOLS = frozenset({
    "desktop_click", "desktop_type", "desktop_keys", "desktop_scroll", "desktop_batch",
    "desktop_focus",
})
# 电脑操控：只读感知（读屏幕/无障碍树），与文件读取同为纯读
_DESKTOP_READ_TOOLS = frozenset({
    "desktop_windows", "desktop_snapshot", "desktop_screenshot",
})

# 执行任意命令
_COMMAND_TOOLS = frozenset({"terminal_exec"})

# 预设检查项（lint/test/build，命令由服务端构造，非任意命令）
_PRESET_EXEC_TOOLS = frozenset({"ci_run"})

# git 工具只读子命令（其余子命令会改写仓库状态 → 视为写操作）
_GIT_READ_SUBCOMMANDS = frozenset({
    "diff", "log", "blame", "status", "show", "shortlog", "rev-parse", "ls-files",
})


@dataclass
class ActionInfo:
    """动作分类结果。"""

    action: str
    label: str = ""
    risk_note: str = ""     # 风险说明（命中风险特征时非空，进审批卡"风险"区）
    paths: tuple[str, ...] = ()   # 涉及的关键路径（审批卡展示用）

    def __post_init__(self) -> None:
        if not self.label:
            self.label = ACTION_LABELS.get(self.action, self.action)


@dataclass
class Decision:
    """裁决结果。"""

    verdict: str            # allow / ask / deny
    action: str
    label: str = ""
    reason: str = ""        # deny 原因 / 规则命中说明
    risk_note: str = ""

    @property
    def skipped(self) -> bool:
        return self.verdict == VERDICT_ALLOW


# ══════════════════════════════════════════════════════════
# 路径工具
# ══════════════════════════════════════════════════════════
def is_within_workspace(workspace_root: str | None, raw_path: str) -> bool:
    """判断目标路径是否落在工作区内（不要求路径已存在）。

    用 `resolve_loose`（纯解析、不设边界）而不是 `safe_resolve`：后者越界时返回
    None，无法区分"越界"与"路径非法"，而本体系需要把越界识别出来交给审批策略。
    """
    from app.orchestration.tools.safe_path import resolve_loose

    if not raw_path:
        return True  # 无路径信息时不按越界处理，避免误判成越界而多弹一次审批
    resolved = resolve_loose(raw_path, workspace_root)
    if not resolved:
        return False
    root = workspace_root or os.getcwd()
    try:
        root_real = os.path.realpath(root)
        target_real = os.path.realpath(resolved)
    except (OSError, ValueError):
        return False
    try:
        return Path(target_real).is_relative_to(Path(root_real))
    except (ValueError, OSError):
        return False


def target_paths(tool_name: str, args: dict) -> tuple[str, ...]:
    """提取工具涉及的文件路径（multi_file_edit 从 edits[].path 取）。"""
    if tool_name == "multi_file_edit":
        edits = args.get("edits")
        if isinstance(edits, list):
            return tuple(
                str(e.get("path")) for e in edits
                if isinstance(e, dict) and e.get("path")
            )
        return ()
    for key in _PATH_KEYS:
        v = args.get(key)
        if isinstance(v, str) and v.strip():
            return (v.strip(),)
    return ()


def is_plan_doc_path(workspace_root: str | None, raw_path: str) -> bool:
    """计划模式唯一放行的写盘目标：工作区 `ai/` 下的 .md 计划文档。

    （自 executor._is_plan_doc_path 迁移：判定归内核，executor 不再自带一套。）
    """
    if not raw_path:
        return False
    try:
        from app.orchestration.tools.safe_path import resolve_loose

        resolved = resolve_loose(raw_path, workspace_root)
        if not resolved:
            return False
        root = Path(workspace_root or os.getcwd()).resolve()
        target = Path(resolved)
        if not target.is_relative_to(root):
            return False
        parts = target.relative_to(root).parts
        return len(parts) == 2 and parts[0].lower() == "ai" and parts[1].lower().endswith(".md")
    except Exception:  # noqa: BLE001
        return False


# ══════════════════════════════════════════════════════════
# 分类
# ══════════════════════════════════════════════════════════
def _classify_shell(command: str) -> ActionInfo:
    """终端命令分类：风险命令 > 删除语义 > 只读命令 > 常规命令。"""
    from app.orchestration.tools import shell_policy

    note = shell_policy.risk_note(command)
    if note:
        return ActionInfo(ACTION_EXEC_RISKY, risk_note=note)
    del_note = shell_policy.delete_note(command)
    if del_note:
        return ActionInfo(ACTION_DELETE, label="执行删除命令", risk_note=del_note)
    if shell_policy.is_readonly_command(command):
        return ActionInfo(ACTION_EXEC_READONLY)
    return ActionInfo(ACTION_EXEC_NORMAL)


def _classify_mcp(risk_level: str) -> ActionInfo:
    """MCP 工具按风险推断分流（推断逻辑仍在 mcp_wrapper，此处只消费结果）。

    low    → read（自动放行）
    medium → network（外部数据源访问；自动审批放行）
    high   → manage（风险操作；自动审批下仍需询问）
    """
    if risk_level == "low":
        return ActionInfo(ACTION_READ, label="调用外部工具（只读）")
    if risk_level == "high":
        return ActionInfo(ACTION_MANAGE, label="调用外部工具（高风险）")
    return ActionInfo(ACTION_NETWORK, label="调用外部工具")


def _browser_page_is_local(ctx) -> bool:
    """AI 当前操作的网页是否为本地页面（读 driver 缓存的最新地址；未知按外部处理）。"""
    try:
        from app.orchestration.browser.driver import current_url, is_local_url

        return is_local_url(current_url(int(getattr(ctx, "session_id", 0) or 0)))
    except Exception:  # noqa: BLE001
        return False


def _browser_step_action(tool_name: str, args: dict, page_local: bool) -> str:
    from app.orchestration.browser.driver import is_local_url

    if tool_name == "browser_navigate":
        url = str(args.get("url") or "")
        if not url:  # back / forward / reload：留在当前站点范围内
            return ACTION_READ if page_local else ACTION_NETWORK
        return ACTION_READ if is_local_url(url) else ACTION_NETWORK
    if tool_name in ("browser_tabs", "browser_screenshot") and args.get("url"):
        return ACTION_READ if is_local_url(str(args.get("url"))) else ACTION_NETWORK
    if tool_name == "browser_clipboard":
        if str(args.get("action") or "copy").lower() in ("copy", "select_all"):
            return ACTION_READ
        return ACTION_BROWSER_LOCAL if page_local else ACTION_BROWSER
    if tool_name in _BROWSER_WRITE_TOOLS:
        return ACTION_BROWSER_LOCAL if page_local else ACTION_BROWSER
    return ACTION_READ


def _classify_browser(tool_name: str, args: dict, ctx) -> ActionInfo:
    """内置浏览器工具分类：读页面放行；改页面按当前页面是否本地区分；批量取最严一步。"""
    page_local = _browser_page_is_local(ctx)
    if tool_name == "browser_batch":
        steps = args.get("actions") or args.get("steps") or []
        worst = ACTION_READ
        local = page_local
        for step in steps if isinstance(steps, list) else []:
            if not isinstance(step, dict):
                continue
            name = _BROWSER_BATCH_ALIASES.get(str(step.get("action") or "").replace("browser_", "").lower())
            if not name:
                continue
            params = step.get("args") if isinstance(step.get("args"), dict) else step
            act = _browser_step_action(name, params, local)
            if name == "browser_navigate" and params.get("url"):
                # 批量里先导航再操作：后续步骤按新页面判定是否本地
                from app.orchestration.browser.driver import is_local_url

                local = is_local_url(str(params.get("url")))
            if _BROWSER_SEVERITY.get(act, 0) > _BROWSER_SEVERITY.get(worst, 0):
                worst = act
        action = worst
    else:
        action = _browser_step_action(tool_name, args, page_local)
    if action == ACTION_READ:
        return ActionInfo(ACTION_READ, label="读取网页" if tool_name != "browser_navigate" else "打开本地网页")
    if action == ACTION_NETWORK:
        return ActionInfo(ACTION_NETWORK, label="打开外部网页" if tool_name != "browser_batch" else "访问外部网页")
    if action == ACTION_BROWSER:
        return ActionInfo(ACTION_BROWSER, risk_note="将在外部网页上点击或输入（可能提交表单）")
    return ActionInfo(ACTION_BROWSER_LOCAL)


def classify(tool_name: str, args: dict, ctx, *, risk_level: str = "low") -> ActionInfo:
    """把一个工具调用归类为动作类别。"""
    args = args or {}

    if tool_name.startswith("mcp_"):
        return _classify_mcp(risk_level)

    if tool_name.startswith("browser_"):
        return _classify_browser(tool_name, args, ctx)

    if tool_name in _COMMAND_TOOLS:
        return _classify_shell(str(args.get("command") or ""))

    if tool_name == "git":
        sub = str(args.get("command") or "").strip().lower()
        # branch / stash 无参时为列举（只读），带参时为写操作
        if sub in ("branch", "stash"):
            write_hint = bool(args.get("name")) or str(args.get("action") or "") in ("push", "pop")
            if write_hint:
                return ActionInfo(ACTION_WRITE, label="执行 Git 写操作")
            return ActionInfo(ACTION_READ, label="执行 Git 只读命令")
        if sub in _GIT_READ_SUBCOMMANDS:
            return ActionInfo(ACTION_READ, label="执行 Git 只读命令")
        return ActionInfo(ACTION_WRITE, label="执行 Git 写操作")

    if tool_name in _PRESET_EXEC_TOOLS:
        return ActionInfo(ACTION_EXEC_NORMAL, label="运行预设检查")

    if tool_name in _WRITE_TOOLS:
        paths = target_paths(tool_name, args)
        outside = next(
            (p for p in paths if not is_within_workspace(getattr(ctx, "workspace_root", None), p)),
            None,
        )
        if outside is not None:
            return ActionInfo(
                ACTION_WRITE_OUTSIDE,
                label="修改工作区外的文件",
                paths=paths,
                risk_note=f"目标路径在工作目录之外：{outside}",
            )
        return ActionInfo(ACTION_WRITE, paths=paths)

    if tool_name in _NETWORK_TOOLS:
        return ActionInfo(ACTION_NETWORK)

    if tool_name in _MANAGE_TOOLS:
        return ActionInfo(ACTION_MANAGE)

    if tool_name in _DESKTOP_WRITE_TOOLS:
        return ActionInfo(ACTION_DESKTOP, risk_note="将真实操作鼠标键盘")

    if tool_name in _DESKTOP_READ_TOOLS:
        return ActionInfo(ACTION_READ, label="读取屏幕内容")

    if tool_name in _MEMORY_WRITE_TOOLS:
        # plan-537-2256：写入记忆库（应用自身数据，不触碰用户文件系统）
        return ActionInfo(ACTION_WRITE, label="写入记忆")

    if tool_name in _READ_TOOLS:
        if tool_name in _PATH_READ_TOOLS:
            paths = target_paths(tool_name, args)
            outside = next(
                (p for p in paths if not is_within_workspace(getattr(ctx, "workspace_root", None), p)),
                None,
            )
            if outside is not None:
                return ActionInfo(
                    ACTION_READ_OUTSIDE,
                    label="读取工作区外的文件",
                    paths=paths,
                    risk_note=f"路径在工作目录之外：{outside}",
                )
            return ActionInfo(ACTION_READ, paths=paths)
        return ActionInfo(ACTION_READ)

    # 未登记工具：按风险等级兜底（不再默认放行，避免新增工具绕过裁决）
    if risk_level == "high":
        return ActionInfo(ACTION_EXEC_RISKY, label=f"执行 {tool_name}", risk_note="高风险操作")
    if risk_level == "low":
        return ActionInfo(ACTION_READ, label=f"调用 {tool_name}")
    return ActionInfo(ACTION_EXEC_NORMAL, label=f"调用 {tool_name}")


# ══════════════════════════════════════════════════════════
# 执行模式边界（唯一保留的硬拒绝来源）
# ══════════════════════════════════════════════════════════
def _mode_kind(execution_mode: str) -> str:
    """模式名 → 判定档（plan-560-2311 C1）。

    事实源在 `permission_profile_service.resolve_mode_kind`——本模块不能反向 import
    `subagent_capabilities`（后者已 import 本模块，会成环），所以归一下沉到服务层。
    这里延迟 import + 缓存，既避免模块加载期的依赖顺序问题，也不让每次裁决都走 import 机制。

    兜底：服务层不可用（配置读取异常等）时退回原名——此时行为与改造前完全一致，
    不会因归一失败而放大拒绝面。
    """
    global _RESOLVE_MODE_KIND
    if _RESOLVE_MODE_KIND is None:
        try:
            from app.services.permission_profile_service import (
                resolve_mode_kind as _impl,
            )

            _RESOLVE_MODE_KIND = _impl
        except Exception:  # noqa: BLE001 — 服务层不可用时按原名判定（旧行为）
            logger.warning("[approval] 模式归一事实源不可用(按原名判定)", exc_info=True)
            return execution_mode
    try:
        return _RESOLVE_MODE_KIND(execution_mode)
    except Exception:  # noqa: BLE001 — 单个模式名解析失败不阻断裁决
        logger.warning("[approval] 模式归一失败 mode=%s(按原名判定)", execution_mode, exc_info=True)
        return execution_mode


def _mode_boundary(
    info: ActionInfo, execution_mode: str, tool_name: str, args: dict, ctx,
) -> str | None:
    """返回拒绝原因；None 表示未越界。

    `execution_mode` 必须是**判定档**（`_mode_kind` 的返回值，即 readonly/plan/agent/workflow），
    不是原始模式名——自定义 kind=readonly/plan 模式只有归一后才命中下面的等值分支。
    """
    if execution_mode == MODE_READONLY:
        if info.action in (ACTION_WRITE, ACTION_WRITE_OUTSIDE):
            return "只读模式不允许写文件"
        if info.action == ACTION_DELETE:
            return "只读模式不允许删除文件或执行删除命令"
        if info.action in (ACTION_EXEC_NORMAL, ACTION_EXEC_RISKY):
            return "只读模式仅允许只读命令，禁止通过终端修改或创建文件"
        if info.action == ACTION_MANAGE:
            return "只读模式不允许管理系统进程"
        if info.action == ACTION_DESKTOP:
            return "只读模式不允许操作电脑（鼠标/键盘）"
        if info.action in (ACTION_BROWSER, ACTION_BROWSER_LOCAL):
            return "只读模式不允许操作网页（点击/输入/执行脚本），只能打开与读取网页"
        return None

    if execution_mode == MODE_PLAN:
        if info.action in (ACTION_WRITE, ACTION_WRITE_OUTSIDE):
            # 唯一例外：写计划文档（ai/*.md）
            if tool_name == "fs_write":
                paths = target_paths(tool_name, args)
                if paths and is_plan_doc_path(getattr(ctx, "workspace_root", None), paths[0]):
                    return None
            # 编辑类工具（原地改写已有文件）：即使目标是 ai/*.md 计划文档也无例外——
            # 文案明确指向「用 fs_write 重写新文档」，避免模型误读为"目标在 ai/ 下
            # 就该放行"而换工具反复重试（用户反馈：多轮调整计划时 AI 反复尝试编辑
            # 历史方案文档，全部被拒）。
            if tool_name in _EDIT_TOOLS:
                return (
                    "计划模式不支持编辑已有文件（包括历史方案文档）；"
                    "调整方案请用 fs_write 重新写出完整的新方案文档，不要编辑旧文档"
                )
            return "计划模式只能写入计划文档（ai/*.md），其余写操作一律不放开"
        if info.action == ACTION_DELETE:
            return "计划模式不允许删除文件或执行删除命令"
        if info.action in (ACTION_EXEC_NORMAL, ACTION_EXEC_RISKY):
            return "计划模式仅允许只读命令，禁止通过终端修改或创建文件"
        if info.action == ACTION_MANAGE:
            return "计划模式不允许管理系统进程"
        if info.action == ACTION_DESKTOP:
            return "计划模式不允许操作电脑（鼠标/键盘）"
        if info.action in (ACTION_BROWSER, ACTION_BROWSER_LOCAL):
            return "计划模式不允许操作网页（点击/输入/执行脚本），只能打开与读取网页"
        return None

    return None


# ══════════════════════════════════════════════════════════
# 裁决
# ══════════════════════════════════════════════════════════
def decide(
    tool_name: str,
    args: dict,
    ctx,
    *,
    risk_level: str = "low",
    rules: list | None = None,
) -> Decision:
    """唯一裁决点：给出 allow / ask / deny。

    `rules` 为调用方预先查好的 exec_policy 规则列表（本函数保持同步，便于在
    executor 内联调用；规则取用需要 db 会话，属调用方职责）。

    判定顺序：
      1. 执行模式边界 → deny（能力边界，不进入审批）
      2. exec_policy 规则 → deny 最高优先；allow 仅在非 ask 模式下生效
         （用户口径：「始终允许」对询问审批无效）
      3. 权限模式策略矩阵
    """
    execution_mode = normalize_execution_mode(getattr(ctx, "permission_mode", None))
    approval_mode = normalize_approval_mode(getattr(ctx, "approval_mode", None))

    info = classify(tool_name, args, ctx, risk_level=risk_level)

    # 1. 执行模式硬边界
    # plan-560-2311 C1：边界判定改用按 kind 归一的判定档。此前直接拿模式名做等值比较，
    # 自定义 kind=readonly/plan 模式与内置字面量都不相等 → 硬边界整体被跳过（可写可执行）。
    # 只在判定处归一，不改写会话里存着的模式名（自定义模式名必须原样保留）。
    boundary = _mode_boundary(
        info, _mode_kind(execution_mode), tool_name, args or {}, ctx,
    )
    if boundary:
        return Decision(
            VERDICT_DENY, info.action, label=info.label,
            reason=boundary, risk_note=info.risk_note,
        )

    # 2. 显式用户规则（执行策略页配置 / 审批卡「始终允许」落库）
    if rules:
        try:
            from app.services import exec_policy_service

            decision, just = exec_policy_service.match_tool_rule(rules, tool_name)
            if decision is None and tool_name in _COMMAND_TOOLS:
                decision, just = exec_policy_service.match_rule(
                    rules, str((args or {}).get("command") or ""),
                )
            if decision == "deny":
                return Decision(
                    VERDICT_DENY, info.action, label=info.label,
                    reason=just or f"执行策略已禁止 {tool_name}",
                    risk_note=info.risk_note,
                )
            if decision == "allow" and approval_mode != APPROVAL_ASK:
                return Decision(
                    VERDICT_ALLOW, info.action, label=info.label,
                    reason=just or "执行策略已放行", risk_note=info.risk_note,
                )
        except Exception:
            logger.warning("exec_policy 规则匹配异常(忽略)", exc_info=True)

    # 3. 权限模式策略矩阵
    verdict = _MATRIX.get(info.action, {}).get(approval_mode, VERDICT_ASK)
    return Decision(
        verdict, info.action, label=info.label,
        reason="" if verdict == VERDICT_ALLOW else info.risk_note,
        risk_note=info.risk_note,
    )


def can_run_in_parallel(tool_name: str, args: dict, ctx, *, risk_level: str = "low") -> bool:
    """plan-522-2209：该调用能否与其他并发安全的调用同时执行。

    判定口径：按「动作类别 × 权限模式」矩阵**确定放行**才可并行。原因是审批是互斥交
    互——ASK 会弹一张审批卡，批内多个调用同时问会一次冒出多张卡，用户既分不清先后、
    也无法判断批哪一张；DENY 则本就不执行，没有并行的意义。两者都必须独占串行。

    只做静态裁决（不查 exec_policy 规则）：调用方需要同步判定，规则取用要 db 会话。
    用户「始终允许」规则只会让判定更宽松，这里保守地按矩阵判——最多少并行几个调用。
    """
    approval_mode = normalize_approval_mode(getattr(ctx, "approval_mode", None))
    info = classify(tool_name, args or {}, ctx, risk_level=risk_level)
    verdict = _MATRIX.get(info.action, {}).get(approval_mode, VERDICT_ASK)
    return verdict == VERDICT_ALLOW
