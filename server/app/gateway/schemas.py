"""API 请求/响应 Pydantic schema（v2：项目任务驱动）。"""
from typing import Any

from pydantic import BaseModel, Field


# ── 通用 ──
class IdResponse(BaseModel):
    id: int


class ApiError(BaseModel):
    code: str
    message: str
    detail: dict[str, Any] | None = None


# ── 项目 ──
class ProjectCreate(BaseModel):
    name: str | None = None  # 默认取路径末段
    path: str
    rules_docs: list[str] | None = None
    auto_scan_rules: bool = True


class ProjectUpdate(BaseModel):
    name: str | None = None
    rules_docs: list[str] | None = None
    auto_scan_rules: bool | None = None
    pinned: bool | None = None
    archived: bool | None = None


class ProjectOut(BaseModel):
    id: int
    name: str
    path: str
    rules_docs: list[str] | None = None
    auto_scan_rules: bool = True
    pinned: bool = False
    archived: bool = False


# ── 会话 ──
class SessionCreate(BaseModel):
    project_id: int
    title: str | None = None
    model_id: int | None = None


class CompactionIndexOut(BaseModel):
    """压缩块索引（v30.1：AI 按索引查看压缩前会话的定位信息）。"""
    index: int | None = None
    compaction_id: str | None = None
    summary_message_id: int | None = None
    shadowed_ids: list[int] = []
    shadowed_tokens: int = 0
    saved_tokens: int = 0
    trigger: str = "pressure"
    created_at: str | None = None
    summary_preview: str = ""


class SessionUpdate(BaseModel):
    title: str | None = None
    model_id: int | None = None
    pinned: bool | None = None
    status: str | None = None  # active / archived
    permission_mode: str | None = None  # v2.2: default / accept_edits / plan


class SessionOut(BaseModel):
    id: int
    project_id: int | None = None
    title: str | None = None
    model_id: int | None = None
    status: str = "active"
    pinned: bool = False
    permission_mode: str = "default"  # v2.2: default / accept_edits / plan
    fork_parent_id: int | None = None
    worktree_path: str | None = None
    has_running: bool = False
    has_interrupted_turn: bool = False
    last_activity_at: str | None = None  # 最近一条消息时间（侧栏相对时间展示）


# ── 轮次 ──
class TurnCreate(BaseModel):
    session_id: int
    content: str
    attachments: list[dict[str, Any]] | None = None
    scheduled_task_id: int | None = None  # 定时任务触发
    reasoning_effort: str | None = None  # v4: turn 级推理深度覆盖
    mode: str | None = None  # v6: 命令模式: readonly(只读审阅) / plan(先规划后执行)


class TurnOut(BaseModel):
    id: int
    session_id: int
    user_message_id: int | None = None
    status: str = "running"
    summary: str | None = None
    token_usage: int = 0
    started_at: str | None = None
    completed_at: str | None = None


# ── 代理 ──
class AgentOut(BaseModel):
    id: int
    kind: str = "main"
    name: str
    model_id: int | None = None
    session_id: int | None = None
    turn_id: int | None = None
    parent_agent_id: int | None = None
    status: str = "idle"


# ── 消息 ──
class MessageOut(BaseModel):
    id: int
    session_id: int
    turn_id: int | None = None
    thread_id: int | None = None
    sender_type: str
    sender_id: int | None = None
    msg_type: str
    content: dict[str, Any]
    token_usage: int = 0
    created_at: str | None = None


