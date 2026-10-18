"""系统设置：工作目录、全局规则、记忆开关、AI 规则来源等桌面版可配置项。"""
import json
import os
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

from app.core.config import settings, update_workspace_root
from app.core.desktop_marks import normalize_verify_mode

router = APIRouter()

_USER_CONFIG_PATH = Path(
    os.environ.get("CHATCODER_USER_CONFIG", str(Path.home() / ".chatcoder" / "config.json"))
)

# ── AI 规则来源识别 ──
# plan-114-450：来源表统一由 app.orchestration.rules_loader（RULE_SOURCE_LABELS / source_of）提供，
# 路由不再自带一份（此前两份不一致：界面能开关的来源 ≠ 注入时过滤的来源）。


def _is_dir_candidate(path: Path) -> bool:
    """规则候选可能是文件或目录（如 .trae/rules 目录）。"""
    return path.exists()


# ── 通用配置读写 ──

def _load_config() -> dict:
    """读取持久化用户配置。"""
    try:
        if _USER_CONFIG_PATH.exists():
            return json.loads(_USER_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _save_config(data: dict) -> None:
    """写入持久化用户配置。"""
    try:
        _USER_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _USER_CONFIG_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


# ── 工作目录 ──

class WorkspaceOut(BaseModel):
    workspace_root: str
    exists: bool


class WorkspaceIn(BaseModel):
    workspace_root: str


def _persist_workspace(path: str) -> None:
    """持久化工作目录到用户配置文件,下次启动自动恢复。"""
    data = _load_config()
    data["workspace_root"] = path
    _save_config(data)


# plan-20261018-operator-boost：电脑操控默认值修订。键 → (旧默认, 新默认)。
# 设置页「修改即保存」会把整页字段一并落盘，因此「已落盘」不代表用户改过；
# 仍等于旧默认值的项视为未定制，一次性升级为新默认；用户改成其它值的保持不动。
_DESKTOP_DEFAULTS_REV = 2
_DESKTOP_DEFAULT_UPGRADES: dict[str, tuple[object, object]] = {
    "desktop_enabled": (False, True),
    "desktop_screenshot_quality": (75, 100),
    "desktop_screenshot_max_dim": (1600, 0),
    "desktop_mark_shot": (True, False),
}


def _upgrade_desktop_defaults(data: dict) -> None:
    """把仍等于旧默认值的电脑操控配置升级为新默认（幂等，只执行一次）。"""
    try:
        rev = int(data.get("desktop_defaults_rev") or 0)
    except (TypeError, ValueError):
        rev = 0
    if rev >= _DESKTOP_DEFAULTS_REV:
        return
    for key, (old, new) in _DESKTOP_DEFAULT_UPGRADES.items():
        if key in data and data[key] == old:
            data[key] = new
    data["desktop_defaults_rev"] = _DESKTOP_DEFAULTS_REV
    _save_config(data)


def load_persisted_workspace() -> None:
    """启动时从用户配置恢复工作目录及全局设置。"""
    data = _load_config()
    path = data.get("workspace_root")
    if path and Path(path).exists():
        update_workspace_root(path)
    # v4.2: 启动时恢复自动审批等持久化设置到运行时 settings
    if "auto_approve_tools" in data:
        settings.auto_approve_tools = bool(data["auto_approve_tools"])
    if "force_approval_tools" in data:
        settings.force_approval_tools = str(data["force_approval_tools"])
    if "session_token_budget" in data:
        settings.session_token_budget = int(data["session_token_budget"])
    if "enhanced_search" in data:
        settings.enhanced_search = bool(data["enhanced_search"])
    if "show_todos" in data:
        settings.show_todos = bool(data["show_todos"])
    if "show_reasoning" in data:
        settings.show_reasoning = bool(data["show_reasoning"])
    # plan-156-619 S2：消息搜索入口开关（默认关闭，关闭时只隐藏入口按钮，Ctrl+F 仍可用）
    if "show_message_search" in data:
        settings.show_message_search = bool(data["show_message_search"])
    if "memory_enabled" in data:
        settings.auto_memory_enabled = bool(data["memory_enabled"])
    if "agent_max_steps" in data:
        try:
            settings.agent_max_steps = int(data["agent_max_steps"])
        except (TypeError, ValueError):
            pass
    # v45: 启动时恢复异常自动重试策略
    if "agent_retry_count" in data:
        try:
            settings.agent_retry_count = max(0, int(data["agent_retry_count"]))
        except (TypeError, ValueError):
            pass
    if "agent_retry_intervals" in data:
        settings.agent_retry_intervals = str(data["agent_retry_intervals"] or "")
    if "browser_enabled" in data:
        settings.browser_enabled = bool(data["browser_enabled"])
    if "browser_headless" in data:
        settings.browser_headless = bool(data["browser_headless"])
    # plan-20261018-operator-boost：电脑操控默认值升级（只升级「仍等于旧默认值」的项，用户改过的不动）
    _upgrade_desktop_defaults(data)
    # plan-334-1661: 电脑操控相关开关
    # plan-365-1757: 追加「点准」开关（截图元素编号 / 点击前校验）
    # plan-365-1769: desktop_verify_click 由布尔改为三档字符串（off/lenient/strict），
    # 不再走布尔循环——旧布尔值经 normalize_verify_mode 迁移。
    _DESKTOP_BOOL_KEYS = (
        "desktop_enabled", "desktop_plain_ops_enabled", "desktop_browser_ops_enabled",
        "desktop_require_foreground", "desktop_recipe_enabled",
        "desktop_mark_shot",
    )
    for _k in _DESKTOP_BOOL_KEYS:
        if _k in data:
            setattr(settings, _k, bool(data[_k]))
    if "desktop_verify_click" in data:
        settings.desktop_verify_click = normalize_verify_mode(data["desktop_verify_click"])
    if "desktop_destructive_words" in data:
        settings.desktop_destructive_words = str(data["desktop_destructive_words"] or "")
    if "desktop_screenshot_quality" in data:
        try:
            settings.desktop_screenshot_quality = max(10, min(100, int(data["desktop_screenshot_quality"])))
        except (TypeError, ValueError):
            pass
    if "desktop_screenshot_max_dim" in data:
        try:
            settings.desktop_screenshot_max_dim = max(0, int(data["desktop_screenshot_max_dim"]))
        except (TypeError, ValueError):
            pass
    # v31.2: 启动时恢复代理设置（运行时字段 + 环境变量，供 web 工具走代理）
    if "http_proxy" in data:
        _proxy = str(data["http_proxy"] or "")
        settings.http_proxy = _proxy
        if _proxy:
            os.environ["HTTP_PROXY"] = _proxy
            os.environ["HTTPS_PROXY"] = _proxy
    if "plan_mode_allow_outside_access" in data:
        settings.plan_mode_allow_outside_access = bool(data["plan_mode_allow_outside_access"])
    if "sandbox_mode" in data:
        _sm = str(data["sandbox_mode"])
        if _sm in ("workspace-write", "read-only", "danger-full-access"):
            settings.sandbox_mode = _sm
    # v36 (plan-321-1600 R2): 启动时恢复「工作目录外读取自动审批」开关
    if "auto_approve_outside_read" in data:
        settings.auto_approve_outside_read = bool(data["auto_approve_outside_read"])
    # v36 (plan-321-1600 R3): 启动时恢复「额度自动透支」开关
    if "auto_overdraft_on_quota_exceeded" in data:
        settings.auto_overdraft_on_quota_exceeded = bool(data["auto_overdraft_on_quota_exceeded"])
    # v46: 启动时恢复「额度自动重置」开关
    if "auto_reset_on_quota_exceeded" in data:
        settings.auto_reset_on_quota_exceeded = bool(data["auto_reset_on_quota_exceeded"])
    # plan-278-1391: 启动时恢复压缩触发阈值（默认 0.90，范围 0.50~0.95）
    if "auto_compact_threshold_ratio" in data:
        try:
            _ratio = float(data["auto_compact_threshold_ratio"])
        except (TypeError, ValueError):
            _ratio = None
        if _ratio is not None and 0.50 <= _ratio <= 0.95:
            settings.auto_compact_threshold_ratio = round(_ratio, 2)
    # v36: 启动时恢复子代理并发治理（同时运行上限 / 每轮总量上限）
    if "max_concurrent_subagents" in data:
        try:
            settings.max_concurrent_subagents = max(1, min(16, int(data["max_concurrent_subagents"])))
        except (TypeError, ValueError):
            pass
    if "max_subagents_per_turn" in data:
        try:
            settings.max_subagents_per_turn = max(1, min(32, int(data["max_subagents_per_turn"])))
        except (TypeError, ValueError):
            pass
    # plan-156-619 S4d：子会话默认模型与思考深度（设置中心「子代理」面板只留这两项）
    if "subagent_model_id" in data:
        try:
            settings.subagent_model_id = max(0, int(data["subagent_model_id"]))
        except (TypeError, ValueError):
            pass
    if "subagent_reasoning_effort" in data:
        settings.subagent_reasoning_effort = str(data["subagent_reasoning_effort"] or "")[:20]
    if "subagent_enabled" in data:
        settings.subagent_enabled = bool(data["subagent_enabled"])
    # plan-20261018-operator-boost：GUI 操作交给操控子会话 / 操控子会话思考深度
    if "operator_subsession_enabled" in data:
        settings.operator_subsession_enabled = bool(data["operator_subsession_enabled"])
    if "operator_reasoning_effort" in data:
        settings.operator_reasoning_effort = str(data["operator_reasoning_effort"] or "lowest")[:20]
    # plan-156-619 S7：工具卡栏目的全局默认展开策略
    if "tool_card_sections_expanded" in data:
        settings.tool_card_sections_expanded = bool(data["tool_card_sections_expanded"])
    # 工作流运行时：恢复并发上限 / 返工 / 预算；取值与 PUT 共用同一夹紧口径
    if "workflow_enabled" in data:
        settings.workflow_enabled = bool(data["workflow_enabled"])
    for _flag in (
        "workflow_confirm_before_start",
        "workflow_auto_resume_on_network",
        "workflow_require_checks",
        "workflow_rework_confirm",
        "workflow_auto_merge",
    ):
        if _flag in data:
            setattr(settings, _flag, bool(data[_flag]))
    if "workflow_composition_mode" in data:
        settings.workflow_composition_mode = _composition_mode(data["workflow_composition_mode"])
    if "workflow_conflict_policy" in data:
        settings.workflow_conflict_policy = _conflict_policy(data["workflow_conflict_policy"])
    if "workflow_isolation_default" in data:
        settings.workflow_isolation_default = _isolation_default(data["workflow_isolation_default"])
    for _key, (_lo, _hi) in _WORKFLOW_INT_BOUNDS.items():
        if _key in data:
            try:
                setattr(settings, _key, max(_lo, min(_hi, int(data[_key]))))
            except (TypeError, ValueError):
                pass
    # plan-156-624：升级默认值——旧版本没有这两个键时补写为开启并落盘，用户升级后
    # 无需手动去「设置 → 子代理 / 常规」打开子会话与并行分派。只补缺失键：
    # 已存在的 false 是用户显式关闭，不覆盖。
    _defaults_added = False
    for _key, _default in (("subagent_enabled", True), ("parallel_subsession_hint", True)):
        if _key not in data:
            data[_key] = _default
            setattr(settings, _key, _default)
            _defaults_added = True
    if _defaults_added:
        _save_config(data)


@router.get("/settings/workspace", response_model=WorkspaceOut)
async def get_workspace() -> WorkspaceOut:
    path = settings.workspace_root
    return WorkspaceOut(workspace_root=os.path.abspath(path), exists=Path(path).exists())


@router.put("/settings/workspace", response_model=WorkspaceOut)
async def set_workspace(body: WorkspaceIn) -> WorkspaceOut:
    target = Path(body.workspace_root).expanduser()
    target.mkdir(parents=True, exist_ok=True)
    update_workspace_root(str(target))
    _persist_workspace(str(target))
    return WorkspaceOut(workspace_root=str(target.resolve()), exists=True)


# ── v1.0: 全局设置(记忆开关 + 全局规则) ──

class GlobalSettingsOut(BaseModel):
    memory_enabled: bool = True
    global_rules: str = ""
    auto_compact_enabled: bool = True
    # plan-278-1391: 上下文压缩触发阈值（占模型窗口比例）。默认 0.90，可配置范围 0.50~0.95。
    auto_compact_threshold_ratio: float = 0.90
    language: str = "zh"
    # v1.0: Agent/安全配置
    # plan-75-332：auto_approve_tools / force_approval_tools / sandbox_mode /
    # plan_mode_allow_outside_access / auto_approve_outside_read 均已**废弃**——
    # 是否放行统一由「执行模式 × 权限模式」裁决（见 orchestration/approval_policy.py）。
    # 字段保留仅为兼容旧配置文件回读（不做物理删除），前端不再展示。
    auto_approve_tools: bool = False
    force_approval_tools: str = "terminal_exec,ci_run,browser_navigate,browser_click,browser_type"
    session_token_budget: int = 200_000
    # v2.2 (对齐 zcode 3.15/3.18): 常规面板补项
    terminal_shell: str = "auto"  # auto/pwsh/powershell/cmd/git-bash
    terminal_font: str = ""  # 留空继承系统终端字体
    http_proxy: str = ""
    enhanced_search: bool = True  # ripgrep 增强搜索
    show_todos: bool = True  # 消息流显示 todos 卡片
    show_reasoning: bool = True  # 消息流显示 reasoning 块
    # plan-156-619 S2：消息区搜索入口按钮（左上角）。默认关闭；关闭只隐藏入口，Ctrl+F 仍可唤起
    show_message_search: bool = False
    # plan-156-621：每轮用户消息注入「优先用子会话并行处理」的提示词（默认开启）
    parallel_subsession_hint: bool = True
    # v3.0 (plan-88): 计划模式允许访问工作区外路径
    plan_mode_allow_outside_access: bool = False
    # v32 (plan-89): 沙箱模式（三态：workspace-write / read-only / danger-full-access）
    sandbox_mode: str = "workspace-write"
    # v36 (plan-321-1600 R2): 工作目录外读取自动审批（开启后不再弹审批卡）
    auto_approve_outside_read: bool = False
    # v36 (plan-321-1600 R3): ta3 额度自动透支（报错时查额度，日额度>=100% 自动透支一次）
    auto_overdraft_on_quota_exceeded: bool = False
    # v46: ta3 额度自动重置（报错时查额度，窗口用尽则自动提交重置；日窗走透支）
    auto_reset_on_quota_exceeded: bool = False
    # Agent 最大步数（200 / 500 / 1000 / 0=不限制）
    agent_max_steps: int = 1000
    # v45: 异常自动重试策略（任何报错都按此重试，穷尽后才停止并显示报错）
    # agent_retry_count: 重试次数（0 = 不重试）
    # agent_retry_intervals: 每次重试前的等待秒数（逗号分隔，默认 10,20,30）
    agent_retry_count: int = 3
    agent_retry_intervals: str = "10,20,30"
    # 浏览器自动化开关与无头模式
    browser_enabled: bool = True
    browser_headless: bool = True
    # plan-334-1661: 电脑操控（内核常驻的内置工具集，不作为 MCP 注册）
    # 注意：这些字段必须同时出现在 Out 与 In 两个模型里——只加 In 会导致
    # _global_settings_out 传入的 kwarg 被 pydantic 静默丢弃，前端表现为"改了不保存"。
    desktop_enabled: bool = True
    desktop_plain_ops_enabled: bool = True
    desktop_browser_ops_enabled: bool = True
    desktop_require_foreground: bool = True
    desktop_screenshot_quality: int = 100
    desktop_screenshot_max_dim: int = 0
    desktop_recipe_enabled: bool = True
    # plan-365-1757: 「点准」开关（截图元素编号 / 点击前校验 / 破坏性操作词表）
    # plan-20261018-operator-boost：编号默认关闭
    desktop_mark_shot: bool = False
    # plan-365-1769 F5: 点击前校验档位（off / lenient / strict），默认宽松
    desktop_verify_click: str = "lenient"
    desktop_destructive_words: str = ""
    # v36: 子代理并发治理（设置中心「子代理」面板可配，运行时立即生效）
    max_concurrent_subagents: int = 6   # 同时运行上限（超出排队等待，1~16）
    max_subagents_per_turn: int = 10    # 每轮派发总量上限（含已完成，1~32）
    # plan-156-619 S4d：子会话默认模型 / 思考深度（0 / 空 = 跟随主代理）。
    # 工具范围与执行模式由主代理在创建时决定，设置里只保留这两项可调项。
    subagent_model_id: int = 0
    subagent_reasoning_effort: str = ""
    # plan-156-619 S4e：子代理 / 子会话总开关
    subagent_enabled: bool = True
    # plan-20261018-operator-boost：GUI 操作交给操控子会话 / 操控子会话思考深度
    operator_subsession_enabled: bool = True
    operator_reasoning_effort: str = "lowest"
    # plan-156-619 S7：工具卡栏目全局默认展开策略（true = 默认全展开）
    tool_card_sections_expanded: bool = True
    # plan-75-332: 审批卡「解释」专用模型 / 思考深度（0/空 = 跟随会话）
    approval_explain_model_id: int = 0
    approval_explain_reasoning_effort: str = ""
    # 工作流运行时（设置 → 工作流）：并发上限 0 = 不限
    workflow_enabled: bool = True
    workflow_max_concurrent_global: int = 3
    workflow_max_concurrent_per_session: int = 2
    workflow_max_parallel_nodes_per_flow: int = 2
    workflow_default_max_rework: int = 2
    workflow_token_budget_per_run: int = 0
    workflow_confirm_before_start: bool = True
    workflow_auto_resume_on_network: bool = False
    workflow_composition_mode: str = "runtime"
    workflow_hot_files: str = ""
    workflow_require_checks: bool = True
    workflow_rework_confirm: bool = True
    workflow_conflict_policy: str = "queue"
    workflow_auto_merge: bool = True
    workflow_isolation_default: str = "shared"


# 工作流数值设置的取值范围（0 = 不限，仅并发上限支持）；PUT 与启动恢复共用同一口径
_WORKFLOW_INT_BOUNDS: dict[str, tuple[int, int]] = {
    "workflow_max_concurrent_global": (0, 16),
    "workflow_max_concurrent_per_session": (0, 16),
    "workflow_max_parallel_nodes_per_flow": (1, 4),
    "workflow_default_max_rework": (0, 3),
    "workflow_token_budget_per_run": (0, 100_000_000),
}


_COMPOSITION_MODES = ("runtime", "presets_only")


def _composition_mode(value: object) -> str:
    """工作流组装方式只接受两种取值，其它输入回落为默认的 runtime。"""
    text = str(value or "").strip()
    return text if text in _COMPOSITION_MODES else "runtime"


_CONFLICT_POLICIES = ("queue", "ask")
_ISOLATION_MODES = ("shared", "worktree")


def _conflict_policy(value: object) -> str:
    """同文件冲突策略只接受 queue / ask，其它输入回落为默认的 queue。"""
    text = str(value or "").strip()
    return text if text in _CONFLICT_POLICIES else "queue"


def _isolation_default(value: object) -> str:
    """默认隔离方式只接受 shared / worktree，其它输入回落为默认的 shared。"""
    text = str(value or "").strip()
    return text if text in _ISOLATION_MODES else "shared"


class GlobalSettingsIn(BaseModel):
    memory_enabled: bool | None = None
    global_rules: str | None = None
    auto_compact_enabled: bool | None = None
    # plan-278-1391: 压缩触发阈值比例（0.50~0.95）
    auto_compact_threshold_ratio: float | None = None
    language: str | None = None
    # v1.0: Agent/安全配置
    auto_approve_tools: bool | None = None
    force_approval_tools: str | None = None
    session_token_budget: int | None = None
    # v2.2 (对齐 zcode 3.15/3.18): 常规面板补项
    terminal_shell: str | None = None
    terminal_font: str | None = None
    http_proxy: str | None = None
    enhanced_search: bool | None = None
    show_todos: bool | None = None
    show_reasoning: bool | None = None
    show_message_search: bool | None = None
    # plan-156-621：优先使用子会话并行处理的提示词注入开关
    parallel_subsession_hint: bool | None = None
    plan_mode_allow_outside_access: bool | None = None
    sandbox_mode: str | None = None
    # v36 (plan-321-1600 R2): 工作目录外读取自动审批
    auto_approve_outside_read: bool | None = None
    # v36 (plan-321-1600 R3): ta3 额度自动透支
    auto_overdraft_on_quota_exceeded: bool | None = None
    # v46: ta3 额度自动重置
    auto_reset_on_quota_exceeded: bool | None = None
    agent_max_steps: int | None = None
    # v45: 异常自动重试策略
    agent_retry_count: int | None = None
    agent_retry_intervals: str | None = None
    browser_enabled: bool | None = None
    browser_headless: bool | None = None
    # plan-334-1661: 电脑操控（集中一处，_global_settings_out 与 PUT 都读这里）
    desktop_enabled: bool | None = None
    desktop_plain_ops_enabled: bool | None = None
    desktop_browser_ops_enabled: bool | None = None
    desktop_require_foreground: bool | None = None
    desktop_screenshot_quality: int | None = None
    desktop_screenshot_max_dim: int | None = None
    desktop_recipe_enabled: bool | None = None
    # plan-365-1757: 「点准」开关
    desktop_mark_shot: bool | None = None
    # plan-365-1769 F5: 三档字符串；接受 bool 旧值（归一化时兼容 true→lenient / false→off）
    desktop_verify_click: str | bool | None = None
    desktop_destructive_words: str | None = None
    # v36: 子代理并发治理
    max_concurrent_subagents: int | None = None
    max_subagents_per_turn: int | None = None
    # plan-156-619 S4d：子会话默认模型 / 思考深度
    subagent_model_id: int | None = None
    subagent_reasoning_effort: str | None = None
    # plan-156-619 S4e：子代理 / 子会话总开关
    subagent_enabled: bool | None = None
    # plan-20261018-operator-boost：操控子会话
    operator_subsession_enabled: bool | None = None
    operator_reasoning_effort: str | None = None
    # plan-156-619 S7：工具卡栏目全局默认展开策略
    tool_card_sections_expanded: bool | None = None
    # plan-75-332: 审批卡「解释」专用模型 / 思考深度（0/空 = 跟随会话）
    approval_explain_model_id: int | None = None
    approval_explain_reasoning_effort: str | None = None
    # 工作流运行时
    workflow_enabled: bool | None = None
    workflow_max_concurrent_global: int | None = None
    workflow_max_concurrent_per_session: int | None = None
    workflow_max_parallel_nodes_per_flow: int | None = None
    workflow_default_max_rework: int | None = None
    workflow_token_budget_per_run: int | None = None
    workflow_confirm_before_start: bool | None = None
    workflow_auto_resume_on_network: bool | None = None
    workflow_composition_mode: str | None = None
    workflow_hot_files: str | None = None
    workflow_require_checks: bool | None = None
    workflow_rework_confirm: bool | None = None
    workflow_conflict_policy: str | None = None
    workflow_auto_merge: bool | None = None
    workflow_isolation_default: str | None = None


def _global_settings_out(data: dict) -> GlobalSettingsOut:
    """按持久化数据组装全局设置响应。

    v46 修复：此前 GET / PUT 各自手写一遍字段列表，v36 新增的
    auto_approve_outside_read 与 auto_overdraft_on_quota_exceeded 只加进了
    请求模型与持久化分支，却漏在这两处回填 —— 用户勾选后值确实已落盘，
    但重新进入面板读到的是类默认值，表现为"没保存成功"。
    现两条路径统一走本函数，新增字段只需改一处，不会再漏。
    """
    return GlobalSettingsOut(
        memory_enabled=data.get("memory_enabled", True),
        global_rules=data.get("global_rules", ""),
        auto_compact_enabled=data.get("auto_compact_enabled", True),
        # plan-278-1391: 压缩触发阈值（默认取运行时 settings，即 0.90）
        auto_compact_threshold_ratio=float(
            data.get("auto_compact_threshold_ratio", settings.auto_compact_threshold_ratio)
        ),
        language=data.get("language", "zh"),
        auto_approve_tools=data.get("auto_approve_tools", settings.auto_approve_tools),
        force_approval_tools=data.get("force_approval_tools", settings.force_approval_tools),
        session_token_budget=data.get("session_token_budget", 200_000),
        # v2.2: 常规面板补项
        terminal_shell=data.get("terminal_shell", "auto"),
        terminal_font=data.get("terminal_font", ""),
        http_proxy=data.get("http_proxy", ""),
        enhanced_search=data.get("enhanced_search", True),
        show_todos=data.get("show_todos", True),
        show_reasoning=data.get("show_reasoning", True),
        show_message_search=data.get("show_message_search", False),
        # plan-156-621：优先使用子会话并行处理的提示词注入（默认开启）。
        # plan-156-622：只有持久化值**显式为 false** 才关闭；键缺失或值为 null
        # （旧配置文件、手工编辑）都回落到运行时默认 true，避免默认值被翻成关闭。
        parallel_subsession_hint=(
            data["parallel_subsession_hint"]
            if isinstance(data.get("parallel_subsession_hint"), bool)
            else bool(getattr(settings, "parallel_subsession_hint", True))
        ),
        plan_mode_allow_outside_access=data.get("plan_mode_allow_outside_access", False),
        sandbox_mode=data.get("sandbox_mode", settings.sandbox_mode),
        # v36 (plan-321-1600 R2/R3): 两项自动审批/自动额度开关（回填持久化值）
        auto_approve_outside_read=bool(
            data.get("auto_approve_outside_read", settings.auto_approve_outside_read)
        ),
        auto_overdraft_on_quota_exceeded=bool(
            data.get("auto_overdraft_on_quota_exceeded", settings.auto_overdraft_on_quota_exceeded)
        ),
        # v46: 额度自动重置（与自动透支同为额度恢复开关）
        auto_reset_on_quota_exceeded=bool(
            data.get("auto_reset_on_quota_exceeded", settings.auto_reset_on_quota_exceeded)
        ),
        agent_max_steps=data.get("agent_max_steps", settings.agent_max_steps),
        agent_retry_count=data.get("agent_retry_count", settings.agent_retry_count),
        agent_retry_intervals=data.get("agent_retry_intervals", settings.agent_retry_intervals),
        browser_enabled=data.get("browser_enabled", settings.browser_enabled),
        browser_headless=data.get("browser_headless", settings.browser_headless),
        # plan-334-1661: 电脑操控（新增字段只需改这里，不会再漏——见本函数 docstring）
        desktop_enabled=data.get("desktop_enabled", getattr(settings, "desktop_enabled", True)),
        desktop_plain_ops_enabled=data.get(
            "desktop_plain_ops_enabled", getattr(settings, "desktop_plain_ops_enabled", True)),
        desktop_browser_ops_enabled=data.get(
            "desktop_browser_ops_enabled", getattr(settings, "desktop_browser_ops_enabled", True)),
        desktop_require_foreground=data.get(
            "desktop_require_foreground", getattr(settings, "desktop_require_foreground", True)),
        desktop_screenshot_quality=int(data.get(
            "desktop_screenshot_quality", getattr(settings, "desktop_screenshot_quality", 100))),
        desktop_screenshot_max_dim=int(data.get(
            "desktop_screenshot_max_dim", getattr(settings, "desktop_screenshot_max_dim", 0))),
        desktop_recipe_enabled=data.get(
            "desktop_recipe_enabled", getattr(settings, "desktop_recipe_enabled", True)),
        # plan-365-1757: 「点准」开关
        desktop_mark_shot=data.get(
            "desktop_mark_shot", getattr(settings, "desktop_mark_shot", False)),
        # plan-365-1769 F5: 三档归一化输出（旧布尔值自动迁移）
        desktop_verify_click=normalize_verify_mode(data.get(
            "desktop_verify_click", getattr(settings, "desktop_verify_click", "lenient"))),
        desktop_destructive_words=str(data.get(
            "desktop_destructive_words", getattr(settings, "desktop_destructive_words", "")) or ""),
        # plan-75-332: 审批卡「解释」专用模型 / 思考深度（0/空 = 跟随会话）
        approval_explain_model_id=int(data.get("approval_explain_model_id", 0) or 0),
        approval_explain_reasoning_effort=str(
            data.get("approval_explain_reasoning_effort", "")
            or getattr(settings, "approval_explain_reasoning_effort", "")
            or ""
        ),
        # v36: 子代理并发治理
        max_concurrent_subagents=data.get("max_concurrent_subagents", settings.max_concurrent_subagents),
        max_subagents_per_turn=data.get("max_subagents_per_turn", settings.max_subagents_per_turn),
        # plan-156-619 S4d：子会话默认模型 / 思考深度
        subagent_model_id=data.get("subagent_model_id", settings.subagent_model_id),
        subagent_reasoning_effort=data.get("subagent_reasoning_effort", settings.subagent_reasoning_effort),
        subagent_enabled=bool(data.get("subagent_enabled", settings.subagent_enabled)),
        operator_subsession_enabled=bool(data.get(
            "operator_subsession_enabled", settings.operator_subsession_enabled)),
        operator_reasoning_effort=str(data.get(
            "operator_reasoning_effort", settings.operator_reasoning_effort) or "lowest"),
        tool_card_sections_expanded=bool(data.get(
            "tool_card_sections_expanded", settings.tool_card_sections_expanded,
        )),
        # 工作流运行时：取值在 PUT 与启动恢复时已夹紧，此处只读
        **{k: int(data.get(k, getattr(settings, k))) for k in _WORKFLOW_INT_BOUNDS},
        workflow_enabled=bool(data.get("workflow_enabled", settings.workflow_enabled)),
        workflow_confirm_before_start=bool(
            data.get("workflow_confirm_before_start", settings.workflow_confirm_before_start)
        ),
        workflow_auto_resume_on_network=bool(
            data.get("workflow_auto_resume_on_network", settings.workflow_auto_resume_on_network)
        ),
        workflow_composition_mode=_composition_mode(
            data.get("workflow_composition_mode", settings.workflow_composition_mode)
        ),
        workflow_hot_files=str(data.get("workflow_hot_files", settings.workflow_hot_files) or ""),
        workflow_require_checks=bool(
            data.get("workflow_require_checks", settings.workflow_require_checks)
        ),
        workflow_rework_confirm=bool(
            data.get("workflow_rework_confirm", settings.workflow_rework_confirm)
        ),
        workflow_conflict_policy=_conflict_policy(
            data.get("workflow_conflict_policy", settings.workflow_conflict_policy)
        ),
        workflow_auto_merge=bool(data.get("workflow_auto_merge", settings.workflow_auto_merge)),
        workflow_isolation_default=_isolation_default(
            data.get("workflow_isolation_default", settings.workflow_isolation_default)
        ),
    )


@router.get("/settings/global", response_model=GlobalSettingsOut)
async def get_global_settings() -> GlobalSettingsOut:
    """读取全局设置(记忆开关、全局规则、Agent/安全配置、终端/显示选项等)。"""
    return _global_settings_out(_load_config())


class TerminalShellOption(BaseModel):
    value: str
    label: str
    available: bool


class TerminalFontOption(BaseModel):
    value: str
    label: str


class TerminalOptionsOut(BaseModel):
    shells: list[TerminalShellOption]
    fonts: list[TerminalFontOption]


def _detect_shells() -> list[TerminalShellOption]:
    """问题7: 按当前设备实际存在性探测可用 Shell（auto 恒可用）。"""
    import shutil
    from pathlib import Path as _P
    sys32 = _P(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"

    def _exists(p: str | None) -> bool:
        if not p:
            return False
        return _P(p).exists()

    pwsh = shutil.which("pwsh") or str(sys32 / "pwsh.exe")
    powershell = shutil.which("powershell") or str(sys32 / "WindowsPowerShell" / "v1.0" / "powershell.exe")
    cmd = shutil.which("cmd") or str(sys32 / "cmd.exe")
    git_bash = None
    try:
        from app.orchestration.tools.shell_env import resolve_git_bash
        git_bash = resolve_git_bash()
    except Exception:
        pass

    return [
        TerminalShellOption(value="auto", label="自动（按平台默认）", available=True),
        TerminalShellOption(value="pwsh", label="PowerShell 7 (pwsh)", available=_exists(pwsh)),
        TerminalShellOption(value="powershell", label="Windows PowerShell", available=_exists(powershell)),
        TerminalShellOption(value="cmd", label="命令提示符 (cmd)", available=_exists(cmd)),
        TerminalShellOption(value="git-bash", label="Git Bash", available=bool(git_bash)),
    ]


def _terminal_fonts() -> list[TerminalFontOption]:
    """问题8: 常用等宽字体候选，供终端字体下拉选择（value 传给终端渲染器）。"""
    return [
        TerminalFontOption(value="", label="继承系统终端字体"),
        TerminalFontOption(value="Cascadia Code", label="Cascadia Code"),
        TerminalFontOption(value="Consolas", label="Consolas"),
        TerminalFontOption(value="JetBrains Mono", label="JetBrains Mono"),
        TerminalFontOption(value="Fira Code", label="Fira Code"),
        TerminalFontOption(value="Source Code Pro", label="Source Code Pro"),
        TerminalFontOption(value="Sarasa Mono SC", label="Sarasa Mono SC（更纱黑体/等宽）"),
        TerminalFontOption(value="Microsoft YaHei", label="Microsoft YaHei（微软雅黑）"),
    ]


@router.get("/settings/terminal/options", response_model=TerminalOptionsOut)
async def get_terminal_options() -> TerminalOptionsOut:
    """问题7/8: 返回当前设备实际存在的终端 Shell 与候选等宽字体。"""
    return TerminalOptionsOut(shells=_detect_shells(), fonts=_terminal_fonts())


@router.put("/settings/global", response_model=GlobalSettingsOut)
async def set_global_settings(body: GlobalSettingsIn) -> GlobalSettingsOut:
    """更新全局设置。"""
    data = _load_config()
    if body.memory_enabled is not None:
        data["memory_enabled"] = body.memory_enabled
    if body.global_rules is not None:
        data["global_rules"] = body.global_rules
    if body.auto_compact_enabled is not None:
        data["auto_compact_enabled"] = body.auto_compact_enabled
    # plan-278-1391: 压缩触发阈值（夹紧到 0.50~0.95，非法值忽略），运行时立即生效
    if body.auto_compact_threshold_ratio is not None:
        try:
            _ratio = float(body.auto_compact_threshold_ratio)
        except (TypeError, ValueError):
            _ratio = None
        if _ratio is not None and 0.50 <= _ratio <= 0.95:
            data["auto_compact_threshold_ratio"] = round(_ratio, 2)
            settings.auto_compact_threshold_ratio = round(_ratio, 2)
    if body.language is not None:
        data["language"] = body.language
    # v1.0: Agent/安全配置
    if body.auto_approve_tools is not None:
        data["auto_approve_tools"] = body.auto_approve_tools
        settings.auto_approve_tools = body.auto_approve_tools
    if body.force_approval_tools is not None:
        data["force_approval_tools"] = body.force_approval_tools
        settings.force_approval_tools = body.force_approval_tools
    if body.session_token_budget is not None:
        data["session_token_budget"] = body.session_token_budget
        settings.session_token_budget = body.session_token_budget
    # v2.2: 常规面板补项
    if body.terminal_shell is not None:
        data["terminal_shell"] = body.terminal_shell
    if body.terminal_font is not None:
        data["terminal_font"] = body.terminal_font
    if body.http_proxy is not None:
        data["http_proxy"] = body.http_proxy
        # v31.2: 同步运行时 settings（http_client 显式读取，不依赖 trust_env）
        settings.http_proxy = body.http_proxy
        if body.http_proxy:
            os.environ["HTTP_PROXY"] = body.http_proxy
            os.environ["HTTPS_PROXY"] = body.http_proxy
        else:
            os.environ.pop("HTTP_PROXY", None)
            os.environ.pop("HTTPS_PROXY", None)
    if body.enhanced_search is not None:
        data["enhanced_search"] = body.enhanced_search
        settings.enhanced_search = body.enhanced_search
    if body.show_todos is not None:
        data["show_todos"] = body.show_todos
        settings.show_todos = body.show_todos
    if body.show_reasoning is not None:
        data["show_reasoning"] = body.show_reasoning
        settings.show_reasoning = body.show_reasoning
    if body.show_message_search is not None:
        data["show_message_search"] = body.show_message_search
        settings.show_message_search = body.show_message_search
    if body.parallel_subsession_hint is not None:
        data["parallel_subsession_hint"] = body.parallel_subsession_hint
        settings.parallel_subsession_hint = body.parallel_subsession_hint
    if body.plan_mode_allow_outside_access is not None:
        data["plan_mode_allow_outside_access"] = body.plan_mode_allow_outside_access
        settings.plan_mode_allow_outside_access = body.plan_mode_allow_outside_access
    if body.sandbox_mode is not None:
        if body.sandbox_mode in ("workspace-write", "read-only", "danger-full-access"):
            data["sandbox_mode"] = body.sandbox_mode
            settings.sandbox_mode = body.sandbox_mode
    if body.auto_approve_outside_read is not None:
        data["auto_approve_outside_read"] = body.auto_approve_outside_read
        settings.auto_approve_outside_read = body.auto_approve_outside_read
    if body.auto_overdraft_on_quota_exceeded is not None:
        data["auto_overdraft_on_quota_exceeded"] = body.auto_overdraft_on_quota_exceeded
        settings.auto_overdraft_on_quota_exceeded = body.auto_overdraft_on_quota_exceeded
    # v46: 额度自动重置开关（与自动透支并列的额度恢复手段）
    if body.auto_reset_on_quota_exceeded is not None:
        data["auto_reset_on_quota_exceeded"] = body.auto_reset_on_quota_exceeded
        settings.auto_reset_on_quota_exceeded = body.auto_reset_on_quota_exceeded
    if body.memory_enabled is not None:
        data["memory_enabled"] = body.memory_enabled
        settings.auto_memory_enabled = body.memory_enabled
    if body.agent_max_steps is not None:
        data["agent_max_steps"] = int(body.agent_max_steps)
        settings.agent_max_steps = int(body.agent_max_steps)
    # plan-75-332: 审批卡「解释」专用模型 / 思考深度（0/空 = 跟随会话）
    # 改回「跟随会话」时前端显式传 null，必须用 model_fields_set 区分「未提供」与
    # 「显式清空」：否则 `is not None` 会把清空当成未修改，设置不生效。
    if "approval_explain_model_id" in body.model_fields_set:
        _emid = int(body.approval_explain_model_id or 0)
        data["approval_explain_model_id"] = _emid
        settings.approval_explain_model_id = _emid or None
    if "approval_explain_reasoning_effort" in body.model_fields_set:
        _meff = str(body.approval_explain_reasoning_effort or "").strip()
        data["approval_explain_reasoning_effort"] = _meff
        settings.approval_explain_reasoning_effort = _meff
    # v45: 异常自动重试策略（次数 + 间隔序列），运行时立即生效
    if body.agent_retry_count is not None:
        _rc = max(0, int(body.agent_retry_count))
        data["agent_retry_count"] = _rc
        settings.agent_retry_count = _rc
    if body.agent_retry_intervals is not None:
        _ri = str(body.agent_retry_intervals).strip()
        data["agent_retry_intervals"] = _ri
        settings.agent_retry_intervals = _ri
    if body.browser_enabled is not None:
        data["browser_enabled"] = bool(body.browser_enabled)
        settings.browser_enabled = bool(body.browser_enabled)
    if body.browser_headless is not None:
        data["browser_headless"] = bool(body.browser_headless)
        settings.browser_headless = bool(body.browser_headless)
    # plan-334-1661: 电脑操控开关（落盘 + 运行时立即生效，工具门禁每次调用都读 settings）
    for _field in (
        "desktop_enabled", "desktop_plain_ops_enabled", "desktop_browser_ops_enabled",
        "desktop_require_foreground", "desktop_recipe_enabled",
        # plan-365-1757: 「点准」开关
        "desktop_mark_shot",
    ):
        _val = getattr(body, _field, None)
        if _val is not None:
            data[_field] = bool(_val)
            setattr(settings, _field, bool(_val))
    # plan-365-1769 F5: 三档字符串（兼容旧布尔输入）
    if body.desktop_verify_click is not None:
        _vm = normalize_verify_mode(body.desktop_verify_click)
        data["desktop_verify_click"] = _vm
        settings.desktop_verify_click = _vm
    if body.desktop_destructive_words is not None:
        _dw = str(body.desktop_destructive_words)
        data["desktop_destructive_words"] = _dw
        settings.desktop_destructive_words = _dw
    if body.desktop_screenshot_quality is not None:
        _q = max(10, min(100, int(body.desktop_screenshot_quality)))
        data["desktop_screenshot_quality"] = _q
        settings.desktop_screenshot_quality = _q
    if body.desktop_screenshot_max_dim is not None:
        _d = max(0, int(body.desktop_screenshot_max_dim))
        data["desktop_screenshot_max_dim"] = _d
        settings.desktop_screenshot_max_dim = _d
    # v36: 子代理并发治理（夹紧上限，运行时立即生效——agent_loop 每次派发前读取）
    if body.max_concurrent_subagents is not None:
        _mc = max(1, min(16, int(body.max_concurrent_subagents)))
        data["max_concurrent_subagents"] = _mc
        settings.max_concurrent_subagents = _mc
    if body.max_subagents_per_turn is not None:
        _mt = max(1, min(32, int(body.max_subagents_per_turn)))
        data["max_subagents_per_turn"] = _mt
        settings.max_subagents_per_turn = _mt
    # plan-156-619 S4d：子会话默认模型 / 思考深度（子代理设置面板仅保留这两项可调项）
    if body.subagent_model_id is not None:
        _sm = max(0, int(body.subagent_model_id))
        data["subagent_model_id"] = _sm
        settings.subagent_model_id = _sm
    if body.subagent_reasoning_effort is not None:
        _se = str(body.subagent_reasoning_effort or "")[:20]
        data["subagent_reasoning_effort"] = _se
        settings.subagent_reasoning_effort = _se
    if body.subagent_enabled is not None:
        data["subagent_enabled"] = bool(body.subagent_enabled)
        settings.subagent_enabled = bool(body.subagent_enabled)
    if body.operator_subsession_enabled is not None:
        data["operator_subsession_enabled"] = bool(body.operator_subsession_enabled)
        settings.operator_subsession_enabled = bool(body.operator_subsession_enabled)
    if body.operator_reasoning_effort is not None:
        _oe = str(body.operator_reasoning_effort or "lowest")[:20]
        data["operator_reasoning_effort"] = _oe
        settings.operator_reasoning_effort = _oe
    if body.tool_card_sections_expanded is not None:
        data["tool_card_sections_expanded"] = bool(body.tool_card_sections_expanded)
        settings.tool_card_sections_expanded = bool(body.tool_card_sections_expanded)
    # 工作流运行时：夹紧后写入配置；并发上限由服务每拍读取，保存即生效
    if body.workflow_enabled is not None:
        data["workflow_enabled"] = bool(body.workflow_enabled)
        settings.workflow_enabled = bool(body.workflow_enabled)
    for _flag in (
        "workflow_confirm_before_start",
        "workflow_auto_resume_on_network",
        "workflow_require_checks",
        "workflow_rework_confirm",
        "workflow_auto_merge",
    ):
        _flag_value = getattr(body, _flag)
        if _flag_value is not None:
            data[_flag] = bool(_flag_value)
            setattr(settings, _flag, bool(_flag_value))
    if body.workflow_composition_mode is not None:
        _mode = _composition_mode(body.workflow_composition_mode)
        data["workflow_composition_mode"] = _mode
        settings.workflow_composition_mode = _mode
    if body.workflow_conflict_policy is not None:
        _policy = _conflict_policy(body.workflow_conflict_policy)
        data["workflow_conflict_policy"] = _policy
        settings.workflow_conflict_policy = _policy
    if body.workflow_isolation_default is not None:
        _isolation = _isolation_default(body.workflow_isolation_default)
        data["workflow_isolation_default"] = _isolation
        settings.workflow_isolation_default = _isolation
    if body.workflow_hot_files is not None:
        _hot = str(body.workflow_hot_files)[:2000]
        data["workflow_hot_files"] = _hot
        settings.workflow_hot_files = _hot
    for _key, (_lo, _hi) in _WORKFLOW_INT_BOUNDS.items():
        _val = getattr(body, _key)
        if _val is not None:
            _clamped = max(_lo, min(_hi, int(_val)))
            data[_key] = _clamped
            setattr(settings, _key, _clamped)
    _save_config(data)
    # 运行时更新 context compaction 设置
    if body.auto_compact_enabled is not None:
        settings.context_compaction_enabled = body.auto_compact_enabled
    return _global_settings_out(data)


# ── 作用域规则(工作目录 / 群聊) ──

class ScopeRulesOut(BaseModel):
    scope: str
    key: str
    rules: str


class ScopeRulesIn(BaseModel):
    rules: str = ""


def _get_scope_rules_key(scope: str, key: str) -> str:
    """生成作用域规则的存储键。"""
    return f"rules_{scope}_{key}"


def _resolve_project_scope_key(project_path: str | None) -> str:
    """把项目路径解析为作用域规则的存储键（仓库绝对路径）。

    plan-234-1171 R7: 项目规则必须按"具体项目"存储，且写入键要与注入时的读取键
    **严格同源**——注入侧 user_rules_loader.load_workdir_rules(workspace) 用的是
    `Path(workspace).resolve()`（会话实际项目路径）。此前面板读写一律用服务端
    settings.workspace_root，与注入侧的项目路径不一致，导致用户写的规则永远读不到。
    未传 project_path 时保持原行为（settings.workspace_root）。
    """
    raw = project_path or settings.workspace_root
    try:
        return str(Path(raw).resolve())
    except (OSError, ValueError):
        return str(raw)


@router.get("/settings/rules/{scope}/{key:path}", response_model=ScopeRulesOut)
async def get_scope_rules(scope: str, key: str) -> ScopeRulesOut:
    """读取指定作用域的规则(scope: workdir / group, key: 路径或会话ID)。"""
    data = _load_config()
    storage_key = _get_scope_rules_key(scope, key)
    rules_data = data.get(storage_key, {})
    return ScopeRulesOut(
        scope=scope,
        key=key,
        rules=rules_data.get("rules", "") if isinstance(rules_data, dict) else str(rules_data),
    )


@router.put("/settings/rules/{scope}/{key:path}", response_model=ScopeRulesOut)
async def set_scope_rules(scope: str, key: str, body: ScopeRulesIn) -> ScopeRulesOut:
    """更新指定作用域的规则。"""
    data = _load_config()
    storage_key = _get_scope_rules_key(scope, key)
    data[storage_key] = {"rules": body.rules, "scope": scope, "key": key}
    _save_config(data)
    return ScopeRulesOut(scope=scope, key=key, rules=body.rules)


@router.get("/settings/rules/workdir", response_model=ScopeRulesOut)
async def get_workdir_rules(project_path: str = "") -> ScopeRulesOut:
    """读取当前工作目录的规则。

    plan-234-1171 R7: `project_path` 指定规则归属项目；未传时保持原行为
    （settings.workspace_root），不破坏既有数据。
    """
    ws_key = _resolve_project_scope_key(project_path)
    data = _load_config()
    storage_key = _get_scope_rules_key("workdir", ws_key)
    rules_data = data.get(storage_key, {})
    return ScopeRulesOut(
        scope="workdir",
        key=ws_key,
        rules=rules_data.get("rules", "") if isinstance(rules_data, dict) else str(rules_data),
    )


@router.put("/settings/rules/workdir", response_model=ScopeRulesOut)
async def set_workdir_rules(body: ScopeRulesIn, project_path: str = "") -> ScopeRulesOut:
    """更新当前工作目录的规则（plan-234-1171 R7: 支持按项目写入）。"""
    ws_key = _resolve_project_scope_key(project_path)
    data = _load_config()
    storage_key = _get_scope_rules_key("workdir", ws_key)
    data[storage_key] = {"rules": body.rules, "scope": "workdir", "key": ws_key}
    _save_config(data)
    return ScopeRulesOut(scope="workdir", key=ws_key, rules=body.rules)


# ── AI 规则来源（第8点：扫描常见 AI 软件规则文档并按来源启停）──

class AiRuleScanItem(BaseModel):
    source: str
    label: str
    path: str
    exists: bool
    kind: str  # file | dir


class AiRulesOut(BaseModel):
    sources: list[dict]  # [{source,label,enabled}]
    global_rules: str = ""
    workdir_rules: str = ""
    # plan-234-1171 R7: 回传项目规则实际归属的路径，供前端显式展示"配的是哪个项目"
    project_path: str = ""


class AiRulesIn(BaseModel):
    enabled_sources: list[str] | None = None
    global_rules: str | None = None
    workdir_rules: str | None = None
    # plan-234-1171 R7: 指定项目规则归属项目（仓库路径）；未传时回退 workspace_root
    project_path: str | None = None


@router.get("/settings/ai-rules/scan", response_model=list[dict])
async def scan_ai_rules(path: str = ""):
    """扫描项目下的 AI 规则文档，按来源识别。"""
    root = Path(path) if path else Path(settings.workspace_root)
    if not root.is_dir():
        return []
    # plan-114-450：与注入侧同一个扫描器（根 + 一级子目录 + 目录形态规则），
    # 界面看到的文件 = 实际会被加载的文件，每个文件都归属到可开关的来源。
    from app.orchestration.rules_loader import RULE_SOURCE_LABELS, scan_rules_docs, source_of
    out: list[dict] = []
    for rel in await scan_rules_docs(str(root)):
        source = source_of(rel)
        if source is None:
            continue
        out.append({
            "source": source,
            "label": RULE_SOURCE_LABELS.get(source, source),
            "path": rel,
            "exists": True,
            "kind": "file",
        })
    return out


@router.get("/settings/ai-rules", response_model=AiRulesOut)
async def get_ai_rules(project_path: str = "") -> AiRulesOut:
    """读取 AI 规则配置（来源启停 + 全局/项目规则）。

    plan-234-1171 R7: `project_path` 指定项目规则的归属项目。此前一律用服务端
    settings.workspace_root 作键，与注入侧按会话项目路径读取的键不一致，
    用户在面板里写的项目规则永远注入不进去。
    """
    data = _load_config()
    ws_key = _resolve_project_scope_key(project_path)
    workdir_data = data.get(_get_scope_rules_key("workdir", ws_key), {})
    workdir_rules = workdir_data.get("rules", "") if isinstance(workdir_data, dict) else str(workdir_data)
    # S11（plan-41-197）：用“键是否存在”判定，不再用 `or` 回落——
    # 用户把来源全部停用时保存的是空数组 []，`[] or 默认全集` 会复活成“全部启用”，
    # 导致重进页面看到“停用未保存成功”（读写两侧同源问题，一起修正）。
    return AiRulesOut(
        sources=_rule_sources_out(data),
        global_rules=data.get("global_rules", ""),
        workdir_rules=workdir_rules,
        project_path=ws_key,
    )


def _rule_sources_out(data: dict) -> list[dict]:
    """plan-114-450：来源列表取自注入侧 RULE_SOURCE_LABELS（此前路由自有一份 6 项的表，
    gemini / windsurf / github 等来源注入时生效却在界面上没有开关）。
    S11：用「键是否存在」判定，空数组是合法值（全部停用）。"""
    from app.orchestration.rules_loader import RULE_SOURCE_LABELS
    enabled = set(data["ai_rules_enabled"]) if "ai_rules_enabled" in data else set(RULE_SOURCE_LABELS.keys())
    return [{"source": s, "label": label, "enabled": s in enabled} for s, label in RULE_SOURCE_LABELS.items()]


@router.put("/settings/ai-rules", response_model=AiRulesOut)
async def set_ai_rules(body: AiRulesIn) -> AiRulesOut:
    """保存 AI 规则配置。

    plan-234-1171 R7: 写入键与 get_ai_rules / user_rules_loader 严格同源，
    保证"在这里写"与"注入时读"指向同一个项目。
    """
    data = _load_config()
    if body.enabled_sources is not None:
        data["ai_rules_enabled"] = list(dict.fromkeys(body.enabled_sources))
    if body.global_rules is not None:
        data["global_rules"] = body.global_rules
    ws_key = _resolve_project_scope_key(body.project_path)
    if body.workdir_rules is not None:
        data[_get_scope_rules_key("workdir", ws_key)] = {
            "rules": body.workdir_rules, "scope": "workdir", "key": ws_key,
        }
    _save_config(data)
    workdir_data = data.get(_get_scope_rules_key("workdir", ws_key), {})
    workdir_rules = workdir_data.get("rules", "") if isinstance(workdir_data, dict) else str(workdir_data)
    return AiRulesOut(
        sources=_rule_sources_out(data),
        global_rules=data.get("global_rules", ""),
        workdir_rules=workdir_rules,
        project_path=ws_key,
    )
