/** 后端 API 客户端（v2：项目任务驱动架构）。
 *
 * 端点与 server/app/gateway/routers 一一对应，类型从 packages/shared 导入。
 */
import type {
  ArtifactOut,
  CompactionIndexOut,
  ConfigProfileOut,
  CronValidateOut,
  ExecPolicyRuleOut,
  FileChangeOut,
  FileDiffOut,
  HookConfigOut,
  MemoryEntryOut,
  MessageOut,
  ModelOut,
  ProjectOut,
  ProviderOut,
  ProviderCredentialOut,
  ScannedModel,
  ScheduledMissedPolicy,
  RollbackAffected,
  RollbackConflict,
  RollbackPreviewFile,
  RollbackPreviewOut,
  RollbackResult,
  RevertFilesResult,
  RevertLockOut,
  ScheduledTaskOut,
  GoalOut,
  SessionOut,
  TaskOut,
  TurnOut,
  TurnSnapshotOut,
  DbConnectionOut,
  DbPolicyOut,
  DebugStatusOut,
  DebugSettingsOut,
  ArthasStatusOut,
  ArthasEntryOut,
  ArthasProcessOut,
  ArthasConfigOut,
  PluginMarketItem,
  RepoCandidate,
  WorktreeMergeFile,
  WorktreeMergePreview,
  WorktreeMergeDirection,
  WorktreeOut,
  WorktreeRepoOut,
  MergeReportOut,
} from "@chatcoder/shared";

/** 后端 API 基址:桌面版直连 127.0.0.1:12973,网页版用相对路径走代理。
 * v6.4: 开发模式直连后端，绕过 vite 代理（Node 18+ Happy Eyeballs IPv6 问题）。
 * v2.1: 打包版端口由主进程探活后透传（getBackendPort），端口冲突自动换空闲端口。 */
const IS_ELECTRON = typeof window !== "undefined" && Boolean((window as Window).chatcoderAPI);
const IS_DEV = import.meta.env.DEV;
/** plan-20261009-ui D0：开发态可用 VITE_API_PORT 指向独立的开发后端（隔离测试库），
 *  避免浏览器调试时读写已安装版本的真实数据；未设置时保持 12973。 */
export const DEV_API_PORT = Number(import.meta.env.VITE_API_PORT) || 12973;

/** 桌面版后端端口（主进程透传，Promise 缓存） */
let _backendPortPromise: Promise<number> | null = null;
function backendPort(): Promise<number> {
  if (!_backendPortPromise) {
    const api = (window as Window).chatcoderAPI as { getBackendPort?: () => Promise<number> };
    _backendPortPromise = (api?.getBackendPort?.() ?? Promise.resolve(12973))
      .then((p) => (Number.isFinite(p) && p > 0 ? p : 12973))
      .catch(() => 12973);
  }
  return _backendPortPromise;
}

/** 桌面版/开发模式的 API 基址（异步解析端口） */
async function directBase(): Promise<string> {
  const port = IS_ELECTRON ? await backendPort() : DEV_API_PORT;
  return `http://127.0.0.1:${port}/api`;
}

/** 默认基址：同步兜底（12973），实际请求前会用 directBase 修正 */
const BASE = IS_ELECTRON ? "http://127.0.0.1:12973/api" : IS_DEV ? `http://127.0.0.1:${DEV_API_PORT}/api` : "/api";

/** 已解析的直接基址缓存 */
let _directBase: string | null = null;
async function baseForFetch(): Promise<string> {
  if (IS_ELECTRON || IS_DEV) {
    if (!_directBase) _directBase = await directBase();
    return _directBase;
  }
  return BASE;
}

/** 带重试的 fetch — 后端启动中时自动重试,避免 "Failed to fetch" */
async function fetchWithRetry(
  input: string,
  init?: RequestInit,
  maxRetries: number = IS_ELECTRON ? 8 : 0,
): Promise<Response> {
  let lastErr: unknown;
  for (let i = 0; i <= maxRetries; i++) {
    try {
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), 10000);
      const res = await fetch(input, { ...init, signal: controller.signal });
      clearTimeout(timeoutId);
      return res;
    } catch (err) {
      lastErr = err;
      if (i < maxRetries) {
        await new Promise((r) => setTimeout(r, 300 * Math.pow(2, i)));
      }
    }
  }
  throw lastErr;
}

/** 统一错误解析：后端返回 {"error":{code,message}}（部分路由为 FastAPI 的 {"detail":...}）。 */
function errorMessage(res: Response, raw: string): string {
  const parsed = parseErrorBody(raw);
  if (parsed?.message) return parsed.message;
  if (parsed?.code) return `[${parsed.code}]`;
  return `HTTP ${res.status}: ${raw.slice(0, 200)}`;
}

/** plan-278-1391: 结构化解析错误响应体，供调用方按 code 分支处理（如「项目已归档」）。 */
function parseErrorBody(raw: string): { message?: string; code?: string; data?: unknown } | null {
  try {
    const body = JSON.parse(raw) as {
      error?: { message?: string; code?: string; detail?: unknown };
      detail?: unknown;
    };
    if (body?.error?.message || body?.error?.code) {
      // 服务端保留的结构化详情（HTTPException 的 dict 详情）优先：界面按它分支处理
      const structured = body.error.detail;
      if (structured && typeof structured === "object" && !Array.isArray(structured)) {
        const s = structured as { message?: string; code?: string };
        return { message: s.message ?? body.error.message, code: s.code ?? body.error.code, data: structured };
      }
      return { message: body.error?.message, code: body.error?.code, data: body.error };
    }
    const d = body?.detail;
    if (d && typeof d === "object" && !Array.isArray(d)) {
      const dobj = d as { message?: string; code?: string };
      return { message: dobj.message, code: dobj.code, data: d };
    }
    if (typeof d === "string") return { message: d, data: d };
  } catch { /* 非 JSON */ }
  return null;
}

/** API 错误：携带 status / code / detail，便于调用方做结构化分支。 */
export class ApiError extends Error {
  status: number;
  code?: string;
  detail?: unknown;
  constructor(message: string, status: number, code?: string, detail?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

/** 由响应构造 ApiError（统一入口，避免各处重复解析）。 */
function toApiError(res: Response, raw: string): ApiError {
  const parsed = parseErrorBody(raw);
  return new ApiError(errorMessage(res, raw), res.status, parsed?.code, parsed?.data);
}

async function post<T>(path: string, body?: unknown): Promise<T> {
  // POST 不可重试——防止 LLM 慢响应时重复创建数据
  const res = await fetch(`${await baseForFetch()}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
    signal: AbortSignal.timeout(600_000),
  });
  if (!res.ok) {
    const detail = await res.text();
    throw toApiError(res, detail);
  }
  return res.json() as Promise<T>;
}

/** 取消是控制信号，不应继承普通 POST 的 10 分钟 LLM 超时。 */
async function postCancel<T>(path: string): Promise<T> {
  const res = await fetch(`${await baseForFetch()}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    signal: AbortSignal.timeout(2500),
  });
  if (!res.ok) {
    const detail = await res.text();
    throw toApiError(res, detail);
  }
  return res.json() as Promise<T>;
}

async function get<T>(path: string): Promise<T> {
  const res = await fetchWithRetry(`${await baseForFetch()}${path}`);
  if (!res.ok) {
    const detail = await res.text();
    throw toApiError(res, detail);
  }
  return res.json();
}

async function patch<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetchWithRetry(`${await baseForFetch()}${path}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const detail = await res.text();
    throw toApiError(res, detail);
  }
  return res.json();
}

async function del<T>(path: string): Promise<T> {
  const res = await fetchWithRetry(`${await baseForFetch()}${path}`, { method: "DELETE" });
  if (!res.ok) {
    const detail = await res.text();
    throw toApiError(res, detail);
  }
  return res.json();
}

async function put<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetchWithRetry(`${await baseForFetch()}${path}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const detail = await res.text();
    throw toApiError(res, detail);
  }
  return res.json();
}

// ── 目录树（项目）──
export interface TreeNode {
  name: string;
  type: "dir" | "file";
  path: string;
  children?: TreeNode[];
}

export interface ProjectTreeOut {
  path: string;
  children: TreeNode[];
}

/** plan-308-1542 需求2：输入框引用 chips 的**结构化**副本。
 *  从前只把引用拼成文本行（"使用技能：$x"）落库，消息流只能靠正则解析，
 *  且正则无终止符会把整行吞成一个纯文本 token（发送后图标变文字）。
 *  落库结构化副本后，消息流可直接渲染出与输入框一致的带图标芯片。 */
export interface ComposerRefOut {
  kind: "file" | "skill" | "mcp" | "plugin";
  value: string;
  label: string;
}

export interface TurnCreateBody {
  session_id: number;
  content: string;
  attachments?: Record<string, unknown>[];
  scheduled_task_id?: number;
  reasoning_effort?: string;
  // plan-230-1144 M2: 模式名放宽为 string——自定义权限模式需透传（后端 resolve_tools 解析白名单）
  mode?: string | null;
  // plan-166-767: 发送请求携带当前会话选中模型，后端据此优先解析（切换后立即发送用新模型）
  model_id?: number;
  // plan-308-1542 需求2: 引用芯片结构化落库（消息流据此渲染图标芯片）
  refs?: ComposerRefOut[];
}

export interface RollbackParams {
  restore_to_composer?: boolean;
  /** 跳过与手动改动冲突的文件，回滚其余（用户在确认弹窗里明确选择后才传 true） */
  allow_partial?: boolean;
}

/** 归档批量永久删除结果（plan-20261014-fix4，对应 POST /archive/purge）。 */
export interface ArchivePurgeResult {
  ok: boolean;
  deleted_projects: number;
  deleted_sessions: number;
  /** 已不存在的 id（重复提交或列表未刷新），不视为错误 */
  missing: { projects: number[]; sessions: number[] };
  /** 清理的附件目录与回滚备份文件数 */
  files_removed: number;
  /** 是否执行了数据库空间回收；有任务在运行或空闲空间很少时跳过 */
  compacted: boolean;
  freed_bytes: number;
  skipped?: "busy" | "small" | "unsupported" | "error";
}

export interface AuditLogOut {
  id: number;
  session_id: number | null;
  turn_id: number | null;
  action: string;
  detail: Record<string, unknown> | null;
  created_at: string | null;
}

export interface DiagnosticsOut {
  ok: boolean;
  /** plan-282-1434：后端实际返回**对象**（`{"git": {ok, detail}, ...}`，见
   *  server/app/gateway/routers/diagnostics.py），此处放宽为两种形态以贴合现实；
   *  消费侧（DiagnosticsPanel）自行归一化为列表。 */
  checks:
    | { name: string; ok: boolean; detail?: string }[]
    | Record<string, { ok?: boolean; detail?: string } | undefined>;
  /** plan-88: 各项目工作区 .chatcoder/checkpoints 占用统计 */
  checkpoints?: Array<{ workspace: string; file_count: number; size_mb: number; orphan_count: number }>;
}

export interface UpdateCheckOut {
  current: string;
  latest: string;
  has_update: boolean;
}

export interface SkillOut {
  id: number;
  name: string;
  display_name: string | null;
  description: string | null;
  source: string;
  path: string | null;
  content: string | null;
  trigger: string | null;
  tools: string[] | null;
  tags: string[] | null;
  is_active: boolean;
  auto_load: boolean;
  /** plan-284-1450：安装来源元数据（含市场图标），已安装列表据此复用市场图标 */
  meta?: Record<string, unknown> | null;
}

/** plan-230-1144 M2: 权限模式（白名单+提示词） */
export interface PermissionProfileOut {
  name: string;
  display_name: string;
  kind: "full" | "readonly" | "plan";
  builtin: boolean;
  description: string;
  /** 工具白名单；空数组 = 全量工具（不限制） */
  tools: string[];
  hint: string;
}

export interface McpServerOut {
  id: number;
  name: string;
  display_name: string | null;
  description: string | null;
  source: string;
  transport: string;
  command: string | null;
  args: string[] | null;
  env: Record<string, string> | null;
  url: string | null;
  tools: Record<string, unknown>[] | null;
  is_active: boolean;
  /** plan-284-1450：安装来源元数据（含市场图标） */
  meta?: Record<string, unknown> | null;
}

export interface ScanResult {
  added: number;
  updated: number;
  unchanged: number;
  total_scanned: number;
}

/** plan-152-704: 全软件 token 用量统计（/api/usage/stats） */
export interface UsageStatsOut {
  total: {
    prompt: number; completion: number; reasoning: number; cached: number;
    total: number; calls: number;
  };
  /** plan-308-1542 需求6：区间内出现过的供应商（下拉框选项） */
  providers?: string[];
  /** 当前筛选的供应商（null = 全部） */
  provider?: string | null;
  by_model: Array<{
    key: string; model: string; provider_name: string; display_name: string;
    prompt: number; completion: number; reasoning: number; cached: number;
    calls: number; total: number;
  }>;
  daily: Array<{ date: string; tokens: number; calls: number }>;
  daily_by_model: Array<{ date: string; key: string; display_name: string; tokens: number }>;
  daily_all: Array<{ date: string; tokens: number }>;
  peak_tokens: number;
  streak_current: number;
  streak_longest: number;
  /** plan-20261021-usage-update：区间内平均首字 / 平均速度（旧后端无此字段） */
  perf?: UsagePerfSummary;
}

export interface UsagePerfSummary {
  avg_first_token_ms: number | null;
  avg_speed: number | null;
  /** 参与平均速度的实测调用数（输出 ≥ 100 token） */
  speed_calls: number;
}

