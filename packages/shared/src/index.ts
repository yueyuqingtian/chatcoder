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
  /** plan-282-1441（#5 工作树）：工作树作为独立工作区展示所需的标识 */
  is_worktree?: boolean;
  parent_project_id?: number | null;
  worktree_branch?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

/** plan-282-1441（#5）：工作树（含 git 状态摘要）——GET /projects/{id}/worktrees */
export interface WorktreeOut {
  id: number;
  name: string;
  path: string;
  branch: string | null;
  parent_project_id: number | null;
  parent_path: string | null;
  /** 有未提交变更 */
  dirty: boolean;
  /** 相对主分支的领先/落后提交数 */
  ahead: number;
  behind: number;
}

/** plan-282-1441（#5）：可用于创建工作树的仓库候选（项目根 + 子仓库）。
 *  典型用途：根目录是空仓库、真实代码在 clinic / clinicFrontEnd 两个子仓库。 */
export interface RepoCandidate {
  path: string;
  name: string;
  /** 是否为项目根仓库 */
  is_root: boolean;
  /** 是否已有提交（空仓库不能作为工作树起点） */
  has_commits: boolean;
  branch: string;
  dirty: boolean;
}

/** 合并差异文件 */
export interface WorktreeMergeFile {
  path: string;
  status: "added" | "modified" | "deleted" | "renamed" | "copied";
  conflict: boolean;
  /** 自动三方合并后的内容（Git 真冲突时为 git diff3 标记内容） */
  merged?: string | null;
  /** 是否可自动合并（无需人工/AI 介入） */
  has_auto_merge?: boolean;
  /** plan-308-1542：相对 base 的改动侧，供 UI 精确提示"仅一侧改动，已自动采用" */
  change_side?: "ours" | "theirs" | "both" | "none";
  /** 判定/降级原因（如"git 无法处理，已采用来源侧"） */
  reason?: string;
  /** 是否为二进制文件（git 无法自动合并，需人工选侧） */
  binary?: boolean;
  /** 是否必须人工处理（二进制冲突等） */
  needs_manual?: boolean;
}

/** plan-308-1542 需求3-A：AI 自动合并的汇总报告（merge.progress 的 done 载荷） */
export interface MergeReportOut {
  total: number;
  /** 由 git 干净合入（含单侧改动） */
  git: number;
  /** AI 介入并解决 */
  ai: number;
  /** 待人工处理 */
  conflicted: number;
  failed: number;
  skipped: number;
  elapsed_ms: number;
  engine?: string;
  files: Array<{
    path: string;
    result: "git" | "ai" | "manual" | "failed" | "skipped";
    reason?: string;
    ms?: number;
  }>;
}

/** 合并方向：to_main=工作树→主工作区；from_main=主工作区→工作树 */
export type WorktreeMergeDirection = "to_main" | "from_main";

export interface WorktreeMergePreview {
  ok: boolean;
  direction?: WorktreeMergeDirection;
  base_branch: string;
  branch: string;
  files: WorktreeMergeFile[];
  has_conflict: boolean;
  /** 来源侧是否存在未提交改动 */
  source_dirty?: boolean;
  /** 目标侧是否存在未提交改动 */
  target_dirty?: boolean;
  /** plan-308-1542：实际使用的合并引擎（git=git 原生；fallback=旧版 git 降级） */
  engine?: "git" | "fallback" | string;
}

/** plan-282-1441（#7）：数据库连接（密码不回传，只有 has_password） */
export interface DbConnectionOut {
  id: number;
  project_id: number;
  name: string;
  kind: "mysql" | "postgresql" | "sqlserver" | string;
  host: string;
  port: number;
  database: string | null;
  username: string | null;
  has_password: boolean;
  params: Record<string, unknown>;
  is_active: boolean;
}

/** plan-282-1441（#7）：数据库权限策略（服务端强制门控的真值源） */
export interface DbPolicyOut {
  project_id: number;
  allow_read: boolean;
  allow_write: boolean;
  allow_ddl: boolean;
  require_approval: boolean;
  row_limit: number;
  timeout_s: number;
}

