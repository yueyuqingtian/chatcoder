/**
 * chatcoder v2 前后端共享协议与类型定义（项目任务驱动架构）。
 * 与服务端 app/core/enums.py 保持一致，修改时需同步。
 */

// v2.1: WS 事件协议契约（seq / 断线补偿 / 穷举事件名）
export * from "./events";

// ── 枚举 ──

export enum MsgType {
  Text = "text",
  Thinking = "thinking",
  ToolCall = "tool_call",
  ToolResult = "tool_result",
  ToolGroup = "tool_group",
  Plan = "plan",
  Summary = "summary",
  Artifact = "artifact",
  Error = "error",
  System = "system",
}

export enum SenderType {
  User = "user",
  Agent = "agent",
  System = "system",
}

export enum TurnStatus {
  Running = "running",
  Completed = "completed",
  Failed = "failed",
  Cancelled = "cancelled",
  Interrupted = "interrupted",
  RolledBack = "rolled_back",
}

export enum TaskStatus {
  Pending = "pending",
  Running = "running",
  Done = "done",
  Failed = "failed",
  Cancelled = "cancelled",
}

export enum ModelSource {
  SystemDefault = "system_default",
  Byok = "byok",
}

export enum AgentKind {
  Main = "main",
  Sub = "sub",
}

export enum ApprovalPolicy {
  OnRequest = "on-request",
  Auto = "auto",
  Never = "never",
  Reject = "reject",
}

export enum SandboxMode {
  ReadOnly = "read-only",
  WorkspaceWrite = "workspace-write",
  DangerFullAccess = "danger-full-access",
}

export enum ExecPolicyDecision {
  Allow = "allow",
  Deny = "deny",
  Ask = "ask",
}

export enum HookEvent {
  PreToolUse = "pre_tool_use",
  PostToolUse = "post_tool_use",
  UserPromptSubmit = "user_prompt_submit",
  PermissionRequest = "permission_request",
  SessionStart = "session_start",
  SessionEnd = "session_end",
  TurnEnd = "turn_end",
  Compact = "compact",
}

export enum MemoryKind {
  Fact = "fact",
  Convention = "convention",
  Pitfall = "pitfall",
  Decision = "decision",
}

// ── 实体类型 ──

export interface ProjectOut {
  id: number;
  name: string;
  path: string;
  rules_docs: string[] | null;
  auto_scan_rules: boolean;
  pinned: boolean;
  archived: boolean;
}

export interface SessionOut {
  id: number;
  project_id: number | null;
  title: string | null;
  model_id: number | null;
  status: string; // active / archived
  pinned: boolean;
  fork_parent_id: number | null;
  worktree_path: string | null;
  has_running?: boolean;
  has_interrupted_turn?: boolean;
  last_activity_at?: string | null;
}

export interface TurnOut {
  id: number;
  session_id: number;
  user_message_id: number | null;
  status: string;
  summary: string | null;
  token_usage: number;
  started_at: string | null;
  completed_at: string | null;
}

export interface AgentOut {
  id: number;
  kind: string; // main / sub
  name: string;
  model_id: number | null;
  session_id: number | null;
  turn_id: number | null;
  parent_agent_id: number | null;
  status: string;
}

export interface MessageOut {
  id: number;
  session_id: number;
  turn_id: number | null;
  thread_id: number | null;
  sender_type: string;
  sender_id: number | null;
  msg_type: string;
  content: Record<string, unknown>;
  token_usage?: number;
  created_at: string | null;
}

export interface TaskOut {
  id: number;
  session_id: number;
  turn_id: number | null;
  parent_task_id: number | null;
  kind?: string;
  depends_on?: number[] | null;
  estimate?: number | null;
  is_hidden?: boolean;
  title: string;
  description: string | null;
  acceptance_criteria: string | null;
  agent_id: number | null;
  status: string;
  priority: number;
  artifact_ids: number[] | null;
  note?: string;
}

export interface ArtifactOut {
  id: number;
  task_id: number | null;
  type: string | null;
  title: string | null;
  storage_ref: string | null;
  summary: string | null;
  files: string[] | null;
}

export interface ScheduledTaskOut {
  id: number;
  session_id: number;
  name: string;
  cron: string;
  prompt: string;
  enabled: boolean;
  last_run_at: string | null;
  next_run_at: string | null;
}

export interface ConfigProfileOut {
  id: number;
  name: string;
  scope: string;
  project_id: number | null;
  data: Record<string, unknown>;
  is_active: boolean;
}

export interface ExecPolicyRuleOut {
  id: number;
  session_id: number | null;
  command_pattern: string;
  decision: string;
  justification: string | null;
}

export interface HookConfigOut {
  id: number;
  event: string;
  command: string;
  matcher: string | null;
  enabled: boolean;
}

export interface MemoryEntryOut {
  id: number;
  session_id: number;
  turn_id: number | null;
  text: string;
  kind: string;
  usage_count: number;
  last_usage_at: string | null;
  generated_at: string | null;
}