/** plan-20261021-usage-update：/api/usage/records 的一行（一次模型调用） */
export interface UsageRecordItem {
  id: number;
  /** UTC 时间，带 Z；前端按本地时区格式化 */
  time: string | null;
  provider: string;
  model: string;
  /** 账号 / Key 备注名（OAuth 账号名、自定义备注；可能为空） */
  account: string;
  /** 掩码后的 Key（如 sk-a••••9f2a；OAuth 账号为空） */
  account_hint: string;
  prompt: number;
  cached: number;
  completion: number;
  reasoning: number;
  /** 请求发起 → 首个输出帧（毫秒）；历史记录为 null */
  first_token_ms: number | null;
  /** 首个输出帧 → 结束（毫秒）；历史记录为 null */
  gen_ms: number | null;
  /** 估算行：按会话时间戳估算的整次请求耗时（毫秒，含等首字） */
  est_ms: number | null;
  /** 输出速度 token/s；不满足口径（输出太少 / 无耗时）为 null */
  speed: number | null;
  /** true = 按会话时间戳估算（前端以 ≈ 标注） */
  estimated: boolean;
  session_id: number | null;
}

export interface UsageRecordsOut {
  total: number;
  page: number;
  page_size: number;
  pages: number;
  provider: string | null;
  /** 整个筛选结果（不止当前页）的平均首字 / 平均速度 */
  summary: UsagePerfSummary;
  items: UsageRecordItem[];
}

/* ── 工作流（设计 §5.9）：程序快照与操作 ── */
/** 并发池占用（服务端 scheduler.snapshot）：limit 为 0 表示不限 */
export interface WorkflowPoolOut {
  global: { in_use: number; limit: number };
  session: { limit: number; in_use: Record<string, number> };
  queued: number;
  /** 跨会话的工作流计数（设置页概览）：运行中（含等待决定）与已暂停 */
  programs?: { running: number; paused: number };
}

/** 工作流模板：内置预设（只读）与用户模板（可改名、停用、删除） */
export interface WorkflowTemplateOut {
  key: string;
  name: string;
  description: string;
  source: "preset" | "user";
  enabled: boolean;
  plan: Record<string, unknown>;
  updated_at?: string | null;
}

/** 续跑前的工作区报告（设计 §6.6） */
export interface WorkflowDriftOut {
  files: string[];
  head: { from: string | null; to: string | null } | null;
}

/** 单个改动文件的差异：diff 为空时 reason 说明原因 */
export interface WorkflowDriftDiffOut {
  path: string;
  status: "added" | "modified" | "deleted";
  diff: string | null;
  reason: string | null;
}

/** 节点的一次尝试（设计 §7.6 尝试历史） */
export interface WorkflowAttemptOut {
  attempt_no: number;
  mode: "fresh" | "checkpoint" | "rework" | "repair" | string;
  status: string;
  reason: string | null;
  started_at: string | null;
  ended_at: string | null;
}

/* 字段与服务端 store._run_dict / _node_dict 一一对应：服务端新增字段时这里同步补齐 */
export type WorkflowRunStatus =
  | "draft" | "running" | "waiting_human" | "paused" | "succeeded" | "failed" | "cancelled";

export interface WorkflowRunOut {
  id: number;
  session_id: number;
  parent_run_id: number | null;
  kind: string;
  package_key: string | null;
  title: string;
  goal: string;
  rationale: string;
  based_on: string | null;
  status: WorkflowRunStatus | string;
  pause_kind: string | null;
  pause_reason: string | null;
  /** 结束原因：user（用户取消）/ adjusted（已按新的要求调整，由新计划替代） */
  closed_reason?: string | null;
  paused_at: string | null;
  resume_count: number;
  rework_total: number;
  token_used: number;
  token_budget: number;
  workspace: string;
  isolation: string;
  /** 计划图（plan_workflow 规范化结果）：flows[].key / title / depends_on / nodes */
  graph: Record<string, unknown> | null;
  plan_rev: number;
  origin_turn_id: number | null;
  last_turn_id: number | null;
  /** 程序内事件序号（非会话级 seq） */
  last_seq: number;
  /** 步骤完成情况：仅列表摘要携带（快照接口由节点直接推导） */
  progress?: { done: number; total: number };
  started_at: string | null;
  updated_at: string | null;
  finished_at: string | null;
  error: Record<string, unknown> | null;
}

export interface WorkflowNodeOut {
  id: number;
  run_id: number;
  key: string;
  title: string;
  kind: string;
  role: string | null;
  task: string;
  inputs: string[];
  acceptance: string[];
  write_scope: string[];
  commands: string[];
  rework_to: string | null;
  max_rework: number | null;
  question: string | null;
  status: string;
  reason: string | null;
  attempt_no: number;
  rework_round: number;
  output: Record<string, unknown> | null;
  verdict: Record<string, unknown> | null;
  checks: Array<Record<string, unknown>> | null;
  agent_id: number | null;
  next_retry_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  error: Record<string, unknown> | null;
  pending: Record<string, unknown> | null;
  /** 冲突闸门载荷：基线 / 当前 / 待写入三栏数据（仅写入冲突时存在） */
  conflict: Record<string, unknown> | null;
  token_used: number;
}

export interface WorkflowFlowOut {
  run: WorkflowRunOut;
  nodes: WorkflowNodeOut[];
}

export interface WorkflowProgramOut {
  run: WorkflowRunOut;
  flows: WorkflowFlowOut[];
}

/** 子代理面板的工作流节点头：节点、所属子工作流与程序、最近一次尝试的模式、评估结论与产出摘要 */
export interface WorkflowNodeBrief {
  node_id: number;
  node_key: string;
  node_title: string;
  kind: string;
  status: string;
  attempt_no: number;
  rework_round: number;
  mode: string;
  verdict: string;
  summary: string;
  flow_key: string;
  flow_title: string;
  program_id: number | null;
  program_title: string;
  /** 产出明细（按节点类型）：explore → plan；execute → verification；review → issues */
  detail: {
    plan?: string[];
    verification?: string[];
    issues?: Array<{ severity: string; file: string; line: number | null; problem: string; fix_hint: string }>;
  };
}

export interface WorkflowEventOut {
  seq: number;
  type: string;
  node_key: string | null;
  payload: Record<string, unknown>;
  created_at: string;
}

/** plan-156-619 S10：后续任务建议（消息流底部卡片的数据契约）。 */
export interface TaskSuggestionOut {
  id: number;
  session_id: number;
  turn_id: number | null;
  title: string;
  summary: string;
  steps: string[];
  /** 建议在子会话里用什么执行模式 */
  mode: string;
  rationale?: string;
}

/** Git 面板数据（plan-20261017-goal12 项 5）。 */
export interface GitFileEntry { path: string; code: string; old_path: string | null }
export interface GitStatusOut {
  root: string;
  branch: string | null;
  detached: boolean;
  upstream: string | null;
  ahead: number;
  behind: number;
  oid: string | null;
  state: "normal" | "merging" | "rebasing";
  staged: GitFileEntry[];
  unstaged: GitFileEntry[];
  untracked: GitFileEntry[];
  conflicts: GitFileEntry[];
  clean: boolean;
  /** plan-20261019-git-multirepo：/git/status-all 汇总的各仓库信息（普通 /git/status 不返回） */
  repos?: Array<{ rel: string; branch: string | null; clean: boolean }>;
}
export interface GitDiffOut { path: string; text: string; truncated: boolean; binary: boolean; new: boolean }
/** 提交上的引用装饰（分支 / 远端分支 / 标签 / HEAD），历史行上以小标签展示。 */
export interface GitRefOut { name: string; kind: "head" | "branch" | "remote" | "tag" }
export interface GitLogEntry {
  sha: string; short: string; author: string; date: string; subject: string;
  parents: string[]; refs: GitRefOut[];
}
export interface GitCommitFileOut {
  path: string;
  /** 变更类型首字母：A 新增 / M 修改 / D 删除 / T 类型变更 */
  code: string;
  additions: number | null;
  deletions: number | null;
}
export interface GitCommitDetailOut {
  sha: string; short: string; author: string; email: string; date: string;
  subject: string; message: string; parents: string[]; merge: boolean;
  files: GitCommitFileOut[]; additions: number; deletions: number;
}
export interface GitBranchesOut {
  local: Array<{ name: string; current: boolean; upstream: string | null; date: string }>;
  remote: Array<{ name: string; date: string }>;
}
/** plan-20261019-toolcard-v2：工具卡 Git 栏的一个仓库摘要——分支 / 领先落后 / 未提交改动文件数与增删行数 */
export interface GitCardRepoOut {
  rel: string;
  name: string;
  branch: string | null;
  detached: boolean;
  oid: string | null;
  ahead: number;
  behind: number;
  /** 未提交改动的文件数（已暂存 + 未暂存 + 未跟踪 + 冲突，同一文件只算一次） */
  files: number;
  /** 未提交改动新增行数（已暂存 + 未暂存相对 HEAD，加未跟踪文本文件的行数） */
  additions: number;
  deletions: number;
}
export interface GitDiffStatOut {
  additions: number;
  deletions: number;
  files: number;
  repos: GitCardRepoOut[];
}
/** plan-20261019-git-multirepo：项目内的一个仓库（根仓库 rel 为空串） */
export interface GitRepoOut {
  rel: string;
  name: string;
  path: string;
  is_root: boolean;
  branch: string | null;
  detached: boolean;
  ahead: number;
  behind: number;
  /** 未提交改动的文件数（含未跟踪） */
  changes: number;
}
/** plan-20261019-git-multirepo：Git 接口的作用对象——哪个仓库（相对项目根）、哪个会话（会话工作树优先） */
export interface GitScope { repo?: string; sessionId?: number | null }

function gitQs(projectId: number, scope: GitScope = {}): string {
  const q = new URLSearchParams({ project_id: String(projectId) });
  if (scope.repo) q.set("repo", scope.repo);
  if (scope.sessionId != null) q.set("session_id", String(scope.sessionId));
  return q.toString();
}

function gitBody(projectId: number, scope: GitScope = {}): Record<string, unknown> {
  return { project_id: projectId, repo: scope.repo || null, session_id: scope.sessionId ?? null };
}