/** plan-282-1441（#8）：调试状态（供调试面板展示"停在哪一行"） */
export interface DebugStatusOut {
  connected: boolean;
  target: "web" | "java" | string;
  breakpoints: number;
  paused: boolean;
  file?: string | null;
  line?: number | null;
  function?: string | null;
  hitCount: number;
  stack: Array<{ function?: string; url?: string; line?: number | null; index?: number | null }>;
  variables: Array<{ name?: string; value?: unknown; type?: string }>;
  reason?: string | null;
}

/** plan-282-1441（#8）：「开发调试」面板配置（落库，修“改了不保存”）。
 *  单行全局配置：Web 调试端口 + JDWP 目标主机/端口。 */
export interface DebugSettingsOut {
  web_port: number;
  jdwp_host: string;
  jdwp_port: number;
  /** false = 从未保存过，返回的是默认值 */
  saved?: boolean;
  updated_at?: string | null;
}

/** plan-282-1441（Arthas 方案）：Java 现场诊断会话状态（可与 IDEA 调试并存）。
 *  来自 /api/debug/arthas/status 与 arthas.event 广播。 */
export interface ArthasStatusOut {
  ok: boolean;
  attached: boolean;
  pid?: number | null;
  http_port?: number | null;
  version?: string | null;
  java?: string;
  jar?: string;
  main_class?: string;
  attached_at?: number;
  idle_seconds?: number;
  active_jobs?: number;
  jobs?: Array<{ job_id: string | number; command?: string; created_at?: number }>;
}

/** Arthas 观测命中条目（watch / trace / tt）：面板列表展示。
 *  value 是 Arthas 自己渲染好的观测表达式结果（多行文本），面板直接显示。 */
export interface ArthasEntryOut {
  type?: string;
  ts?: number | string | null;
  cost?: number | null;
  class?: string | null;
  method?: string | null;
  location?: string | null;
  access_point?: string | null;
  value?: string | null;
  params?: unknown;
  return_obj?: unknown;
  throwable?: unknown;
  children?: unknown[];
}

/** Arthas 进程条目（java_list_processes 的返回，面板可点选 attach）。 */
export interface ArthasProcessOut {
  pid: number;
  main_class: string;
  jvm_args?: string;
  debugging?: boolean;
  jdwp_address?: string;
  kind?: string;
}

/** Arthas 配置与探测结果（面板展示“为什么还不能用”与本地 jar 路径）。 */
export interface ArthasConfigOut {
  ok: boolean;
  java_home?: string;
  java_home_resolved?: string;
  java_home_source?: string;
  boot_jar?: string;
  boot_jar_resolved?: string;
  boot_jar_source?: string;
  cache_dir?: string;
  repo_mirror?: string;
  idle_timeout_sec?: number;
  http_port?: string;
  disabled_commands?: string;
}

/** plan-282-1441（#6）：插件市场条目（内置目录 + 已安装标注） */export interface PluginMarketItem {
  name: string;
  displayName: string;
  description: string;
  descriptionZh: string;
  category: string;
  tags: string[];
  author: string;
  version: string;
  /** 安装来源：本地目录 / git 地址（内置目录条目可能有值，供"从该来源安装"） */
  source?: string;
  featured?: boolean;
  installed?: boolean;
  enabled?: boolean;
  /** 已安装条目附带的信息 */
  keywords?: string[];
  marketplaceName?: string;
  logo?: string;
  skillsDir?: string;
  path?: string;
  installedAt?: string | null;
  /** plan-282-1441：真实扫描到的插件——贡献技能数与技能名（市场卡片展示用） */
  skillCount?: number;
  skills?: string[];
  /** 是否提供 MCP 连接器（插件的 .mcp.json） */
  hasMcp?: boolean;
}

export interface SessionOut {
  id: number;
  project_id: number | null;
  title: string | null;
  model_id: number | null;
  status: string; // active / archived
  pinned: boolean;
  /** v7: 置顶时间——前端"后置顶在上"排序依据 */
  pinned_at?: string | null;
  // plan-230-1144 M2: 模式外置为可配置数据，自定义模式名是任意字符串。
  // plan-75-332: 语义改为**执行模式**——内置 3 值（agent/plan/readonly）；
  // 旧值 default / accept_edits 由后端归一化为 agent。
  permission_mode?: string;
  // plan-75-332: 权限模式（与执行模式正交）——ask 询问审批 / auto 自动审批 / full 完全访问
  approval_mode?: string;
  fork_parent_id: number | null;
  worktree_path: string | null;
  has_running?: boolean;
  has_interrupted_turn?: boolean;
  last_activity_at?: string | null;
  /** 运行中 turn 的开始时间——侧栏「执行中任务」稳定排序键（运行期间不变，避免上下跳动） */
  running_started_at?: string | null;
  /** plan-671: 目标模式状态（前端恢复目标胶囊） */
  goal_text?: string | null;
  goal_status?: "none" | "active" | "completed" | "cancelled";
  goal_turns_used?: number;
}

