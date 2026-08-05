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


class SessionUpdate(BaseModel):
    title: str | None = None
    model_id: int | None = None
    pinned: bool | None = None
    status: str | None = None  # active / archived


class SessionOut(BaseModel):
    id: int
    project_id: int | None = None
    title: str | None = None
    model_id: int | None = None
    status: str = "active"
    pinned: bool = False
    fork_parent_id: int | None = None
    worktree_path: str | None = None
    has_running: bool = False
    has_interrupted_turn: bool = False


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
    id: int
    session_id: int
    turn_id: int | None = None
    parent_task_id: int | None = None
    title: str
    description: str | None = None
    acceptance_criteria: str | None = None
    agent_id: int | None = None
    status: str = "pending"
    priority: int = 0
    artifact_ids: list[int] | None = None
    note: str | None = None


class ArtifactOut(BaseModel):
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


class ExecPolicyRuleCreate(BaseModel):
    session_id: int | None = None
    command_pattern: str
    decision: str
    justification: str | None = None


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


class RollbackPreviewFile(BaseModel):
    """回滚预览单文件：展示回滚前/后内容供用户审核。"""
    path: str
    action: str  # "restore"（恢复）/ "delete"（删除新建文件）
    conflict: bool = False  # True=存在用户手动改动冲突，回滚将跳过该文件
    reason: str | None = None
    before: str | None = None  # 回滚前（当前）文件内容
    after: str | None = None   # 回滚后文件内容


class RollbackPreviewOut(BaseModel):
    ok: bool
    turn_id: int
    files: list[RollbackPreviewFile]


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
    base_url: str | None = None
    intelligence_level: int = 2
    context_window: int | None = None
    source_type: str = "byok"
    is_active: bool = True
    is_multimodal: bool = False
    api_format: str = "openai"
    has_api_key: bool = False
    reasoning_efforts: list[str] = []