export const api = {
  // ── 项目 ──
  listProjects: (params?: { include_archived?: boolean }) =>
    get<ProjectOut[]>(`/projects${params?.include_archived ? "?include_archived=true" : ""}`),
  createProject: (data: { path: string; name?: string; rules_docs?: string[]; auto_scan_rules?: boolean; icon?: string | null; color?: string | null }) =>
    post<ProjectOut>("/projects", data),
  getProject: (id: number) => get<ProjectOut>(`/projects/${id}`),
  updateProject: (id: number, data: { name?: string; rules_docs?: string[]; auto_scan_rules?: boolean; pinned?: boolean; archived?: boolean; icon?: string | null; color?: string | null }) =>
    patch<ProjectOut>(`/projects/${id}`, data),
  deleteProject: (id: number) => del<{ ok: boolean }>(`/projects/${id}`),
  // ── 工作树（plan-282-1441 #5）──
  /** 可用于创建工作树的仓库（项目根 + 子仓库） */
  listRepoCandidates: (projectId: number) =>
    get<RepoCandidate[]>(`/projects/${projectId}/repo-candidates`),
  listWorktrees: (projectId: number) => get<WorktreeOut[]>(`/projects/${projectId}/worktrees`),
  createWorktreeForProject: (projectId: number, data: { name?: string; branch?: string; repos?: string[] } = {}) =>
    post<{ ok: boolean; count: number; worktrees: Array<{ project_id: number; path: string; branch: string; name: string; repo: string }>; project_id: number; path: string; branch: string; name: string }>(
      `/projects/${projectId}/worktrees`, data),
  deleteWorktreeProject: (worktreeProjectId: number, force = false) =>
    del<{ ok: boolean; branch?: string | null; branch_deleted?: boolean; deleted_sessions?: number; detached?: boolean }>(
      `/projects/worktrees/${worktreeProjectId}${force ? "?force=true" : ""}`),
  /** plan-308-1542 修复：清理"git 侧已不存在、数据库仍登记"的失效工作树（左面板僵尸项） */
  cleanupStaleWorktrees: () =>
    post<{ ok: boolean; cleaned: number; stale: Array<{ id: number; name: string; path: string }> }>(
      "/projects/worktrees/cleanup-stale", {}),
  /** plan-20261019-git-multirepo：工作树包含的仓库（工作树组有多个，合并时逐个选择） */
  worktreeRepos: (worktreeProjectId: number) =>
    get<WorktreeRepoOut[]>(`/projects/worktrees/${worktreeProjectId}/repos`),
  // plan-20261019-git-multirepo：以下合并接口的 repo 为工作树组里的成员（相对路径，"." = 根仓库）
  worktreeMergePreview: (worktreeProjectId: number, direction: WorktreeMergeDirection = "to_main", repo = "") =>
    post<WorktreeMergePreview>(`/projects/worktrees/${worktreeProjectId}/merge/preview`, { direction, repo }),
  worktreeMergeFile: (worktreeProjectId: number, path: string, direction: WorktreeMergeDirection = "to_main", repo = "") =>
    post<{ ok: boolean; path: string; base: string | null; ours: string | null; theirs: string | null; binary?: boolean }>(
      `/projects/worktrees/${worktreeProjectId}/merge/file`, { path, direction, repo }),
  worktreeMergeApply: (
    worktreeProjectId: number,
    files: Array<{ path: string; content?: string; deleted?: boolean }>,
    direction: WorktreeMergeDirection = "to_main",
    repo = "",
  ) =>
    post<{ ok: boolean; committed: boolean; written: string[] }>(
      `/projects/worktrees/${worktreeProjectId}/merge/apply`, { files, direction, repo }),
  worktreeMergeAi: (
    worktreeProjectId: number,
    path: string,
    hunk?: Record<string, unknown>,
    opts?: { direction?: WorktreeMergeDirection; modelId?: number | null; repo?: string },
  ) =>
    post<{ ok: boolean; suggestion?: string; error?: string; model?: string }>(
      `/projects/worktrees/${worktreeProjectId}/merge/ai`,
      { path, hunk, direction: opts?.direction ?? "to_main", model_id: opts?.modelId ?? null, repo: opts?.repo ?? "" }),
  /** plan-308-1542 需求3-A：一键 AI 智能合并（进度经 WS merge.progress 广播）。 */
  worktreeMergeAiAll: (
    worktreeProjectId: number,
    opts?: { direction?: WorktreeMergeDirection; modelId?: number | null; sessionId?: number | null; repo?: string },
  ) =>
    post<{ ok: boolean; merge_id: string; resolved?: Record<string, string>; report?: MergeReportOut; engine?: string; preview?: WorktreeMergePreview }>(
      `/projects/worktrees/${worktreeProjectId}/merge/ai-all`,
      { direction: opts?.direction ?? "to_main", model_id: opts?.modelId ?? null, session_id: opts?.sessionId ?? null,
        repo: opts?.repo ?? "" }),
  /** 合并前自动提交某一侧的未提交改动（side: worktree | main） */
  worktreeCommit: (worktreeProjectId: number, side: "worktree" | "main", message?: string, repo = "") =>
    post<{ ok: boolean; committed: boolean; message: string }>(
      `/projects/worktrees/${worktreeProjectId}/commit`, { side, message, repo }),
  // ── 插件市场（plan-282-1441 #6）──
  pluginMarketplace: () =>
    get<{ ok: boolean; items: PluginMarketItem[]; installedCount: number }>("/plugins/marketplace"),
  pluginInstalled: () => get<PluginMarketItem[]>("/plugins/installed"),
  pluginInstallDir: (path: string) =>
    post<{ ok: boolean; name: string; path: string; skills: number; connectorCount?: number }>("/plugins/install-dir", { path }),
  pluginInstallGit: (repoUrl: string, market = "local") =>
    post<{ ok: boolean; name: string; path: string; skills: number; connectorCount?: number }>("/plugins/install-git", { repo_url: repoUrl, market }),
  pluginSetEnabled: (name: string, enabled: boolean) =>
    patch<{ ok: boolean; name: string; enabled: boolean }>(
      `/plugins/${encodeURIComponent(name)}/enabled`, { enabled }),
  pluginUninstall: (name: string) =>
    del<{ ok: boolean; name: string }>(`/plugins/${encodeURIComponent(name)}`),
  // ── 市场目录（plan-41-198）：左面板「拓展」市场视图的统一数据源 ──
  /** 市场目录：内置精选 + 本机真实项叠加（已安装 / 可直接安装 / 需前往市场）。 */
  marketCatalog: (kind: MarketKind) =>
    get<MarketCatalogOut>(`/market/catalog?kind=${kind}`),
  /** 已安装聚合计数（插件 / 技能 / 连接器 / 智能体）。 */
  marketInstalled: () =>
    get<{ ok: boolean; counts: Record<MarketKind | "agent", number> }>("/market/installed"),
  /** plan-41-225：市场浏览（远端分页 + 已装状态 + 缓存降级）。
   *  全量可见的实现：远端分页 + 无限滚动（本地只渲染可视区）；搜索/分类透传远端参数
   *  以覆盖全库（43,710 条技能量级），而不是本地过滤已加载的那几页。 */
  marketBrowse: (opts: {
    kind: MarketKind; page?: number; pageSize?: number;
    keyword?: string; category?: string; sort?: "hot" | "new";
  }) => {
    const q = new URLSearchParams({ kind: opts.kind });
    q.set("page", String(opts.page ?? 1));
    q.set("page_size", String(opts.pageSize ?? 40));
    if (opts.keyword) q.set("keyword", opts.keyword);
    if (opts.category) q.set("category", opts.category);
    if (opts.sort) q.set("sort", opts.sort);
    return get<MarketBrowseOut>(`/market/browse?${q.toString()}`);
  },
  /** plan-41-225：把市场条目安装到本机（三类均可，全程不跳浏览器）。
   *  ident 为**远端条目 id**（从 `MarketItem.id` 形如 `skill:xxx` 拆出）；
   *  技能→下载包并落盘入库；插件→下载包并复用插件安装流程；连接器→写 MCP 配置（默认不启用）。 */
  marketInstall: (data: {
    kind: MarketKind; ident: string; displayName?: string; description?: string;
  }) => post<{
    ok: boolean; kind: string; name: string; path?: string;
    skills?: number; bytes?: number; connectors?: string[];
    /** plan-59-286：插件贡献的连接器数量（与连接器安装返回的 connectors 清单区分开） */
    connectorCount?: number;
    updated?: boolean; enabled?: boolean;
  }>("/market/install", data),

  // ── 数据库连接（plan-282-1441 #7，内置 MCP）──
  listDbConnections: (projectId: number) => get<DbConnectionOut[]>(`/db/connections?project_id=${projectId}`),
  createDbConnection: (data: {
    project_id: number; name: string; kind: string; host: string; port?: number;
    database?: string; username?: string; password?: string;
    params?: Record<string, unknown>; is_active?: boolean;
  }) => post<{ ok: boolean; id: number }>("/db/connections", data),
  updateDbConnection: (id: number, data: {
    name?: string; kind?: string; host?: string; port?: number; database?: string;
    username?: string; password?: string; params?: Record<string, unknown>; is_active?: boolean;
  }) => patch<{ ok: boolean }>(`/db/connections/${id}`, data),
  deleteDbConnection: (id: number) => del<{ ok: boolean }>(`/db/connections/${id}`),
  testDbConnection: (id: number) =>
    post<{ ok: boolean; server?: string; error?: string }>(`/db/connections/${id}/test`, {}),
  getDbPolicy: (projectId: number) => get<DbPolicyOut>(`/db/policy?project_id=${projectId}`),
  setDbPolicy: (projectId: number, data: Partial<DbPolicyOut>) =>
    put<DbPolicyOut>(`/db/policy?project_id=${projectId}`, data),
  // ── 调试状态（plan-282-1441 #8，供调试面板轮询/收尾）──
  debugStatus: (sessionId: number, target: "web" | "java") =>
    post<DebugStatusOut>(`/debug/${target}/status`, { session_id: sessionId }),
  debugAction: (target: "web" | "java", action: string, sessionId: number, extra: Record<string, unknown> = {}) =>
    post<Record<string, unknown>>(`/debug/${target}/${action}`, { session_id: sessionId, ...extra }),
  // ── plan-308-1542 需求7-A：断点明细 / 删除 / 清空 ──
  debugBreakpoints: (sessionId: number, target: "web" | "java") =>
    post<{ ok: boolean; connected: boolean; breakpoints: Array<{ id: string; file?: string | null; line?: number | null; class?: string | null; source?: string; enabled?: boolean }> }>(
      "/debug/breakpoints", { session_id: sessionId, target }),
  debugRemoveBreakpoint: (sessionId: number, target: "web" | "java", breakpointId: string) =>
    post<{ ok: boolean; error?: string }>("/debug/breakpoints/remove",
      { session_id: sessionId, target, breakpoint_id: breakpointId }),
  debugClearBreakpoints: (sessionId: number, target: "web" | "java") =>
    post<{ ok: boolean; removed?: number }>("/debug/breakpoints/clear", { session_id: sessionId, target }),
  // ── plan-308-1542 需求7-B：与 IntelliJ IDEA 的双向断点通道 ──
  ideaBreakpoints: (projectPath: string) =>
    post<{ ok: boolean; available?: boolean; reason?: string; count?: number; path?: string; breakpoints: Array<{ id: string; file: string; line: number | null; enabled?: boolean; source?: string }> }>(
      "/debug/idea/breakpoints", { project_path: projectPath }),
  ideaAddBreakpoint: (projectPath: string, file: string, line: number) =>
    post<{ ok: boolean; added?: boolean; duplicated?: boolean; backup?: string; warning?: string; error?: string }>(
      "/debug/idea/breakpoints/add", { project_path: projectPath, file, line }),
  ideaRemoveBreakpoint: (projectPath: string, file: string, line: number) =>
    post<{ ok: boolean; removed?: number; warning?: string; error?: string }>(
      "/debug/idea/breakpoints/remove", { project_path: projectPath, file, line }),
  ideaDebugSession: (projectPath: string) =>
    post<{ ok: boolean; idea_running: boolean; sessions: Array<{ pid: number; main_class: string; jdwp_port: number }>; note?: string }>(
      "/debug/idea/session", { project_path: projectPath }),
  ideaMethodAtLine: (projectPath: string, file: string, line: number) =>
    post<{ ok: boolean; class?: string; method?: string | null; target?: string | null; error?: string }>(
      "/debug/idea/method-at-line", { project_path: projectPath, file, line }),
  // 面板配置（落库）：Web 调试端口 / JDWP 目标
  debugSettings: () => get<DebugSettingsOut>("/debug/settings"),
  saveDebugSettings: (patch: Partial<DebugSettingsOut>) =>
    put<DebugSettingsOut>("/debug/settings", patch),
  // ── Arthas 现场诊断（可与 IDEA 调试并存；见《chatcoder 调试 MCP 集成 Arthas 方案》）──
  arthasStatus: (sessionId: number) =>
    post<ArthasStatusOut>("/debug/arthas/status", { session_id: sessionId }),
  arthasProcesses: (sessionId: number, javaHome?: string) =>
    post<{ ok: boolean; processes: ArthasProcessOut[]; error?: string;
           java_home?: string; java_home_source?: string }>(
      "/debug/arthas/processes", { session_id: sessionId, java_home: javaHome || null }),
  arthasAttach: (sessionId: number, pid: number) =>
    post<ArthasStatusOut>("/debug/arthas/attach", { session_id: sessionId, pid }),
  arthasStop: (sessionId: number) =>
    post<{ ok: boolean; message?: string; pid?: number }>("/debug/arthas/stop", { session_id: sessionId }),
  /** observe=true：建立持续观测（异步作业 + 后台泵推送命中），面板「建立观测」用它；
   *  不传则为同步执行（一次性命令）。 */
  arthasExec: (sessionId: number, command: string, opts: { observe?: boolean } = {}) =>
    post<{ ok: boolean; job_id?: string | number | null; error?: string | null; command?: string }>(
      "/debug/arthas/exec",
      opts.observe
        ? { session_id: sessionId, command, async: true, observe: true }
        : { session_id: sessionId, command }),
  arthasInterrupt: (sessionId: number, jobId: string | number) =>
    post<{ ok: boolean; message?: string; error?: string | null }>(
      "/debug/arthas/interrupt", { session_id: sessionId, job_id: jobId }),
  arthasConfig: () => post<ArthasConfigOut>("/debug/arthas/config", {}),
  setArthasConfig: (patch: Record<string, unknown>) =>
    put<{ ok: boolean; error?: string }>("/debug/arthas/config", patch),
  scanProjectRules: (id: number) => get<string[]>(`/projects/${id}/scan-rules`),
  getProjectTree: (id: number, depth = 8) => get<ProjectTreeOut>(`/projects/${id}/tree?depth=${depth}`),
  /** 问题2: 轻量文件存在性校验（不读内容），供消息内文件链接判断是否可点击 */
  projectStat: (id: number, path: string) =>
    get<{ exists: boolean; is_dir: boolean }>(`/projects/${id}/stat?path=${encodeURIComponent(path)}`),
  /** 问题13: 按路径子串搜索项目内全部文件（@ 引用文件补全） */
  projectFileSearch: (id: number, q: string, limit = 100) =>
    get<string[]>(`/projects/${id}/files/search?q=${encodeURIComponent(q)}&limit=${limit}`),
  readProjectFile: (id: number, path: string) =>
    get<{ path: string; content: string; size: number; truncated: boolean; language: string | null }>(
      `/projects/${id}/read-file?path=${encodeURIComponent(path)}`,
    ),

  // ── 会话 ──
  listSessions: (projectId?: number, includeArchived?: boolean) => {
    const q = [projectId != null ? `project_id=${projectId}` : "", includeArchived ? "include_archived=true" : ""]
      .filter(Boolean).join("&");
    return get<SessionOut[]>(`/sessions${q ? `?${q}` : ""}`);
  },
  createSession: (data: { project_id: number; title?: string; model_id?: number; permission_mode?: string; approval_mode?: string; goal_text?: string }) =>
    post<SessionOut>("/sessions", data),
  getSession: (id: number) => get<SessionOut>(`/sessions/${id}`),
  /** 从主会话开一个侧边会话（子会话）：沿用主会话的项目、模型与模式，之后走同一条 turn 流水线。 */
  createSideSession: (parentId: number) => post<SessionOut>(`/sessions/${parentId}/side-sessions`),
  listSideSessions: (parentId: number) => get<SessionOut[]>(`/sessions/${parentId}/side-sessions`),
  /** plan-156-619 S5：创建**执行子会话**（带任务，创建即开工）。子会话派生出来仍是同级。 */
  createSubsession: (parentId: number, data: { title: string; task: string; context?: string; mode?: string }) =>
    post<SessionOut>(`/sessions/${parentId}/subsessions`, data),
  /** plan-156-619 S5：主会话下全部子会话——左栏子会话行的唯一数据源（list_sessions 会过滤掉子会话）。 */
  listSubsessions: (parentId: number) => get<SessionOut[]>(`/sessions/${parentId}/subsessions`),
  /** plan-156-620：全部未归档执行子会话（一次请求，前端按 side_parent_id 分桶）。
   *  替代「每个主会话各请求一次」——上百个并发请求曾拖慢所有接口。 */
  listSubsessionIndex: () => get<SessionOut[]>(`/sessions/subsession-index`),
  /** plan-156-619 S10：取会话当前待处理的后续任务建议（刷新后重建卡片）。 */
  getLatestSuggestion: (sessionId: number) =>
    get<{ suggestion: TaskSuggestionOut | null }>(`/sessions/${sessionId}/suggestions/latest`),
  /** 接受建议 → 新建子会话执行（返回新子会话 id）。 */
  acceptSuggestion: (suggestionId: number) =>
    post<{ subsession_id: number; session_id: number }>(`/suggestions/${suggestionId}/accept`),
  /** 取消建议 → 永久不再展示。 */
  dismissSuggestion: (suggestionId: number) =>
    post<{ ok: boolean }>(`/suggestions/${suggestionId}/dismiss`),
  updateSession: (id: number, data: { title?: string; model_id?: number; pinned?: boolean; status?: string; permission_mode?: string; approval_mode?: string }) =>
    patch<SessionOut>(`/sessions/${id}`, data),
  /** 删除会话。默认语义 = 归档（可恢复）；permanent=true 走物理删除（plan-282-1441 #4） */
  deleteSession: (id: number, permanent = false) =>
    del<{ ok: boolean }>(`/sessions/${id}${permanent ? "?permanent=true" : ""}`),
  /** plan-20261014-fix4：归档页批量永久删除——项目（含工作树子项目与其下全部会话）与会话一次请求完成；
   *  已不存在的 id 计入 missing 而不报错。POST 自带 10 分钟超时，足够覆盖删除后的空间回收。 */
  purgeArchived: (data: { project_ids: number[]; session_ids: number[] }) =>
    post<ArchivePurgeResult>("/archive/purge", data),
  // ── 目标模式（plan-671）──
  getSessionGoal: (id: number) => get<GoalOut>(`/sessions/${id}/goal`),
  setSessionGoal: (id: number, text: string) => post<GoalOut>(`/sessions/${id}/goal`, { text }),
  cancelSessionGoal: (id: number) => del<GoalOut>(`/sessions/${id}/goal`),
  /** 用户确认目标完成（goal_complete 工具的同义用户路径） */
  completeSessionGoal: (id: number) => del<GoalOut>(`/sessions/${id}/goal?complete=true`),
  forkSession: (id: number) => post<SessionOut>(`/sessions/${id}/fork`),
  /** 停止按钮：中断本会话的一切执行（主代理及子代理、后台子代理、运行中的工作流——暂停保留进度） */
  stopSession: (id: number) => post<{ ok: boolean; turns: number; agent_managers: number; workflows_paused: number; wakeup_cancelled?: number }>(`/sessions/${id}/stop`),
  /** plan-20261020-bgtask：会话名下仍在运行的后台命令（右上角「后台任务」栏的数据源） */
  listBgProcesses: (sessionId: number) => get<{ items: BgProcessOut[] }>(`/sessions/${sessionId}/bg-processes`),
  /** 用户停止一个后台命令：终止进程树，并由服务端把「被用户停止」通知给会话 */
  stopBgProcess: (sessionId: number, shellId: string) =>
    post<{ ok: boolean; stopped: boolean; returncode: number | null }>(
      `/sessions/${sessionId}/bg-processes/${encodeURIComponent(shellId)}/stop`,
    ),
  renameSession: (id: number, title: string) =>
    post<SessionOut>(`/sessions/${id}/rename?title=${encodeURIComponent(title)}`),
  createWorktree: (id: number, branch?: string) =>
    post<{ ok: boolean; path: string; branch: string | null }>(
      `/sessions/${id}/worktree${branch ? `?branch=${encodeURIComponent(branch)}` : ""}`),
  removeWorktree: (id: number) => del<{ ok: boolean }>(`/sessions/${id}/worktree`),

  // ── 轮次（turn）──
  createTurn: (body: TurnCreateBody) => post<TurnOut>("/turns", body),
  /** plan-547: 向运行中的 turn 注入用户消息（下次 LLM 调用前传达，不新开 turn） */
  injectTurnInput: (turnId: number, body: { request_id?: string; content: string; attachments?: Record<string, unknown>[]; refs?: ComposerRefOut[] }) =>
    post<{ ok: boolean; queued: boolean; error?: string }>(`/turns/${turnId}/inputs`, body),
  listTurns: (sessionId: number) => get<TurnOut[]>(`/turns/sessions/${sessionId}`),
  cancelTurn: (turnId: number) => postCancel<{ ok: boolean }>(`/turns/${turnId}/cancel?source=main`),
  resumeTurn: (turnId: number) => post<TurnOut>(`/turns/${turnId}/resume`),
  rollbackTurn: (turnId: number, params: RollbackParams = {}) => {
    const qs = new URLSearchParams();
    if (params.restore_to_composer === false) qs.set("restore_to_composer", "false");
    if (params.allow_partial) qs.set("allow_partial", "true");
    const q = qs.toString() ? `?${qs.toString()}` : "";
    return post<RollbackResult>(`/turns/${turnId}/rollback${q}`);
  },
  rollbackPreview: (turnId: number) => get<RollbackPreviewOut>(`/turns/${turnId}/rollback_preview`),
  // ── Git（plan-20261017-goal12 项 5）──
  // plan-20261019-git-multirepo：scope 指定操作哪个仓库（相对项目根，空 = 根仓库）与会话（会话工作树优先）
  /** 项目内的全部仓库（根仓库在前），带分支与改动数 */
  gitRepos: (projectId: number, scope: GitScope = {}) =>
    get<GitRepoOut[]>(`/git/repos?${gitQs(projectId, { sessionId: scope.sessionId })}`),
  /** 汇总全部仓库的状态（路径带仓库前缀），供工具卡按项目展示改动 */
  gitStatusAll: (projectId: number, scope: GitScope = {}) =>
    get<GitStatusOut>(`/git/status-all?${gitQs(projectId, { sessionId: scope.sessionId })}`),
  /** plan-20261019-toolcard-v2: 全部仓库未提交改动的增删行数合计（含未跟踪文本文件），供工具卡「更改 +N −M」 */
  gitDiffStat: (projectId: number, scope: GitScope = {}) =>
    get<GitDiffStatOut>(`/git/diff-stat?${gitQs(projectId, { sessionId: scope.sessionId })}`),
  gitStatus: (projectId: number, scope: GitScope = {}) => get<GitStatusOut>(`/git/status?${gitQs(projectId, scope)}`),
  gitDiff: (projectId: number, path: string, staged = false, scope: GitScope = {}) =>
    get<GitDiffOut>(`/git/diff?${gitQs(projectId, scope)}&path=${encodeURIComponent(path)}&staged=${staged ? "true" : "false"}`),
  gitLog: (projectId: number, limit = 40, skip = 0, scope: GitScope = {}) =>
    get<GitLogEntry[]>(`/git/log?${gitQs(projectId, scope)}&limit=${limit}&skip=${skip}`),
  gitCommitDetail: (projectId: number, sha: string, scope: GitScope = {}) =>
    get<GitCommitDetailOut>(`/git/commit?${gitQs(projectId, scope)}&sha=${encodeURIComponent(sha)}`),
  gitCommitDiff: (projectId: number, sha: string, path: string, scope: GitScope = {}) =>
    get<GitDiffOut>(`/git/commit/diff?${gitQs(projectId, scope)}&sha=${encodeURIComponent(sha)}&path=${encodeURIComponent(path)}`),
  gitBranches: (projectId: number, scope: GitScope = {}) => get<GitBranchesOut>(`/git/branches?${gitQs(projectId, scope)}`),
  gitStage: (projectId: number, paths: string[], scope: GitScope = {}) =>
    post<{ ok: boolean }>("/git/stage", { ...gitBody(projectId, scope), paths }),
  gitUnstage: (projectId: number, paths: string[], scope: GitScope = {}) =>
    post<{ ok: boolean }>("/git/unstage", { ...gitBody(projectId, scope), paths }),
  gitDiscard: (projectId: number, paths: string[], untracked: string[], scope: GitScope = {}) =>
    post<{ ok: boolean }>("/git/discard", { ...gitBody(projectId, scope), paths, untracked }),
  gitCommit: (projectId: number, message: string, amend = false, scope: GitScope = {}) =>
    post<{ ok: boolean; sha: string }>("/git/commit", { ...gitBody(projectId, scope), message, amend }),
  // 拉取冲突不抛错：conflict/conflicts/state 交给面板直接进入冲突处理
  gitSync: (projectId: number, action: "fetch" | "pull" | "push", scope: GitScope = {}) =>
    post<{ ok: boolean; output: string; conflict?: boolean; conflicts?: string[]; state?: string }>(
      "/git/sync", { ...gitBody(projectId, scope), action }),
  // 切换分支时可能返回 switched=false + reason=dirty：界面据此让用户选择暂存或放弃改动
  gitBranch: (projectId: number, action: "checkout" | "create" | "delete", name: string,
    opts: { base?: string; force?: boolean; onDirty?: "stash" | "discard" } = {}, scope: GitScope = {}) =>
    post<{
      ok: boolean; output: string;
      switched?: boolean; stashed?: boolean; created?: boolean; deleted?: boolean;
      reason?: "dirty"; files?: string[];
      conflict?: boolean; conflicts?: string[]; state?: string;
    }>("/git/branch", {
      ...gitBody(projectId, scope), action, name,
      base: opts.base, force: opts.force, on_dirty: opts.onDirty,
    }),
  gitResolve: (projectId: number, path: string, side: "ours" | "theirs", scope: GitScope = {}) =>
    post<{ ok: boolean }>("/git/resolve", { ...gitBody(projectId, scope), path, side }),
  gitMergeAbort: (projectId: number, scope: GitScope = {}) =>
    post<{ ok: boolean }>("/git/merge/abort", gitBody(projectId, scope)),
  getTurnSnapshot: (turnId: number) => get<TurnSnapshotOut>(`/turns/${turnId}/snapshot`),

  // ── 变更审核（v11）──
  getTurnChanges: (turnId: number) => get<FileChangeOut[]>(`/turns/${turnId}/changes`),
  /** plan-89-387: 传 callKey 取「这一次编辑」的变更（同轮多次编辑各自独立）；不传为整轮累积 */
  getFileDiff: (turnId: number, path: string, callKey?: string) =>
    get<FileDiffOut>(`/turns/${turnId}/changes/diff?path=${encodeURIComponent(path)}${callKey ? `&call_key=${encodeURIComponent(callKey)}` : ""}`),
  reviewFiles: (turnId: number, paths: string[], reviewed: boolean) =>
    put<{ ok: boolean; updated: number }>(`/turns/${turnId}/reviews`, { paths, reviewed }),
  /** plan-20261018-turn-changes: 仅撤销该轮的文件改动（不删消息、不改 turn 状态）。
   *  后端会强校验「更新的轮次必须先撤销」，乱序请求返回 409 order_violation。 */
  revertTurnFiles: (turnId: number, allowPartial = false) =>
    post<RevertFilesResult>(`/turns/${turnId}/revert_files?allow_partial=${allowPartial ? "true" : "false"}`),
  /** plan-20261018-turn-changes: 撤销顺序锁——该会话当前唯一可撤销的轮次。 */
  getRevertLock: (sessionId: number) =>
    get<RevertLockOut>(`/turns/sessions/${sessionId}/revert_lock`),

  // ── 会话数据查询 ──
  listSessionMessages: (sessionId: number, threadId?: number) =>
    get<MessageOut[]>(`/turns/sessions/${sessionId}/messages${threadId != null ? `?thread_id=${threadId}` : ""}`),
  // v30.1: 压缩块索引 / 原文还原（AI 与前端按索引查看压缩前会话）
  listCompactions: (sessionId: number) =>
    get<CompactionIndexOut[]>(`/sessions/${sessionId}/compactions`),
  getCompactedMessages: (sessionId: number, compactionId: string) =>
    get<MessageOut[]>(`/sessions/${sessionId}/compactions/${encodeURIComponent(compactionId)}/messages`),
  restoreCompaction: (sessionId: number, compactionId: string) =>
    post<{ ok: boolean; restored_messages: number }>(`/sessions/${sessionId}/compactions/${encodeURIComponent(compactionId)}/restore`),
  // v19: 会话子代理列表（消息流卡片重建）
  listSessionSubagents: (sessionId: number) =>
    get<Array<{
      agent_id: number; name: string; turn_id: number | null; task_id: number | null;
      task_title: string | null; status: string;
      /** v36 (plan-321-1600 M2): 面板头部“用时 / 文件数 / 用量”展示字段 */
      created_at?: string | null; updated_at?: string | null;
      token_usage?: number; files_count?: number;
      /** 工作流节点的子代理：节点信息（普通子代理为空） */
      workflow?: WorkflowNodeBrief | null;
      /** 工作流节点的子代理：派发时刻（线程首条消息 id），主消息流据此定位节点卡（普通子代理为空） */
      spawn_message_id?: number | null;
      /** plan-20261019-subagent-ghost: 线程消息数（0 且无任务、非运行中 = 幽灵条目） */
      message_count?: number;
    }>>(`/turns/sessions/${sessionId}/subagents`),
  listSessionTasks: (sessionId: number) => get<TaskOut[]>(`/turns/sessions/${sessionId}/tasks`),
  /** v38 (plan-482): 确认/取消方案文档（不再涉及 group/steps）。 */
  confirmPlanTurn: (turnId: number, data: { accepted: boolean }) =>
    post<{ ok: boolean; permission_mode?: string }>(`/turns/${turnId}/plan/confirm`, data),
  retryTask: (turnId: number, taskId: number) =>
    post<{ ok: boolean }>(`/turns/${turnId}/tasks/${taskId}/retry`, {}),
  listSessionArtifacts: (sessionId: number) => get<ArtifactOut[]>(`/turns/sessions/${sessionId}/artifacts`),
  /** 摘要面板「产物」：本会话写过且现在仍存在的文件（与变更卡片同一口径，删除的不算） */
  listSessionProducts: (sessionId: number) => get<{ files: string[] }>(`/turns/sessions/${sessionId}/products`),
  listSessionSnapshots: (sessionId: number) => get<TurnSnapshotOut[]>(`/turns/sessions/${sessionId}/snapshots`),
  listSessionAudit: (sessionId: number) => get<AuditLogOut[]>(`/turns/sessions/${sessionId}/audit`),
  getSessionUsage: (sessionId: number) => get<{
    total: number; context_window: number; message_count: number;
    input: number; cached_input: number; output: number; reasoning_output: number;
    agent_name: string;
    source?: string;  // v1.1: api_last=最后一次 API 真实占用 / est=本地估算
  }>(`/turns/sessions/${sessionId}/usage`),
  getUsageStats: (params?: { start?: string; end?: string; days?: number; provider?: string }) => {
    const q = new URLSearchParams();
    if (params?.start) q.set("start", params.start);
    if (params?.end) q.set("end", params.end);
    if (params?.days) q.set("days", String(params.days));
    // plan-308-1542 需求6：按供应商筛选
    if (params?.provider) q.set("provider", params.provider);
    const qs = q.toString();
    return get<UsageStatsOut>(`/usage/stats${qs ? `?${qs}` : ""}`);
  },
  /** plan-20261021-usage-update：调用记录（分页，默认每页 20 条），区间 / 供应商口径同 getUsageStats */
  getUsageRecords: (params?: { start?: string; end?: string; days?: number; provider?: string; page?: number; page_size?: number }) => {
    const q = new URLSearchParams();
    if (params?.start) q.set("start", params.start);
    if (params?.end) q.set("end", params.end);
    if (params?.days) q.set("days", String(params.days));
    if (params?.provider) q.set("provider", params.provider);
    if (params?.page) q.set("page", String(params.page));
    if (params?.page_size) q.set("page_size", String(params.page_size));
    const qs = q.toString();
    return get<UsageRecordsOut>(`/usage/records${qs ? `?${qs}` : ""}`);
  },

  // ── 定时任务（plan-230-1144 M1.1：调度器落地）──
  listScheduledTasks: () => get<ScheduledTaskOut[]>("/scheduled-tasks"),
  createScheduledTask: (data: { session_id: number; name: string; cron: string; prompt: string; missed_policy?: ScheduledMissedPolicy }) =>
    post<ScheduledTaskOut>("/scheduled-tasks", data),
  updateScheduledTask: (id: number, data: { name?: string; cron?: string; prompt?: string; enabled?: boolean; missed_policy?: ScheduledMissedPolicy }) =>
    patch<ScheduledTaskOut>(`/scheduled-tasks/${id}`, data),
  deleteScheduledTask: (id: number) => del<{ ok: boolean }>(`/scheduled-tasks/${id}`),
  /** 立即试跑一次（不影响既有排程） */
  runScheduledTask: (id: number) => post<{ ok: boolean; task_id: number; triggered_at: string }>(`/scheduled-tasks/${id}/run`, {}),
  /** 预览接下来 N 次触发时刻 */
  previewScheduledTask: (id: number, count = 5) =>
    get<{ cron: string; next_runs: string[] }>(`/scheduled-tasks/${id}/preview?count=${count}`),
  /** 表单实时校验 cron */
  validateCron: (cron: string) =>
    get<CronValidateOut>(`/scheduled-tasks/meta/validate?cron=${encodeURIComponent(cron)}`),

  // ── 权限模式（plan-230-1144 M2：白名单+提示词外置，支持自定义模式）──
  listPermissionProfiles: () => get<PermissionProfileOut[]>("/permission-profiles"),
  upsertPermissionProfile: (data: {
    name: string; display_name?: string; kind?: string;
    description?: string; tools?: string[]; hint?: string;
  }) => post<PermissionProfileOut>("/permission-profiles", data),
  deletePermissionProfile: (name: string) => del<{ ok: boolean }>(`/permission-profiles/${name}`),

  // ── 配置 profile ──
  listProfiles: (projectId?: number) =>
    get<ConfigProfileOut[]>(`/profiles${projectId ? `?project_id=${projectId}` : ""}`),
  createProfile: (data: { name: string; scope?: string; project_id?: number; data?: Record<string, unknown> }) =>
    post<ConfigProfileOut>("/profiles", data),
  updateProfile: (id: number, data: { data?: Record<string, unknown>; is_active?: boolean }) =>
    patch<ConfigProfileOut>(`/profiles/${id}`, data),
  deleteProfile: (id: number) => del<{ ok: boolean }>(`/profiles/${id}`),
  switchSessionProfile: (sessionId: number, profileId: number) =>
    post<{ ok: boolean; profile_id: number }>(`/profiles/sessions/${sessionId}/profile?profile_id=${profileId}`),

  // ── 执行策略 ──
  listExecPolicyRules: () => get<ExecPolicyRuleOut[]>("/exec-policy"),
  listExecPolicyTools: () => get<ExecPolicyToolInfo[]>("/exec-policy/tools"),
  createExecPolicyRule: (data: { session_id?: number; command_pattern: string; decision: string; justification?: string; tool_name?: string }) =>
    post<ExecPolicyRuleOut>("/exec-policy", data),
  deleteExecPolicyRule: (id: number) => del<{ ok: boolean }>(`/exec-policy/${id}`),

  // ── 钩子 ──
  listHooks: () => get<HookConfigOut[]>("/hooks"),
  /** S12（plan-41-197）：command=执行脚本；prompt=向 AI 注入提示词（二选一） */
  createHook: (data: {
    event: string; command?: string; matcher?: string; enabled?: boolean;
    hook_type?: "command" | "prompt"; prompt?: string;
  }) => post<HookConfigOut>("/hooks", data),
  updateHook: (id: number, data: {
    command?: string; matcher?: string; enabled?: boolean;
    hook_type?: "command" | "prompt"; prompt?: string;
  }) => patch<HookConfigOut>(`/hooks/${id}`, data),
  deleteHook: (id: number) => del<{ ok: boolean }>(`/hooks/${id}`),

  // ── 记忆 ──
  // plan-230-1144 M4.1: 三层化——可按 scope/project 过滤、可提升作用域
  listMemories: (opts?: { sessionId?: number; scope?: string; projectId?: number; includeCandidate?: boolean; q?: string }) => {
    const q = new URLSearchParams();
    if (opts?.q) q.set("q", opts.q);
    if (opts?.sessionId != null) q.set("session_id", String(opts.sessionId));
    if (opts?.scope) q.set("scope", opts.scope);
    if (opts?.projectId != null) q.set("project_id", String(opts.projectId));
    if (opts?.includeCandidate === false) q.set("include_candidate", "false");
    const qs = q.toString();
    return get<MemoryEntryOut[]>(`/memories${qs ? `?${qs}` : ""}`);
  },
  /** plan-memory-fix：各作用域总数（与列表筛选无关） */
  countMemories: (includeCandidate = true) =>
    get<{ all: number; global: number; project: number; session: number }>(
      `/memories/counts${includeCandidate ? "" : "?include_candidate=false"}`),
  deleteMemory: (id: number) => del<{ ok: boolean }>(`/memories/${id}`),
  /** S8（plan-41-197）：编辑记忆文本/类型（编辑即人工确认，候选标记清除） */
  updateMemory: (id: number, data: { text?: string; kind?: string }) =>
    patch<{ ok: boolean }>(`/memories/${id}`, data),
  /** 提升/降级记忆作用域（session ↔ project ↔ global） */
  promoteMemory: (id: number, targetScope: "session" | "project" | "global", projectId?: number) =>
    post<{ ok: boolean }>(`/memories/${id}/promote`, { target_scope: targetScope, project_id: projectId }),
  consolidateMemories: (sessionId: number, projectId: number) =>
    post<{ ok: boolean; path: string; entries: number }>(`/memories/consolidate?session_id=${sessionId}&project_id=${projectId}`),

  // ── 模型 ──
  listModels: () => get<ModelOut[]>("/models"),
  createModel: (data: {
    name: string; provider?: string; provider_id?: number; base_url?: string; intelligence_level?: number;
    context_window?: number; source_type?: string; is_active?: boolean; is_multimodal?: boolean;
    api_format?: string; api_key?: string; reasoning_efforts?: string[];
  }) => post<ModelOut>("/models", data),
  updateModel: (id: number, data: Record<string, unknown>) => patch<ModelOut>(`/models/${id}`, data),
  deleteModel: (id: number) => del<{ ok: boolean }>(`/models/${id}`),

  // ── 供应商 ──
  listProviders: () => get<ProviderOut[]>("/providers"),
  createProvider: (data: { name: string; base_url?: string; api_key?: string; api_format?: string; is_active?: boolean }) =>
    post<ProviderOut>("/providers", data),
  updateProvider: (id: number, data: Record<string, unknown>) => patch<ProviderOut>(`/providers/${id}`, data),
  deleteProvider: (id: number) => del<{ ok: boolean }>(`/providers/${id}`),
  /** plan-41-225: 保存供应商顺序（模型页左列拖拽排序，数组下标即目标顺序） */
  reorderProviders: (ids: number[]) =>
    post<{ ok: boolean; updated: number }>("/providers/reorder", { ids }),
  scanProviderModels: (id: number) => post<{ models: ScannedModel[] }>(`/providers/${id}/scan`, {}),
  listProviderModels: (id: number) => get<ModelOut[]>(`/providers/${id}/models`),
  bulkSaveProviderModels: (id: number, models: Array<{
    name: string; is_active?: boolean; context_window?: number; is_multimodal?: boolean; reasoning_efforts?: string[];
  }>) => post<ModelOut[]>(`/providers/${id}/models`, { models }),

  // ── plan-248-1258 M2.2/M2.3: 供应商凭据（多 Key/多账号）与代理 ──
  listProviderCredentials: (id: number) => get<ProviderCredentialOut[]>(`/providers/${id}/credentials`),
  createProviderCredential: (id: number, data: {
    label?: string; api_key?: string; token_ref?: string; priority?: number; is_active?: boolean;
  }) => post<ProviderCredentialOut>(`/providers/${id}/credentials`, data),
  updateProviderCredential: (credentialId: number, data: Record<string, unknown>) =>
    patch<ProviderCredentialOut>(`/credentials/${credentialId}`, data),
  deleteProviderCredential: (credentialId: number) => del<{ ok: boolean }>(`/credentials/${credentialId}`),
  testProviderProxy: (id: number) =>
    post<{ ok: boolean; latency_ms: number; proxy: string | null; error: string | null }>(`/providers/${id}/proxy-test`, {}),

  // ── ta3（Ta+3 牛码）供应商（v23）──
  ta3LoginStart: (id: number) => post<{
    status: string; authorize_url?: string; state?: string; port?: number; expires_in?: number;
    account?: Record<string, unknown> | null;
  }>(`/providers/${id}/ta3/login/start`, {}),
  ta3LoginCancel: (id: number) => post<{ ok: boolean }>(`/providers/${id}/ta3/login/cancel`, {}),
  ta3LoginStatus: (id: number) => get<{ status: string; account?: Record<string, unknown> | null; error?: string | null }>(`/providers/${id}/ta3/login/status`),
  ta3Logout: (id: number) => post<{ ok: boolean }>(`/providers/${id}/ta3/logout`, {}),
  ta3Sync: (id: number) => post<{ synced: number; models: Array<{ name: string }> }>(`/providers/${id}/ta3/sync`, {}),
  // ── ta3 额度与用量 / 模型状态 / 后台网页（plan-270-1358，对齐 Ta+3 v0.4.6 quotaService）──
  ta3Quota: (id: number, opts?: { force?: boolean }) =>
    get<Ta3QuotaOut>(`/providers/${id}/ta3/quota${opts?.force ? "?force=true" : ""}`),
  ta3QuotaTrend: (id: number, opts?: {
    period?: "DAILY" | "MONTHLY"; startDate?: string; endDate?: string; callSource?: string;
  }) => {
    const qs = new URLSearchParams();
    if (opts?.period) qs.set("period", opts.period);
    if (opts?.startDate) qs.set("start_date", opts.startDate);
    if (opts?.endDate) qs.set("end_date", opts.endDate);
    if (opts?.callSource) qs.set("call_source", opts.callSource);
    const q = qs.toString();
    return get<Ta3QuotaTrendOut>(`/providers/${id}/ta3/quota/trend${q ? `?${q}` : ""}`);
  },
  ta3QuotaOverdraft: (id: number, remark = "") =>
    post<Ta3QuotaActionResult>(`/providers/${id}/ta3/quota/overdraft`, { remark }),
  ta3QuotaReset: (id: number, windowType: "WEEKLY" | "MONTHLY", remark = "") =>
    post<Ta3QuotaActionResult>(`/providers/${id}/ta3/quota/reset`, { window_type: windowType, remark }),
  ta3ModelStatus: (id: number, opts?: { model?: string; protocol?: "OPENAI" | "ANTHROPIC"; force?: boolean }) => {
    const qs = new URLSearchParams();
    if (opts?.model) qs.set("model", opts.model);
    if (opts?.protocol) qs.set("protocol", opts.protocol);
    if (opts?.force) qs.set("force", "true");
    const q = qs.toString();
    return get<Ta3ModelStatusOut>(`/providers/${id}/ta3/model-status${q ? `?${q}` : ""}`);
  },
  ta3AdminWeb: (id: number) => post<{ ok: boolean; url: string }>(`/providers/${id}/ta3/admin-web`, {}),

  // ── kiro（AWS Kiro）供应商（v26）：账号登录型，导入本机 Kiro 登录态 ──
  kiroLoginStart: (id: number) => post<{
    status: string; account?: Record<string, unknown> | null;
    region?: string | null; error?: string | null;
  }>(`/providers/${id}/kiro/login/start`, {}),
  kiroLoginCancel: (id: number) => post<{ ok: boolean }>(`/providers/${id}/kiro/login/cancel`, {}),
  kiroLoginStatus: (id: number) => get<{
    status: string; account?: Record<string, unknown> | null;
    region?: string | null; error?: string | null;
  }>(`/providers/${id}/kiro/login/status`),
  kiroLogout: (id: number) => post<{ ok: boolean }>(`/providers/${id}/kiro/logout`, {}),
  kiroSync: (id: number) =>
    post<{ synced: number; models: Array<{ name: string; model_id: string }> }>(`/providers/${id}/kiro/sync`, {}),
  /** 剩余积分（GetUsageLimits）；force=true 跳过 10s 缓存 */
  kiroQuota: (id: number, opts?: { force?: boolean }) =>
    get<KiroQuotaOut>(`/providers/${id}/kiro/quota${opts?.force ? "?force=true" : ""}`),

  // ── workbuddy（腾讯 CodeBuddy/WorkBuddy）供应商（v24）──
  // plan-271-1364: 每次 loginStart 视为「登录一个新账号」，成功后新建凭据行
  workbuddyLoginStart: (id: number) => post<{
    status: string; auth_url?: string; state?: string; expires_in?: number;
    account?: Record<string, unknown> | null;
    credential_id?: number | null; account_label?: string | null;
  }>(`/providers/${id}/workbuddy/login/start`, {}),
  workbuddyLoginCancel: (id: number) => post<{ ok: boolean }>(`/providers/${id}/workbuddy/login/cancel`, {}),
  workbuddyLoginStatus: (id: number) => get<{
    status: string; account?: Record<string, unknown> | null; error?: string | null;
    credential_id?: number | null; account_label?: string | null;
  }>(`/providers/${id}/workbuddy/login/status`),
  /** credentialId 给定 = 只退该账号；不传 = 退出全部账号（兼容旧行为） */
  workbuddyLogout: (id: number, credentialId?: number) =>
    post<{ ok: boolean }>(`/providers/${id}/workbuddy/logout${credentialId != null ? `?credential_id=${credentialId}` : ""}`, {}),
  workbuddySync: (id: number, credentialId?: number) =>
    post<{ synced: number; models: Array<{ name: string }> }>(`/providers/${id}/workbuddy/sync${credentialId != null ? `?credential_id=${credentialId}` : ""}`, {}),
  // plan-248-1258 M2.6: 积分余额查询与 Buddy 加油站签到
  workbuddyCredits: (id: number, opts?: { credentialId?: number; refresh?: boolean }) => {
    const qs = new URLSearchParams();
    if (opts?.credentialId != null) qs.set("credential_id", String(opts.credentialId));
    if (opts?.refresh) qs.set("refresh", "true");
    const q = qs.toString();
    return get<{ credentials: Array<{
      credential_id: number | null; label: string | null; credits: number | null;
      account: Record<string, unknown> | null; logged_in: boolean;
    }> }>(`/providers/${id}/workbuddy/credits${q ? `?${q}` : ""}`);
  },
  workbuddyCheckin: (id: number, credentialId?: number) =>
    post<{ results: Array<{ credential_id: number | null; label?: string | null; status: string; credit?: number | null; credits?: number | null; message?: string | null; error?: string | null }> }>(
      `/providers/${id}/workbuddy/checkin${credentialId != null ? `?credential_id=${credentialId}` : ""}`, {}),

  // ── trae（TRAE SOLO CN）供应商（v25）──
  traeLoginStart: (id: number) => post<{
    status: string; authorize_url?: string; state?: string; port?: number; expires_in?: number;
    account?: Record<string, unknown> | null;
  }>(`/providers/${id}/trae/login/start`, {}),
  traeLoginCancel: (id: number) => post<{ ok: boolean }>(`/providers/${id}/trae/login/cancel`, {}),
  traeLoginStatus: (id: number) => get<{ status: string; account?: Record<string, unknown> | null; error?: string | null }>(`/providers/${id}/trae/login/status`),
  traeLogout: (id: number) => post<{ ok: boolean }>(`/providers/${id}/trae/logout`, {}),
  traeSync: (id: number) => post<{ synced: number; models: Array<{ name: string }> }>(`/providers/${id}/trae/sync`, {}),

  // ── 诊断 ──
  runDiagnostics: () => get<DiagnosticsOut>("/diagnostics"),
  updateCheck: () => get<UpdateCheckOut>("/update-check"),
  /** plan-88: 手动触发 checkpoint 垃圾回收（不传 workspace=全部活跃项目） */
  cleanupCheckpoints: (workspace?: string) =>
    post<{ ok: boolean; results: Array<Record<string, unknown>> }>(
      `/diagnostics/checkpoints/cleanup${workspace ? `?workspace=${encodeURIComponent(workspace)}` : ""}`,
    ),
  // ── plan-230-1144 M3: 符号索引状态 / 手动重建 ──
  /** 单工作区索引摘要。注：设置页已不再展示索引摘要（统一收敛到「索引库」），
   * 本封装保留供诊断/排障脚本等调用；界面请用 symbolIndexWorkspaces。 */
  symbolIndexStatus: (workspace?: string) =>
    get<{ ok: boolean; workspace?: string; available?: boolean; files?: number; symbols?: number; last_updated?: number | null; error?: string }>(
      `/diagnostics/symbol-index${workspace ? `?workspace=${encodeURIComponent(workspace)}` : ""}`,
    ),
  symbolIndexRebuild: (workspace?: string) =>
    post<{ ok: boolean; workspace?: string; submitted?: boolean; status?: string; message?: string; error?: string }>(
      `/diagnostics/symbol-index/rebuild${workspace ? `?workspace=${encodeURIComponent(workspace)}` : ""}`,
      {},
    ),

  // ── plan-248-1258 M3.2/M3.4: 代码符号索引（每工作区开关 + 索引库页面）──
  symbolIndexWorkspaces: () =>
    get<{ workspaces: Array<{
      workspace: string; name: string; enabled: boolean; status: string;
      files: number; symbols: number; last_updated: number | null;
      progress: number; error: string | null; exists?: boolean;
      /** 进行中的实时计数（仅索引中非 0） */
      files_scanned?: number; files_total?: number;
    }> }>("/symbol-index/workspaces"),
  symbolIndexEnable: (workspace: string) =>
    post<{ error?: string | null }>("/symbol-index/enable", { workspace }),
  symbolIndexDisable: (workspace: string) =>
    post<{ error?: string | null }>("/symbol-index/disable", { workspace }),
  symbolIndexSearch: (workspace: string, query: string, kind?: string) =>
    get<{ hits: Array<{
      file_path: string; kind: string; name: string; qualified_name?: string | null;
      line_start: number; line_end: number; signature?: string | null;
    }> }>(`/symbol-index/search?workspace=${encodeURIComponent(workspace)}&query=${encodeURIComponent(query)}${kind ? `&kind=${encodeURIComponent(kind)}` : ""}`),

  // ── plan-20261009-ui U1: 选择本机文件夹（桌面版走 Electron 选择器；浏览器环境由本机后端代为弹出系统选择窗口） ──
  /** 让本机后端弹出系统自带的「选择文件夹」窗口（阻塞到用户选完 / 取消）；
   *  unsupported=true 表示当前环境弹不出（后端不在本机 / 无桌面会话），调用方回落到 browseDirs 的自绘浏览器 */
  pickDirectory: (initial?: string) =>
    post<{ path: string | null; canceled: boolean; unsupported: boolean; reason: string }>(
      "/utils/pick-directory",
      { title: "选择文件夹", initial: initial || undefined },
    ),
  /** 自绘目录浏览器的数据源（仅 pickDirectory 不可用时使用） */
  browseDirs: (path = "", showHidden = false) =>
    get<{
      path: string;
      /** 上一级；"" = 回到根（盘符 / 此电脑）；null = 当前就是根 */
      parent: string | null;
      crumbs: Array<{ name: string; path: string }>;
      entries: Array<{ name: string; path: string }>;
      places: Array<{ name: string; path: string }>;
      truncated: boolean;
    }>(`/utils/browse-dirs?path=${encodeURIComponent(path)}${showHidden ? "&show_hidden=true" : ""}`),
  /** 浏览器拖入文件夹时读不到绝对路径：按名称 + 子项在本机常见位置定位 */
  resolveDir: (name: string, children: string[]) =>
    get<{ candidates: Array<{ name: string; path: string; score: number }> }>(
      `/utils/resolve-dir?name=${encodeURIComponent(name)}&children=${encodeURIComponent(children.slice(0, 80).join(","))}`,
    ),
  /** plan-20261018-turn-changes: 网页调试环境下用本机后端代开文件 / 目录
   *  （桌面版走 Electron IPC；后端只接受本机页面调用）。 */
  openPathOnHost: (path: string, target: "explorer" | "vscode" | "idea" | "terminal" = "explorer") =>
    post<{ ok: boolean; error?: string }>("/utils/open-path", { path, target }),

  // ── 技能 ──
  listSkills: (source?: string) =>
    get<SkillOut[]>(`/skills${source ? `?source=${source}` : ""}`),
  createSkill: (data: {
    name: string; display_name?: string; description?: string; content?: string;
    source?: string; trigger?: string; tools?: string[]; tags?: string[]; auto_load?: boolean;
  }) => post<SkillOut>("/skills", data),
  updateSkill: (id: number, data: {
    display_name?: string; description?: string; content?: string; trigger?: string;
    tools?: string[]; tags?: string[]; is_active?: boolean; auto_load?: boolean;
  }) => patch<SkillOut>(`/skills/${id}`, data),
  deleteSkill: (id: number) => del<{ ok: boolean }>(`/skills/${id}`),
  // plan-230-1144 M1.3: 手动刷新 MCP 工具清单（握手失败抛 400，前端展示健康状态）
  // plan-234-1171 R1: workspace 传项目工作区根——codegraph 依赖 rootUri 定位项目，
  // 且 args 中的 ${workspaceFolder} 需在服务端 spawn 前替换。
  refreshMcpTools: (id: number, workspace?: string) =>
    post<{ ok: boolean; count: number; server: McpServerOut }>(
      `/mcp-servers/${id}/refresh-tools${workspace ? `?workspace=${encodeURIComponent(workspace)}` : ""}`,
      {},
    ),

  // ── MCP ──
  listMcpServers: (source?: string) =>
    get<McpServerOut[]>(`/mcp-servers${source ? `?source=${source}` : ""}`),
  createMcpServer: (data: {
    name: string; display_name?: string; description?: string; source?: string;
    transport?: string; command?: string; args?: string[]; env?: Record<string, string>;
    url?: string; is_active?: boolean; path?: string;
  }) => post<McpServerOut>("/mcp-servers", data),
  updateMcpServer: (id: number, data: {
    display_name?: string; description?: string; transport?: string; command?: string;
    args?: string[]; env?: Record<string, string>; url?: string; is_active?: boolean;
  }, workspace?: string) => patch<McpServerOut>(
    `/mcp-servers/${id}${workspace ? `?workspace=${encodeURIComponent(workspace)}` : ""}`, data,
  ),
 deleteMcpServer: (id: number) => del<{ ok: boolean }>(`/mcp-servers/${id}`),
  scanMcpServers: () => post<Array<{
    name: string; transport: string; command: string | null;
    args: string[]; env: Record<string, string> | null; url: string | null;
    source_path: string;
  }>>("/mcp-servers/scan"),

  // ── 技能仓库（第15点：云端 git url 技能仓库）──
  listSkillRepos: () => get<Array<{ id: string; name: string; url: string; synced?: boolean; skill_count?: number }>>("/skills/repos"),
  createSkillRepo: (data: { url: string; name?: string }) => post<{ id: string; name: string; url: string }>("/skills/repos", data),
  syncSkillRepo: (repoId: string) => post<{ repo: Record<string, unknown>; skills: Array<{ name: string; display_name: string; description: string; path: string; content: string; trigger: string; tools: string[] }> }>(`/skills/repos/${repoId}/sync`),
  importRepoSkill: (repoId: string, skillName: string) => post<{ ok: boolean; id: number }>("/skills/repos/import", { repo_id: repoId, skill_name: skillName }),
  deleteSkillRepo: (repoId: string) => del<{ ok: boolean }>(`/skills/repos/${repoId}`),

  // ── 本地技能导入（v1.1：选择本地目录/md 文件导入技能）──
  importLocalSkill: (data: { path: string; mode?: "copy" | "link" }) =>
    post<{ ok: boolean; imported: string[]; skipped: string[]; count: number }>("/skills/import-local", data),

  /** 自动扫描外部工具（Claude/Codex/CodeBuddy/Qoder/Trae）的技能目录并同步入库。
   *  workspace 缺省时后端回退到最近使用的项目。 */
  scanSkills: (workspace?: string) =>
    post<ScanResult>(`/skills/scan${workspace ? `?workspace=${encodeURIComponent(workspace)}` : ""}`, {}),

  // ── 文件上传（v14: 附件统一为文件地址，不再传 base64）──
  uploadFile: async (file: File): Promise<UploadOut> => {
    const formData = new FormData();
    formData.append("file", file);
    const res = await fetch(`${await baseForFetch()}/upload`, { method: "POST", body: formData });
    if (!res.ok) {
      const detail = await res.text();
      throw new Error(`Upload ${res.status}: ${detail}`);
    }
    return res.json();
  },

  // ── AI 规则（第8点：扫描多软件规则文档并按来源启停）──
  scanAiRules: (path?: string) =>
    get<Array<{ source: string; label: string; path: string; exists: boolean; kind: string }>>(
      `/settings/ai-rules/scan${path ? `?path=${encodeURIComponent(path)}` : ""}`
    ),
  getAiRules: (projectPath?: string) => get<{
    sources: Array<{ source: string; label: string; enabled: boolean }>;
    global_rules: string; workdir_rules: string; project_path: string;
  }>(`/settings/ai-rules${projectPath ? `?project_path=${encodeURIComponent(projectPath)}` : ""}`),
  // plan-234-1171 R7: project_path 决定项目规则归属；写入键与注入读取键同源
  setAiRules: (data: {
    enabled_sources?: string[]; global_rules?: string;
    workdir_rules?: string; project_path?: string;
  }) => put<{
    sources: Array<{ source: string; label: string; enabled: boolean }>;
    global_rules: string; workdir_rules: string; project_path: string;
  }>("/settings/ai-rules", data),

  // ── 全局设置（v2.2: 设置中心持久化统一）──
  getGlobalSettings: () => get<GlobalSettingsOut>("/settings/global"),

  // ── 工作流 ──
  listWorkflows: (sessionId: number) => get<WorkflowRunOut[]>(`/sessions/${sessionId}/workflows`),
  /** “从预设开始”：以预设骨架生成一个待确认的工作流（编辑后再开始） */
  startWorkflowFromPreset: (sessionId: number, preset: string) =>
    post<WorkflowProgramOut>(`/sessions/${sessionId}/workflows/draft`, { preset }),
  getWorkflow: (programId: number) => get<WorkflowProgramOut>(`/workflows/${programId}`),
  workflowEvents: (programId: number, afterSeq = 0) =>
    get<WorkflowEventOut[]>(`/workflows/${programId}/events?after_seq=${afterSeq}`),
  /** 续跑前的工作区漂移预览：暂停期间写入范围内被外部改动的文件 */
  /** 续跑前的工作区报告：改动文件与暂停期间 HEAD 的变化（用户提交过） */
  workflowDrift: (programId: number) =>
    get<WorkflowDriftOut>(`/workflows/${programId}/drift`),
  /** 某个改动文件自暂停以来的差异（统一格式）；reason 说明为什么无法展示差异 */
  workflowDriftDiff: (programId: number, path: string) =>
    get<WorkflowDriftDiffOut>(`/workflows/${programId}/drift/diff?path=${encodeURIComponent(path)}`),
  /** 取消运行；reason=adjusted 表示草稿已按新的要求调整，由新计划替代 */
  cancelWorkflowRun: (programId: number, reason: "user" | "adjusted" = "user") =>
    post<WorkflowProgramOut>(`/workflows/${programId}/cancel`, { reason }),
  /** 草稿计划中把某个子工作流的模板换成另一个模板的子工作流（只允许草稿） */
  swapWorkflowFlowTemplate: (programId: number, flowKey: string, template: string, flow: string) =>
    post<WorkflowProgramOut>(
      `/workflows/${programId}/flows/${encodeURIComponent(flowKey)}/template`,
      { template, flow },
    ),
  /** 新建模板：从一个探索步骤起步，在模板编辑器中补全 */
  createWorkflowTemplate: (name: string, description = "") =>
    post<WorkflowTemplateOut>("/workflows/templates", { name, description }),
  /** 运行中的步骤补充说明：经子代理的指令通道，下一次模型调用前送达 */
  addWorkflowNodeNote: (programId: number, flowKey: string, nodeKey: string, content: string) =>
    post<{ ok: boolean }>(
      `/workflows/${programId}/nodes/${encodeURIComponent(flowKey)}/${encodeURIComponent(nodeKey)}/note`,
      { content },
    ),
  /** 节点的尝试历史（时间、模式、结果、原因） */
  workflowNodeAttempts: (programId: number, flowKey: string, nodeKey: string) =>
    get<WorkflowAttemptOut[]>(
      `/workflows/${programId}/nodes/${encodeURIComponent(flowKey)}/${encodeURIComponent(nodeKey)}/attempts`,
    ),
  workflowAction: (
    programId: number,
    action: "start" | "pause" | "resume" | "cancel",
    note?: string,
    acceptDrift = false,
    acceptLocalChanges = false,
    sharedWorkspace = false,
  ) =>
    post<WorkflowProgramOut>(
      `/workflows/${programId}/${action}`,
      action === "resume"
        ? { note: note ?? "", accept_drift: acceptDrift }
        : action === "start"
          ? { accept_local_changes: acceptLocalChanges, shared_workspace: sharedWorkspace }
          : undefined,
    ),
  workflowPool: () => get<WorkflowPoolOut>("/workflows/pool"),
  listWorkflowTemplates: () => get<WorkflowTemplateOut[]>("/workflows/templates"),
  saveWorkflowTemplate: (programId: number, name: string, description = "") =>
    post<WorkflowTemplateOut>(`/workflows/${programId}/template`, { name, description }),
  copyWorkflowPreset: (key: string) =>
    post<WorkflowTemplateOut>(`/workflows/templates/${encodeURIComponent(key)}/copy`),
  patchWorkflowTemplate: (
    key: string,
    body: { name?: string; description?: string; enabled?: boolean; plan?: Record<string, unknown> },
  ) => patch<WorkflowTemplateOut>(`/workflows/templates/${encodeURIComponent(key)}`, body),
  deleteWorkflowTemplate: (key: string) =>
    del<{ ok: boolean }>(`/workflows/templates/${encodeURIComponent(key)}`),
  decideWorkflowGate: (
    programId: number, flowKey: string, nodeKey: string,
    decision: "approve" | "rework" | "abort" | "include", feedback = "",
  ) =>
    post<WorkflowProgramOut>(
      `/workflows/${programId}/gates/${encodeURIComponent(flowKey)}/${encodeURIComponent(nodeKey)}`,
      { decision, feedback },
    ),
  /** 重试失败的节点：checkpoint 从断点继续，fresh 从头重做该步骤；其它已完成的步骤保持不动 */
  retryWorkflowNode: (
    programId: number, flowKey: string, nodeKey: string, mode: "checkpoint" | "fresh",
  ) =>
    post<WorkflowProgramOut>(
      `/workflows/${programId}/nodes/${encodeURIComponent(flowKey)}/${encodeURIComponent(nodeKey)}/retry`,
      { mode },
    ),
  setGlobalSettings: (data: Partial<GlobalSettingsIn>) => put<GlobalSettingsOut>("/settings/global", data),
  /** 单会话并发上限与当前占用（只读内存快照） */
  getRuntimeLimits: () => get<RuntimeLimitsOut>("/runtime/limits"),
  /** 问题7/8: 终端 Shell 存在性探测 + 候选等宽字体 */
  getTerminalOptions: () => get<TerminalOptionsOut>("/settings/terminal/options"),

  // ── 电脑操控（plan-334-1661）──
  getDesktopStatus: () => get<DesktopStatus>("/desktop/status"),
  /** 主动拉起内核并取回环境信息（设置页「立即检测」） */
  probeDesktop: () => post<{ ok: boolean; info: Record<string, unknown>; calib?: Record<string, unknown> }>("/desktop/probe", {}),
  listDesktopRecipes: (keyword = "") =>
    get<{ count: number; items: DesktopRecipe[] }>(
      `/desktop/recipes${keyword ? `?keyword=${encodeURIComponent(keyword)}` : ""}`,
    ),
  createDesktopRecipe: (data: {
    app_name: string; intent: string; principle: string; pitfalls?: string; scope?: RecipeScope;
  }) => post<{ created: boolean; item: DesktopRecipe }>("/desktop/recipes", data),
  updateDesktopRecipe: (
    id: number,
    data: { app_name?: string; intent?: string; principle?: string; pitfalls?: string; scope?: RecipeScope },
  ) => put<{ item: DesktopRecipe }>(`/desktop/recipes/${id}`, data),
  deleteDesktopRecipes: (ids: number[]) =>
    post<{ deleted: number }>("/desktop/recipes/delete", { ids }),
  deleteAllDesktopRecipes: () => del<{ deleted: number }>("/desktop/recipes"),
  exportDesktopRecipes: () => get<{ version: number; count: number; items: DesktopRecipe[] }>(
    "/desktop/recipes/export",
  ),
  importDesktopRecipes: (payload: { items: unknown[] }) =>
    post<{ created: number; updated: number; skipped: number }>("/desktop/recipes/import", payload),

  // ── 子代理类型（v2.2: 对齐 zcode 3.13）──
  listSubagents: () => get<SubagentProfileOut[]>("/subagents"),
  createSubagent: (data: {
    name: string; description?: string | null; tools_whitelist?: string[] | null;
    model_id?: number | null; system_prompt?: string | null; is_active?: boolean;
    /** plan-330-1648 M2: 思考深度档位（null = 跟随会话本轮档位） */
    reasoning_effort?: string | null;
  }) => post<SubagentProfileOut>("/subagents", data),
  updateSubagent: (id: number, data: {
    name?: string; description?: string | null; tools_whitelist?: string[] | null;
    model_id?: number | null; system_prompt?: string | null; is_active?: boolean;
    reasoning_effort?: string | null;
  }) => patch<SubagentProfileOut>(`/subagents/${id}`, data),
  deleteSubagent: (id: number) => del<{ ok: boolean }>(`/subagents/${id}`),
};