export interface ModelOut {
  id: number;
  name: string;
  provider: string | null;
  provider_id: number | null;
  provider_name: string | null;
  base_url: string | null;
  api_format: string;
  context_window: number | null;
  is_multimodal: boolean;
  is_active: boolean;
  source_type: string;
  has_api_key: boolean;
  reasoning_efforts: string[];
}

export interface ProviderOut {
  id: number;
  name: string;
  base_url: string | null;
  api_format: string;
  is_active: boolean;
  has_api_key: boolean;
  model_count: number;
  created_at: string | null;
}

export interface ScannedModel {
  id: string;
  context_window: number | null;
  owned_by: string | null;
}

export interface TurnSnapshotOut {
  id: number;
  session_id: number;
  turn_id: number;
  user_message_id: number | null;
  git_head: string | null;
  file_list: string[] | null;
  new_files: string[] | null;
  rolled_back: boolean;
  created_at: string | null;
}

export interface RollbackResult {
  ok: boolean;
  turn_id: number;
  rolled_back_msgs: number;
  file_recovery: Record<string, unknown>;
  user_message: string | null;
}

/** 回滚预览：单个文件回滚前后的内容对比（供用户审核确认）。 */
export interface RollbackPreviewFile {
  path: string;
  /** "restore"（恢复）/ "delete"（删除新建文件） */
  action: string;
  /** true=存在用户手动改动冲突，回滚将跳过该文件 */
  conflict: boolean;
  reason: string | null;
  /** 回滚前（当前）文件内容 */
  before: string | null;
  /** 回滚后文件内容 */
  after: string | null;
}

export interface RollbackAffected {
  /** 该 turn 及其之后将被取消的任务数 */
  tasks: number;
  /** 该 turn 及其之后将被软删的消息数 */
  messages: number;
}

export interface RollbackPreviewOut {
  ok: boolean;
  turn_id: number;
  files: RollbackPreviewFile[];
  affected: RollbackAffected;
}

/** 变更审核：单文件变更元数据（不含文件全文）。 */
export interface FileChangeOut {
  path: string;
  /** "modified"（修改）/ "added"（新增）/ "deleted"（删除） */
  action: string;
  /** 新增行数 */
  additions: number;
  /** 删除行数 */
  deletions: number;
  /** 后端持久化审核状态 */
  reviewed: boolean;
}

/** 变更审核：单文件 diff（按需拉取，大文件截断）。 */
export interface FileDiffOut {
  path: string;
  /** 写盘前内容（新建文件为 null） */
  before: string | null;
  /** 当前磁盘内容（已删除文件为 null） */
  after: string | null;
  /** 变更行数超限已截断 */
  truncated: boolean;
}

// ── WebSocket 事件 ──

/** 服务端 → 客户端 */
export type ServerWsEvent =
  | { event: "message.created"; payload: { msg: MessageOut } }
  | { event: "turn.started"; payload: { turn_id: number } }
  | { event: "turn.updated"; payload: { turn_id: number; status: string } }
  | { event: "turn.completed"; payload: { turn_id: number; summary: string | null; artifact_ids: number[] } }
  | { event: "turn.interrupted"; payload: { turn_id: number; last_message_id: number | null } }
  | { event: "turn.rolled_back"; payload: { turn_id: number; rolled_back_msgs: number; file_recovery: Record<string, unknown> } }
  | { event: "agent.started"; payload: { agent_id: number; kind: string; name: string; turn_id: number | null } }
  | { event: "agent.updated"; payload: { agent_id: number; status: string; tool?: string; step?: number } }
  | { event: "agent.completed"; payload: { agent_id: number; summary: string | null; artifact_ids: number[] } }
  | { event: "thinking.delta"; payload: { agent_id: number; turn_id: number | null; delta: string } }
  | { event: "thinking.done"; payload: { agent_id: number; turn_id: number | null; full_text: string } }
  | { event: "token.delta"; payload: { agent_id: number; turn_id: number | null; delta: string } }
  | { event: "token.done"; payload: { agent_id: number; turn_id: number | null; full_text: string } }
  | { event: "tool.call"; payload: { turn_id: number; agent_id: number; tool: string; args_preview: string } }
  | { event: "tool.result"; payload: { turn_id: number; tool: string; ok: boolean; duration_ms: number; output_preview: string } }
  | { event: "task.updated"; payload: { task_id: number; status: string; note?: string } }
  | { event: "usage.update"; payload: Record<string, unknown> }
  | { event: "approval.request"; payload: { approval_id: string; detail: Record<string, unknown> } }
  | { event: "approval.response"; payload: { approval_id: string; approved: boolean } }
  | { event: "session.completed"; payload: { session_id: number } }
  | { event: "config.changed"; payload: { profile_id: number; changed_keys: string[] } }
  | { event: "scheduled.triggered"; payload: { task_id: number; turn_id: number } }
  | { event: "error"; payload: { code: string; message: string } };

/** 客户端 → 服务端 */
export type ClientWsEvent =
  | { event: "approval.response"; payload: { approval_id: string; approved: boolean } }
  | { event: "terminal.input"; payload: { id: string; data: string } }
  | { event: "browser.command"; payload: { id: string; cmd: string; payload: Record<string, unknown> } }
  | { event: "cancel"; payload: { turn_id: number } };