/** plan-671: 目标模式（对齐 zcode goal-continuation）。 */
export interface GoalOut {
  text: string | null;
  status: "none" | "active" | "completed" | "cancelled";
  turns_used: number;
  max_turns: number;
  created_at: string | null;
}

/** v30.1: 压缩块索引（AI/前端定位压缩前会话的索引条目）。 */
export interface CompactionIndexOut {
  index: number | null;
  compaction_id: string | null;
  summary_message_id: number | null;
  shadowed_ids: number[];
  shadowed_tokens: number;
  saved_tokens: number;
  trigger: string;
  created_at: string | null;
  summary_preview: string;
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
  /** plan-644: 本 turn 产出的方案文档路径（相对工作区；null = 与计划流程无关） */
  plan_doc_path?: string | null;
  /** plan-644: 计划生命周期（proposed/confirmed/done/cancelled/superseded） */
  plan_status?: string | null;
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

/** 错过策略：skip=错过即跳过只等下一次；run_once=重启后补跑一次 */
export type ScheduledMissedPolicy = "skip" | "run_once";
/** 最近一次执行结果（由 scheduler_loop 回写） */
export type ScheduledRunStatus = "triggered" | "ok" | "failed" | "skipped" | "cancelled" | "orphaned";

export interface ScheduledTaskOut {
  id: number;
  session_id: number;
  name: string;
  cron: string;
  prompt: string;
  enabled: boolean;
  last_run_at: string | null;
  next_run_at: string | null;
  // plan-230-1144 M1.1：调度器落地后的执行状态
  last_status: ScheduledRunStatus | null;
  last_error: string | null;
  missed_policy: ScheduledMissedPolicy;
}

/** cron 实时校验结果（GET /scheduled-tasks/meta/validate） */
export interface CronValidateOut {
  valid: boolean;
  error?: string;
  next_run_at?: string | null;
  never_fires?: boolean;
  fields?: {
    minutes: number[];
    hours: number[];
    days_of_month: number[];
    months: number[];
    days_of_week: number[];
  };
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
  /** 工具级规则：非空 = 作用于工具本身（command_pattern 存 "(tool)xxx"） */
  tool_name: string | null;
}

export interface HookConfigOut {
  id: number;
  event: string;
  command: string;
  matcher: string | null;
  enabled: boolean;
  /** S12（plan-41-197）：动作类型——command=执行脚本 / prompt=向 AI 注入提示词 */
  hook_type?: "command" | "prompt";
  /** 提示词型钩子的注入文本 */
  prompt?: string | null;
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
  // plan-230-1144 M4.1: 三层化字段
  scope?: "session" | "project" | "global" | string;
  project_id?: number | null;
  /** 候选区（低置信，未注入 prompt，可被检索） */
  candidate?: boolean;
  expires_at?: string | null;
  superseded_by?: number | null;
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
  /** plan-248-1258 M2.4: 所属供应商启用状态（false 时输入框选择器过滤该模型） */
  provider_active?: boolean;
  /** plan-89-386: 所属供应商排序位（全局模型选择器按设置页供应商顺序展示） */
  provider_sort_order?: number;
  // trae 供应商扩展（源自 trae_meta）
  trae_max_context?: number | null;        // max 档上下文（如 1000000 = 1M）
  trae_consumption_rate?: number | null;   // 积分消耗倍率（max 档更快）
  trae_available?: boolean;                // TRAE 客户端实际可用
  trae_thinking?: boolean;                 // 支持思考档位
}

export interface ProviderOut {
  id: number;
  name: string;
  base_url: string | null;
  api_format: string;
  is_active: boolean;
  has_api_key: boolean;
  model_count: number;
  // v23: ta3 供应商登录态
  auth_status?: string | null;
  account_label?: string | null;
  created_at: string | null;
  /** plan-248-1258 M2.3: 供应商级代理（inherit/global/custom/direct） */
  proxy_mode?: string;
  proxy_url?: string | null;
  /** plan-248-1258 M2.2: 凭据统计（多 Key/多账号） */
  credential_count?: number;
  active_credential_count?: number;
  /** plan-271-1364: 凭据取用策略（sticky=粘性优先 | round_robin=按优先级轮转） */
  credential_strategy?: string;
  /** plan-41-225: 供应商排序位（模型页左列拖拽排序依据） */
  sort_order?: number;
}

/** plan-248-1258 M2.2: 供应商凭据（多 API Key / 多登录账号） */
export interface ProviderCredentialOut {
  id: number;
  provider_id: number;
  label: string | null;
  has_api_key: boolean;
  api_key_preview: string | null;
  token_ref: string | null;
  priority: number;
  is_active: boolean;
  /** ok | cooldown | error | disabled */
  status: string;
  last_error: string | null;
  cooldown_until: string | null;
  last_ok_at: string | null;
  /** workbuddy 等账号积分余额缓存 */
  credits: number | null;
  extra: Record<string, unknown> | null;
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
  /** restore_to_composer=True 时回填的附件（图片等），供输入框恢复展示与重发 */
  user_attachments?: Array<Record<string, unknown>> | null;
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

/** 行级 diff（服务端用 SequenceMatcher 预计算，保证与 +N -M 徽标同源一致）。
 * type: add=新增 del=删除 ctx=上下文；old_no/new_no 为变更前后行号（1-based，ctx 两值相同）。 */
export interface DiffLine {
  type: "add" | "del" | "ctx";
  text: string;
  old_no?: number | null;
  new_no?: number | null;
}

/** 变更审核：单文件 diff（按需拉取，大文件截断）。 */
export interface FileDiffOut {
  path: string;
  /** 写盘前内容（新建文件为 null） */
  before: string | null;
  /** 写盘后内容（本次编辑口径）/ 当前磁盘内容（整轮累积口径） */
  after: string | null;
  /** 变更行数超限已截断 */
  truncated: boolean;
  /** 二进制/大文件说明（不展示文本 diff） */
  reason?: string | null;
  /** 行级 diff（服务端预计算，优先于 before/after 本地 LCS） */
  lines?: DiffLine[] | null;
  /** 新增行数（与 lines 同源，卡片 +N -M 与展开内容据此对齐） */
  additions?: number;
  /** 删除行数 */
  deletions?: number;
  /** true=按 call_key 命中的本次编辑；false=整轮累积（老数据回退） */
  single_edit?: boolean;
}

// ── WebSocket 事件 ──

/** 服务端 → 客户端 */
export type ServerWsEvent =
  | { event: "message.created"; payload: { msg: MessageOut } }
  | { event: "turn.started"; payload: { turn_id: number } }
  | { event: "turn.updated"; payload: { turn_id: number; status: string } }
  | { event: "turn.completed"; payload: { turn_id: number; summary: string | null; artifact_ids: number[] } }
  | { event: "turn.interrupted"; payload: { turn_id: number; last_message_id: number | null } }
  | { event: "turn.failed"; payload: { turn_id: number; status: string; summary: string | null; error: string | null } }
  | { event: "turn.rolled_back"; payload: { turn_id: number; rolled_back_msgs: number; file_recovery: Record<string, unknown> } }
  | { event: "agent.started"; payload: { agent_id: number; kind: string; name: string; turn_id: number | null } }
  | { event: "agent.updated"; payload: { agent_id: number; status: string; tool?: string; step?: number } }
  | { event: "agent.completed"; payload: { agent_id: number; summary: string | null; artifact_ids: number[] } }
  | { event: "subagent.failed"; payload: { agent_id: number; status: string; error: string | null } }
  | { event: "thinking.delta"; payload: { agent_id: number; turn_id: number | null; delta: string } }
  | { event: "thinking.done"; payload: { agent_id: number; turn_id: number | null; full_text: string } }
  | { event: "token.delta"; payload: { agent_id: number; turn_id: number | null; delta: string } }
  | { event: "token.done"; payload: { agent_id: number; turn_id: number | null; full_text: string } }
  /** v35: turn 级瞬态状态（重试/恢复提示），前端流式状态行展示；text 空串 = 清除 */
  | { event: "turn.status"; payload: { turn_id: number; thread_id?: number | null; text: string } }
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