export type {
  ProjectOut, SessionOut, TurnOut, MessageOut, TaskOut, ArtifactOut, ModelOut,
  ProviderOut, ProviderCredentialOut, ScannedModel,
  WorktreeOut, WorktreeRepoOut, WorktreeMergeFile, WorktreeMergePreview, WorktreeMergeDirection, MergeReportOut, PluginMarketItem,
  DbConnectionOut, DbPolicyOut, DebugStatusOut, RepoCandidate,
  DebugSettingsOut,
  ArthasStatusOut, ArthasEntryOut, ArthasProcessOut, ArthasConfigOut,
  ExecPolicyRuleOut, HookConfigOut, MemoryEntryOut, ScheduledTaskOut,
  RollbackPreviewFile, RollbackAffected, RollbackConflict, RollbackPreviewOut, FileChangeOut, FileDiffOut,
};

/** plan-41-198：市场条目类型与三种来源：
 *  - scan     本机扫描到、尚未安装 → 可直接一键安装（本地目录安装）；
 *  - market   仅在内置精选目录中 → 前往市场获取后走导入/安装通道；
 *  - installed 已安装 → 进入管理（启停/卸载）。 */
export type MarketKind = "plugin" | "skill" | "connector";

export interface MarketItem {
  id: string;
  kind: MarketKind;
  name: string;
  displayName: string;
  /** 中文说明（优先展示） */
  descriptionZh?: string;
  /** 原始说明（多为英文，作为补充） */
  description?: string;
  category: string;
  tags?: string[];
  author?: string;
  downloads?: number | null;
  version?: string | null;
  featured?: boolean;
  installKind: "remote" | "scan" | "market" | "installed";
  installed: boolean;
  enabled: boolean;
  source?: string;
  path?: string;
  skills?: string[];
  hasMcp?: boolean;
  /** plan-284-1450：远端原始分类 code（界面显示中文，筛选仍用 code） */
  categoryCode?: string;
  /** plan-284-1450：远端条目主键（已装比对用） */
  marketId?: string;
  /** plan-284-1451：本机记录 id（技能/连接器才有）——行内启停与卸载用它定位记录 */
  localId?: number;
  /** plan-284-1452：分组标记——builtin 应用内置 / local 本机已安装；缺省为市场条目 */
  group?: "builtin" | "local";
  /** plan-41-225：远端图标地址（仅 https；缺失或加载失败时回退首字母色块） */
  iconUrl?: string;
  /** plan-41-225：安装/获取次数（远面条目） */
  installCount?: number;
  /** plan-41-225：条目归属（official 等） */
  location?: string;
  /** plan-41-225：内容更新时间（远端毫秒时间戳字符串） */
  updatedAt?: string;
}