# ── 任务与产物 ──
class TaskOut(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    session_id: int
    turn_id: int | None = None
    parent_task_id: int | None = None
    kind: str = "request"
    depends_on: list[int] | None = None
    estimate: int | None = None
    is_hidden: bool = False
    title: str
    description: str | None = None
    acceptance_criteria: str | None = None
    agent_id: int | None = None
    status: str = "pending"
    priority: int = 0
    artifact_ids: list[int] | None = None
    note: str | None = None


class TaskConfirmStep(BaseModel):
    task_id: int | None = None
    title: str


class TaskConfirmBody(BaseModel):
    accepted: bool
    steps: list[TaskConfirmStep] | None = None


class ArtifactOut(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    task_id: int | None = None
    type: str | None = None
    title: str | None = None
    storage_ref: str | None = None
    summary: str | None = None
    files: list[str] | None = None


# ── 定时任务 ──
class ScheduledTaskCreate(BaseModel):
    session_id: int
    name: str
    cron: str
    prompt: str


class ScheduledTaskUpdate(BaseModel):
    name: str | None = None
    cron: str | None = None
    prompt: str | None = None
    enabled: bool | None = None


class ScheduledTaskOut(BaseModel):
    id: int
    session_id: int
    name: str
    cron: str
    prompt: str
    enabled: bool = True
    last_run_at: str | None = None
    next_run_at: str | None = None


# ── 配置 profile ──
class ConfigProfileOut(BaseModel):
    id: int
    name: str
    scope: str = "global"
    project_id: int | None = None
    data: dict[str, Any]
    is_active: bool = False


class ConfigProfileCreate(BaseModel):
    name: str
    scope: str = "global"
    project_id: int | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class ConfigProfileUpdate(BaseModel):
    data: dict[str, Any] | None = None
    is_active: bool | None = None


# ── 执行策略 ──
class ExecPolicyRuleOut(BaseModel):
    id: int
    session_id: int | None = None
    command_pattern: str
    decision: str
    justification: str | None = None
    # v2.2/v3.0: 工具级规则（非空 = 该规则作用于工具本身，command_pattern 存 "(tool)xxx"）
    tool_name: str | None = None


class ExecPolicyRuleCreate(BaseModel):
    session_id: int | None = None
    command_pattern: str
    decision: str
    justification: str | None = None
    tool_name: str | None = None


# ── 钩子 ──
class HookConfigOut(BaseModel):
    id: int
    event: str
    command: str
    matcher: str | None = None
    enabled: bool = True


class HookConfigCreate(BaseModel):
    event: str
    command: str
    matcher: str | None = None
    enabled: bool = True


# ── 记忆 ──
class MemoryEntryOut(BaseModel):
    id: int
    session_id: int
    turn_id: int | None = None
    text: str
    kind: str = "fact"
    usage_count: int = 0
    last_usage_at: str | None = None
    generated_at: str | None = None


# ── 回滚 ──
class TurnSnapshotOut(BaseModel):
    id: int
    session_id: int
    turn_id: int
    user_message_id: int | None = None
    git_head: str | None = None
    file_list: list[str] | None = None
    new_files: list[str] | None = None
    rolled_back: bool = False
    created_at: str | None = None


class RollbackResult(BaseModel):
    ok: bool
    turn_id: int
    rolled_back_msgs: int
    file_recovery: dict[str, Any]
    user_message: str | None = None  # restore_to_composer=True 时回填
    user_attachments: list[dict[str, Any]] | None = None  # restore_to_composer=True 时回填附件（图片等）


class RollbackPreviewFile(BaseModel):
    """回滚预览单文件：展示回滚前/后内容供用户审核。"""
    path: str
    action: str  # "restore"（恢复）/ "delete"（删除新建文件）
    conflict: bool = False  # True=存在用户手动改动冲突，回滚将跳过该文件
    reason: str | None = None
    before: str | None = None  # 回滚前（当前）文件内容
    after: str | None = None   # 回滚后文件内容


class RollbackAffected(BaseModel):
    """回滚连带影响统计（v12）：随回滚一并撤销的任务/消息数。"""
    tasks: int = 0
    messages: int = 0


class RollbackPreviewOut(BaseModel):
    ok: bool
    turn_id: int
    files: list[RollbackPreviewFile]
    affected: RollbackAffected = RollbackAffected()


# ── 变更审核 ──
class FileChangeOut(BaseModel):
    """变更审核清单单文件：仅元数据（不含文件全文），reviewed 为后端持久化状态。"""
    path: str
    action: str  # "modified"（修改）/ "added"（新增）/ "deleted"（删除）
    additions: int = 0  # 新增行数
    deletions: int = 0  # 删除行数
    reviewed: bool = False  # 后端持久化审核状态


class FileDiffOut(BaseModel):
    """单文件变更 diff（按需拉取，大文件截断）。"""
    path: str
    before: str | None = None  # 写盘前内容（新建文件为 None）
    after: str | None = None   # 当前磁盘内容（已删除文件为 None）
    truncated: bool = False    # 变更行数超限已截断
    reason: str | None = None  # 二进制/大文件说明（不展示文本 diff）


class ReviewBatchBody(BaseModel):
    paths: list[str]
    reviewed: bool = True


# ── 审计 ──
class AuditLogOut(BaseModel):
    id: int
    session_id: int | None = None
    turn_id: int | None = None
    action: str
    detail: dict[str, Any] | None = None
    created_at: str | None = None


# ── 模型（沿用现有字段）──
class ModelCreate(BaseModel):
    name: str
    provider: str | None = None
    provider_id: int | None = None
    base_url: str | None = None
    intelligence_level: int = 2
    context_window: int | None = None
    source_type: str = "byok"
    is_active: bool = True
    is_multimodal: bool = False
    api_format: str = "openai"
    api_key: str | None = None
    reasoning_efforts: list[str] | None = None


class ModelUpdate(BaseModel):
    name: str | None = None
    provider: str | None = None
    provider_id: int | None = None
    base_url: str | None = None
    intelligence_level: int | None = None
    context_window: int | None = None
    is_active: bool | None = None
    is_multimodal: bool | None = None
    api_format: str | None = None
    api_key: str | None = None
    reasoning_efforts: list[str] | None = None


class ModelOut(BaseModel):
    id: int
    name: str
    provider: str | None = None
    provider_id: int | None = None
    provider_name: str | None = None
    base_url: str | None = None
    intelligence_level: int = 2
    context_window: int | None = None
    source_type: str = "byok"
    is_active: bool = True
    is_multimodal: bool = False
    api_format: str = "openai"
    has_api_key: bool = False
    reasoning_efforts: list[str] = []
    # ── trae 供应商扩展（源自 trae_meta）──
    trae_max_context: int | None = None        # max 档上下文（如 1000000 = 1M）
    trae_consumption_rate: float | None = None  # 积分消耗倍率（max 档更快）
    trae_available: bool = False               # TRAE 客户端实际可用
    trae_thinking: bool = False                # 支持思考档位


# ── 供应商（v16）──
class ProviderCreate(BaseModel):
    name: str
    base_url: str | None = None
    api_key: str | None = None
    api_format: str = "openai"
    is_active: bool = True


class ProviderUpdate(BaseModel):
    name: str | None = None
    base_url: str | None = None
    api_key: str | None = None  # 传空字符串 = 清除
    api_format: str | None = None
    is_active: bool | None = None


class ProviderOut(BaseModel):
    id: int
    name: str
    base_url: str | None = None
    api_format: str = "openai"
    is_active: bool = True
    has_api_key: bool = False
    model_count: int = 0
    # v23: ta3 供应商登录态
    auth_status: str | None = None
    account_label: str | None = None
    created_at: str | None = None


# ── ta3 登录/同步（v23）──
class Ta3LoginStartOut(BaseModel):
    status: str
    authorize_url: str | None = None
    state: str | None = None
    port: int | None = None
    expires_in: int | None = None


class Ta3LoginStatusOut(BaseModel):
    status: str  # pending | logged_in | failed
    account: dict | None = None
    error: str | None = None


class Ta3SyncOut(BaseModel):
    synced: int
    models: list[dict]


# ── workbuddy（腾讯 CodeBuddy/WorkBuddy）登录/同步（v24）──
class WorkBuddyLoginStartOut(BaseModel):
    status: str  # pending | logged_in
    auth_url: str | None = None
    state: str | None = None
    expires_in: int | None = None


class WorkBuddyLoginStatusOut(BaseModel):
    status: str  # pending | logged_in | failed
    account: dict | None = None
    error: str | None = None


class WorkBuddySyncOut(BaseModel):
    synced: int
    models: list[dict]


# ── TRAE SOLO CN 登录/同步（v25）──
class TraeLoginStartOut(BaseModel):
    status: str  # pending | logged_in | failed
    authorize_url: str | None = None
    state: str | None = None
    port: int | None = None
    expires_in: int | None = None


class TraeLoginStatusOut(BaseModel):
    status: str  # pending | logged_in | failed
    account: dict | None = None
    error: str | None = None


class TraeSyncOut(BaseModel):
    synced: int
    models: list[dict]


class ScannedModel(BaseModel):
    """供应商扫描到的模型条目。"""
    id: str
    context_window: int | None = None
    owned_by: str | None = None


class ProviderScanOut(BaseModel):
    models: list[ScannedModel]


class ProviderModelItem(BaseModel):
    """批量保存时单个模型的配置。"""
    name: str
    is_active: bool = True
    context_window: int | None = None
    is_multimodal: bool = False
    reasoning_efforts: list[str] | None = None


class ProviderModelsBulkIn(BaseModel):
    models: list[ProviderModelItem]


# ─────────────────────────────────────────────────────────────
# WS 事件协议镜像（v2.1，对齐 zcode 方案 3.2：packages/shared/src/events.ts 的 pydantic 侧）
# 规则：新增事件必须同时在 packages/shared/src/events.ts 与本表登记；
# broadcast 前用 payload 模型做宽松校验（失败仅告警不阻断广播，避免影响主流程）。
# ─────────────────────────────────────────────────────────────

class WsEventPayload(BaseModel):
    """宽松基类：事件 payload 允许任意附加字段。"""

    model_config = {"extra": "allow"}


class EvTurnStarted(WsEventPayload):
    turn_id: int | None = None


class EvTurnCompleted(WsEventPayload):
    turn_id: int | None = None
    summary: str | None = None
    artifact_ids: list[int] | None = None


class EvTokenDelta(WsEventPayload):
    agent_id: int | None = None
    turn_id: int | None = None
    delta: str = ""


class EvToolCall(WsEventPayload):
    turn_id: int | None = None
    agent_id: int | None = None
    tool: str = ""
    args_preview: str | None = None
    args_partial: str | None = None


class EvToolResult(WsEventPayload):
    turn_id: int | None = None
    tool: str = ""
    ok: bool = True
    duration_ms: int | None = None
    output_preview: str | None = None
    change_stat: dict | None = None


class EvTodoUpdated(WsEventPayload):
    turn_id: int | None = None
    todos: list | None = None
    persisted: bool = False


class EvTaskUpdated(WsEventPayload):
    task_id: int | None = None
    status: str | None = None
    note: str | None = None


class EvUsageUpdate(WsEventPayload):
    agent_id: int | None = None
    turn_id: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    context_window: int | None = None
    breakdown: dict | None = None


class EvCompactEvent(WsEventPayload):
    """压缩事件载荷（compact.started / compact.summary / compact.completed）。

    started: used_tokens/context_window/ratio 为触发时的占用信息；
    summary/completed: compaction_id/shadowed_range/shadowed_seqs/saved_tokens/
    summary_message_id 为落库后的压缩结果（阴影定价），供前端渲染压缩卡片。
    """
    agent_id: int | None = None
    turn_id: int | None = None
    used_tokens: int | None = None
    context_window: int | None = None
    ratio: float | None = None
    # v30: 压缩结果字段
    compaction_id: str | None = None
    shadowed_range: list[int] | None = None
    shadowed_seqs: list[int] | None = None
    shadowed_tokens: int | None = None
    saved_tokens: int | None = None
    summary_message_id: int | None = None
    summary: str | None = None
    trigger: str | None = None


class EvApprovalRequest(WsEventPayload):
    approval_id: str = ""
    detail: dict | None = None


class EvAgentEvent(WsEventPayload):
    agent_id: int | None = None
    kind: str | None = None
    status: str | None = None
    summary: str | None = None
    artifact_ids: list[int] | None = None


class EvError(WsEventPayload):
    code: str | None = None
    message: str | None = None


# 事件名 → payload 模型（broadcast 校验用；未登记的兜底 WsEventPayload）
WS_EVENT_PAYLOAD_MODELS: dict[str, type[WsEventPayload]] = {
    "message.created": WsEventPayload,
    "turn.started": EvTurnStarted,
    "turn.updated": EvTurnStarted,
    "turn.completed": EvTurnCompleted,
    "turn.interrupted": EvTurnStarted,
    "turn.rolled_back": WsEventPayload,
    "agent.started": EvAgentEvent,
    "agent.updated": EvAgentEvent,
    "agent.completed": EvAgentEvent,
    "thinking.delta": EvTokenDelta,
    "thinking.done": WsEventPayload,
    "token.delta": EvTokenDelta,
    "token.done": WsEventPayload,
    # v35: turn 级瞬态状态（重试/恢复提示），前端流式状态行展示，不落库
    "turn.status": WsEventPayload,
    "tool.call": EvToolCall,
    "tool.result": EvToolResult,
    "file.change": WsEventPayload,
    "todo.updated": EvTodoUpdated,
    "task.proposed": WsEventPayload,
    "task.planned": WsEventPayload,
    "task.updated": EvTaskUpdated,
    "usage.update": EvUsageUpdate,
    "compact.started": EvCompactEvent,
    "compact.summary": EvCompactEvent,
    "compact.completed": EvCompactEvent,
    "approval.request": EvApprovalRequest,
    "approval.response": WsEventPayload,
    "api.retry": WsEventPayload,
    "config.changed": WsEventPayload,
    "scheduled.triggered": WsEventPayload,
    "session.updated": WsEventPayload,
    "session.completed": WsEventPayload,
    "error": EvError,
    "ack": WsEventPayload,
    "sync.response": WsEventPayload,
    # 客户端事件转发（terminal/browser 面板协作预留通道）
    "terminal.input": WsEventPayload,
    "browser.command": WsEventPayload,
    "cancel": WsEventPayload,
}