/** plan-41-225：远端市场分类维度（facets.categories） */
export interface MarketCategoryFacet { code: string; label: string; count: number }

/** plan-41-225：市场浏览响应（远端分页 + 已装状态 + 缓存降级） */
export interface MarketBrowseOut {
  ok: boolean;
  kind: MarketKind;
  items: MarketItem[];
  page: number;
  pageSize: number;
  lastPage: number;
  total: number;
  categories: MarketCategoryFacet[];
  installedCount: number;
  marketUrl: string;
  /** cached=true 命中缓存；degraded=true 表示远端不可达（用了过期缓存或本机兜底） */
  cached?: boolean;
  degraded?: boolean;
}

export interface MarketCatalogOut {
  ok: boolean;
  kind: MarketKind;
  marketUrl: string;
  items: MarketItem[];
  categories: string[];
  installedCount: number;
  localCount?: number;
}

/** v2.2 (对齐 zcode 3.18): 全局设置（设置中心持久化统一，落 config.json） */
export interface GlobalSettingsOut {
  memory_enabled: boolean;
  global_rules: string;
  /** plan-278-1391: 上下文压缩触发阈值（0.50~0.95，默认 0.90）；压缩常开，由此阈值决定何时触发 */
  auto_compact_threshold_ratio?: number;
  language: string;
  /** plan-75-332: 以下五项旧风控已废弃（后端仅保留字段兼容旧 config.json，
   *  不再参与任何判定）——是否需审批统一由会话权限模式裁决。 */
  auto_approve_tools: boolean;
  force_approval_tools: string;
  /** 常规面板补项 */
  terminal_shell: string;
  terminal_font: string;
  http_proxy: string;
  enhanced_search: boolean;
  show_todos: boolean;
  show_reasoning: boolean;
  /** plan-156-619 S2：消息区搜索入口按钮（左上角）。默认关闭；关闭只隐藏入口，Ctrl+F 仍可唤起 */
  show_message_search?: boolean;
  /** plan-156-621：每轮用户消息注入「优先用子会话并行处理」的提示词（默认开启） */
  parallel_subsession_hint?: boolean;
  /** v3.0 (plan-88): 计划模式允许访问工作区外路径（plan-75-332 废弃） */
  plan_mode_allow_outside_access: boolean;
  /** v32 (plan-89): 沙箱模式（plan-75-332 废弃） */
  sandbox_mode: string;
  /** v36 (plan-321-1600 R2): 工作目录外读取自动审批（plan-75-332 废弃） */
  auto_approve_outside_read?: boolean;
  /** plan-75-332: 审批「解释」专用模型 id；null/缺省 = 跟随会话模型 */
  approval_explain_model_id?: number | null;
  /** plan-75-332: 审批「解释」专用思考深度；null/缺省 = 跟随会话思考深度 */
  approval_explain_reasoning_effort?: string | null;
  /** v36 (plan-321-1600 R3): ta3 额度自动透支（报错时查额度，日额度>=100% 自动透支一次） */
  auto_overdraft_on_quota_exceeded?: boolean;
  /** v46: ta3 额度自动重置（报错时查额度，周/月额度用尽则自动提交重置） */
  auto_reset_on_quota_exceeded?: boolean;
  agent_max_steps?: number;
  /** v45: 异常自动重试次数（0 = 不重试） */
  agent_retry_count?: number;
  /** v45: 每次重试前的等待秒数（逗号分隔，默认 10,20,30） */
  agent_retry_intervals?: string;
  /** 采样温度（0–2）：工具轮 / 文本轮，默认 0.3 / 0.7；Claude 系与思考模式下由适配器按规则忽略 */
  agent_tool_temperature?: number;
  agent_text_temperature?: number;
  /** 单次最大输出 token（0 = 不限制；官方直连由适配器按模型兜底） */
  agent_max_output_tokens?: number;
  browser_enabled?: boolean;
  browser_headless?: boolean;
  /** 下一步建议卡开关（默认关闭；开启后智能体 / 工作流模式才注入建议卡工具与提示词） */
  suggestion_enabled?: boolean;
  /** plan-334-1661: 电脑操控总开关（开启后 desktop_* 工具集可用） */
  desktop_enabled?: boolean;
  /** plan-334-1661: 普通电脑操作子开关 */
  desktop_plain_ops_enabled?: boolean;
  /** plan-334-1661: 浏览器操作子开关 */
  desktop_browser_ops_enabled?: boolean;
  /** plan-334-1661: 写操作是否要求目标窗口在前台（安全护栏） */
  desktop_require_foreground?: boolean;
  /** plan-334-1661: 截图 JPEG 画质（10~100） */
  desktop_screenshot_quality?: number;
  /** plan-334-1661: 截图缩放长边像素（0 = 不缩放） */
  desktop_screenshot_max_dim?: number;
  /** plan-334-1661: 操作路线沉淀开关 */
  desktop_recipe_enabled?: boolean;
  /** plan-20261019-desktop-targeting: 电脑操控坐标口径（auto 按模型自动 / pixel 图上像素 / norm1000 0–1000 比例） */
  desktop_coord_mode?: string;
  /** 单会话子代理并发上限（1~30） */
  max_concurrent_subagents?: number;
  /** 单会话子会话并发上限（1~30） */
  max_subsessions_per_parent?: number;
  /** plan-156-619 S4e: 子代理开关（plan-20261020-subsession-switch 起只控制子代理） */
  subagent_enabled?: boolean;
  /** plan-20261020-subsession-switch: 子会话开关（含操控子会话），与子代理独立 */
  subsession_enabled?: boolean;
  /** plan-156-619 S4e: 子代理默认模型 id；0 = 跟随主代理 */
  subagent_model_id?: number;
  /** plan-156-619 S4e: 子代理思考深度档位；空 = 跟随会话档位 */
  subagent_reasoning_effort?: string;
  /** plan-20261018-operator-boost: GUI 操作（电脑操控 / 内置浏览器）交给操控子会话 */
  operator_subsession_enabled?: boolean;
  /** plan-20261018-operator-boost: 操控子会话思考深度（lowest = 模型最低档 / follow = 跟随子会话设置 / 具体档位） */
  operator_reasoning_effort?: string;
  /** plan-156-619 S7: 综合工具卡各栏目的全局默认展开策略（true = 默认全展开）。旧字段，仅作逐栏默认缺省时的回落 */
  tool_card_sections_expanded?: boolean;
  /** plan-20261019-toolcard-v2: 工具卡默认形态（capsule = 右上角悬浮胶囊 / expanded = 展开卡片） */
  tool_card_default_form?: "capsule" | "expanded";
  /** plan-20261019-toolcard-v2: 逐栏默认展开（git / plan / process / subagent / workflow），缺省的栏目回落旧布尔 */
  tool_card_section_defaults?: Partial<Record<"git" | "plan" | "process" | "subagent" | "workflow", boolean>>;
  /** 工作流运行时（设置 → 工作流）：关闭后主代理不再规划新的工作流 */
  workflow_enabled?: boolean;
  /** 所有会话的工作流子代理合计上限（0 = 不限） */
  workflow_max_concurrent_global?: number;
  /** 同一会话内工作流子代理合计上限（0 = 不限） */
  workflow_max_concurrent_per_session?: number;
  /** 单个子工作流内并行节点上限（1~4） */
  workflow_max_parallel_nodes_per_flow?: number;
  /** 模板未指定时的返工上限（0~3） */
  workflow_default_max_rework?: number;
  /** 单次运行的 token 预算（0 = 不限） */
  workflow_token_budget_per_run?: number;
  /** 计划生成后是否需要用户确认才开始（关闭则规划后直接执行） */
  workflow_confirm_before_start?: boolean;
  /** 网络中断暂停后是否自动续跑一次（默认关闭） */
  workflow_auto_resume_on_network?: boolean;
  /** 组装方式：runtime（主代理自主组装）/ presets_only（只允许预设或用户模板） */
  workflow_composition_mode?: "runtime" | "presets_only";
  /** 热点文件（逗号或换行分隔）：规划期并行写入时给出串行化建议 */
  workflow_hot_files?: string;
  /** 评估的确定性检查必须通过（关闭则检查结果只记录，不阻止通过） */
  workflow_require_checks?: boolean;
  /** 返工前确认：评估要求返工时先交给主代理核实、由用户确认后再返工（关闭则自动返工） */
  workflow_rework_confirm?: boolean;
  /** 自动三方合并不重叠的改动（关闭则直接转入重做） */
  workflow_auto_merge?: boolean;
  /** 同一处真冲突：queue 先自动重做、多次失败再决定；ask 每次都由用户决定 */
  workflow_conflict_policy?: "queue" | "ask";
  /** 子工作流未声明隔离方式时的默认值：shared 共享工作区 / worktree 独立工作树 */
  workflow_isolation_default?: "shared" | "worktree";
}

export type GlobalSettingsIn = Partial<Omit<GlobalSettingsOut, never>>;

/** plan-20261020-bgtask：运行中的后台命令；elapsed 为服务端算好的已运行秒数（前端以收到的时刻为基准累加） */
export interface BgProcessOut {
  shell_id: string;
  command: string;
  cwd: string;
  turn_id: number | null;
  started_at: number;
  elapsed: number;
}

/** 单会话并发上限与当前占用（只读内存快照） */
export interface RuntimeLimitsOut {
  max_concurrent_subagents: number;
  max_subsessions_per_parent: number;
  sessions: Array<{ session_id: number; subagents_running: number; subsessions_running: number }>;
}

/** 问题7/8: 终端 Shell 选项（available=当前设备实际存在） */
export interface TerminalShellOption { value: string; label: string; available: boolean; }
/** 问题7/8: 候选等宽字体 */
export interface TerminalFontOption { value: string; label: string; }
export interface TerminalOptionsOut { shells: TerminalShellOption[]; fonts: TerminalFontOption[]; }


/** v3.0 (plan-88): 工具级规则候选清单项（PolicyPanel 工具下拉数据源） */
export interface ExecPolicyToolInfo {
  name: string;
  risk_level: string;
  description: string;
  /** plan-20261019-policy-search：工具类别（core / subagent / mcp），权限模式白名单按类分组 */
  group?: string;
}

/** plan-334-1661: 电脑操控操作路线（应用 + 意图 → 通用原则） */
/** plan-20261018-operator-boost：路线类型——desktop 桌面应用（电脑操控）/ browser 网页（内置浏览器） */
export type RecipeScope = "desktop" | "browser";

export interface DesktopRecipe {
  id: number;
  scope?: RecipeScope;
  app_name: string;
  intent: string;
  /** 通用原则：跨界面版本仍成立的操作知识，是复用时的核心依据 */
  principle: string;
  pitfalls: string;
  steps: Array<Record<string, unknown>>;
  usage_count: number;
  last_used_at: string | null;
  /** agent = AI 沉淀；user = 用户手写或编辑过 */
  source: string;
  created_at: string | null;
  updated_at: string | null;
}

/** plan-334-1661: 电脑操控内核状态 */
export interface DesktopStatus {
  core_found: boolean;
  core_path: string;
  running: boolean;
  info: Record<string, unknown>;
}

/** v2.2 (对齐 zcode 3.13): 子代理类型 */
export interface SubagentProfileOut {
  id: number;
  name: string;
  description: string | null;
  tools_whitelist: string[] | null;
  model_id: number | null;
  system_prompt: string | null;
  /** plan-330-1648 M2: 思考深度档位覆盖（null = 跟随会话） */
  reasoning_effort: string | null;
  is_active: boolean;
}

/** plan-270-1358: ta3 额度与用量（字段对齐 Ta+3 quotaService / quotaView 投影）。 */
export interface Ta3QuotaWindowOut {
  /** 枚举：DAILY / WEEKLY / MONTHLY */
  window?: string;
  key?: string;
  /** 中文展示名（仅渲染） */
  label?: string;
  /** null = 该窗口不限额度（不画进度条） */
  percentUsed?: number | null;
  windowStartEpochSeconds?: number;
  resetEpochSeconds?: number;
  canOverdraft?: boolean;
  /** v46: 服务端权威判定「当前窗口是否可重置」（实测周窗未用尽时返回 false） */
  canReset?: boolean;
  selfResetEnabled?: boolean;
  selfResetUsed?: number;
  selfResetMax?: number | null;
  selfResetRemain?: number | null;
  selfResetHint?: string;
  resetPending?: boolean;
}

export interface Ta3QuotaOut {
  userId?: string;
  plan?: { planName?: string; endEpochSeconds?: number | null } | null;
  concurrency?: { limit?: number | null } | null;
  serverTimeEpochSeconds?: number;
  windows?: Ta3QuotaWindowOut[];
}

export interface Ta3QuotaTrendPointOut {
  date?: string;
  requestCount?: number;
  tokenCount?: number;
  inputTokenCount?: number;
  outputTokenCount?: number;
  cacheTokenCount?: number;
  reasoningTokenCount?: number;
  avgResponseTime?: number | null;
}

export interface Ta3QuotaTrendOut {
  period?: string;
  startDate?: string;
  endDate?: string;
  points?: Ta3QuotaTrendPointOut[];
}

export interface Ta3ModelStatusModelOut {
  model?: string;
  displayName?: string;
  protocols?: string[];
  rate?: { currentMultiplier?: number | null; tier?: string; nextMultiplier?: number | null; nextChangeAt?: string; nextTier?: string } | null;
  load?: { grade?: string; reasons?: string[]; latencyP50Ms?: number | null; requestsLastHour?: number | null } | null;
  prices?: { currency?: string; inputPerMtok?: number | null; outputPerMtok?: number | null } | null;
  /** off_peak_cheaper / busy_defer / unavailable */
  hint?: string;
}

export interface Ta3ModelStatusOut {
  generatedAt?: string;
  cacheSeconds?: number | null;
  models?: Ta3ModelStatusModelOut[];
}

export interface Ta3QuotaActionResult {
  status?: string;
  message?: string;
  [k: string]: unknown;
}

/** v26: kiro 剩余积分额度明细（GetUsageLimits 规范化视图）。 */
export interface KiroQuotaBreakdownOut {
  type?: string;
  used?: number;
  limit?: number;
  remaining?: number;
}

/** v26: kiro 剩余积分（服务端 normalize_usage 输出）。 */
export interface KiroQuotaOut {
  available?: boolean;
  used?: number;
  limit?: number;
  remaining?: number;
  /** 下一次额度重置时刻（ISO 字符串） */
  reset_at?: string | null;
  unit?: string;
  account?: string;
  breakdown?: KiroQuotaBreakdownOut[];
  raw?: Record<string, unknown>;
}

/** v14: 上传文件返回结构（附件统一为文件地址）。 */
export interface UploadOut {
  file_id: string;
  filename: string;
  /** 相对 uploads 根目录的路径，AI 通过 read_attachment 按此读取 */
  path: string;
  /** 静态访问地址（前端预览/下载用），如 /api/uploads/{file_id}/{filename} */
  url: string;
  size: number;
  mime_type: string;
  /** image / text / spreadsheet / document */
  type: string;
}

/** v14: 消息附件结构（发送与持久化统一使用）。 */
export interface AttachmentInfo {
  file_id: string;
  filename: string;
  path: string;
  url: string;
  size: number;
  mime_type: string;
  type: string;
  /** 问题15: 后端回填的服务器绝对路径（uploads_dir 下），复制时优先使用 */
  abs_path?: string;
}

/** 将后端返回的相对 url 转成可直接预览的绝对地址（图片缩略图/新窗口打开）。 */
export function resolveFileUrl(url: string): string {
  if (!url) return url;
  if (/^https?:\/\//.test(url)) return url;
  const baseStr = _directBase ?? BASE;
  const origin = baseStr.startsWith("http") ? new URL(baseStr).origin : window.location.origin;
  return url.startsWith("/") ? `${origin}${url}` : `${origin}/${url}`;
}
