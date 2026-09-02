/** 后端 API 客户端（v2：项目任务驱动架构）。
 *
 * 端点与 server/app/gateway/routers 一一对应，类型从 packages/shared 导入。
 */
import type {
  ArtifactOut,
  CompactionIndexOut,
  ConfigProfileOut,
  ExecPolicyRuleOut,
  FileChangeOut,
  FileDiffOut,
  HookConfigOut,
  MemoryEntryOut,
  MessageOut,
  ModelOut,
  ProjectOut,
  ProviderOut,
  ScannedModel,
  RollbackAffected,
  RollbackPreviewFile,
  RollbackPreviewOut,
  RollbackResult,
  ScheduledTaskOut,
  GoalOut,
  SessionOut,
  TaskOut,
  TurnOut,
  TurnSnapshotOut,
} from "@chatcoder/shared";

/** 后端 API 基址:桌面版直连 127.0.0.1:8000,网页版用相对路径走代理。
 * v6.4: 开发模式直连后端，绕过 vite 代理（Node 18+ Happy Eyeballs IPv6 问题）。
 * v2.1: 打包版端口由主进程探活后透传（getBackendPort），端口冲突自动换空闲端口。 */
const IS_ELECTRON = typeof window !== "undefined" && Boolean((window as Window).chatcoderAPI);
const IS_DEV = import.meta.env.DEV;

/** 桌面版后端端口（主进程透传，Promise 缓存） */
let _backendPortPromise: Promise<number> | null = null;
function backendPort(): Promise<number> {
  if (!_backendPortPromise) {
    const api = (window as Window).chatcoderAPI as { getBackendPort?: () => Promise<number> };
    _backendPortPromise = (api?.getBackendPort?.() ?? Promise.resolve(8000))
      .then((p) => (Number.isFinite(p) && p > 0 ? p : 8000))
      .catch(() => 8000);
  }
  return _backendPortPromise;
}

/** 桌面版/开发模式的 API 基址（异步解析端口） */
async function directBase(): Promise<string> {
  const port = IS_ELECTRON ? await backendPort() : 8000;
  return `http://127.0.0.1:${port}/api`;
}

/** 默认基址：同步兜底（8000），实际请求前会用 directBase 修正 */
const BASE = (IS_ELECTRON || IS_DEV) ? "http://127.0.0.1:8000/api" : "/api";

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

/** 统一错误解析：后端返回 {"error":{code,message}}。 */
function errorMessage(res: Response, raw: string): string {
  try {
    const data = JSON.parse(raw) as { error?: { message?: string; code?: string } };
    if (data?.error?.message) return `${data.error.message}`;
    if (data?.error?.code) return `[${data.error.code}]`;
  } catch { /* 非 JSON */ }
  return `HTTP ${res.status}: ${raw.slice(0, 200)}`;
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
    throw new Error(errorMessage(res, detail));
  }
  return res.json() as Promise<T>;
}

async function get<T>(path: string): Promise<T> {
  const res = await fetchWithRetry(`${await baseForFetch()}${path}`);
  if (!res.ok) {
    const detail = await res.text();
    throw new Error(errorMessage(res, detail));
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
    throw new Error(errorMessage(res, detail));
  }
  return res.json();
}

async function del<T>(path: string): Promise<T> {
  const res = await fetchWithRetry(`${await baseForFetch()}${path}`, { method: "DELETE" });
  if (!res.ok) {
    const detail = await res.text();
    throw new Error(errorMessage(res, detail));
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
    throw new Error(errorMessage(res, detail));
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

export interface TurnCreateBody {
  session_id: number;
  content: string;
  attachments?: Record<string, unknown>[];
  scheduled_task_id?: number;
  reasoning_effort?: string;
  mode?: "readonly" | "plan" | null;
}

export interface RollbackParams {
  restore_to_composer?: boolean;
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
  checks: { name: string; ok: boolean; detail?: string }[];
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
}

export const api = {
  // ── 项目 ──
  listProjects: (params?: { include_archived?: boolean }) =>
    get<ProjectOut[]>(`/projects${params?.include_archived ? "?include_archived=true" : ""}`),
  createProject: (data: { path: string; name?: string; rules_docs?: string[]; auto_scan_rules?: boolean }) =>
    post<ProjectOut>("/projects", data),
  getProject: (id: number) => get<ProjectOut>(`/projects/${id}`),
  updateProject: (id: number, data: { name?: string; rules_docs?: string[]; auto_scan_rules?: boolean; pinned?: boolean; archived?: boolean }) =>
    patch<ProjectOut>(`/projects/${id}`, data),
  deleteProject: (id: number) => del<{ ok: boolean }>(`/projects/${id}`),
  scanProjectRules: (id: number) => get<string[]>(`/projects/${id}/scan-rules`),
  getProjectTree: (id: number, depth = 8) => get<ProjectTreeOut>(`/projects/${id}/tree?depth=${depth}`),
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
  createSession: (data: { project_id: number; title?: string; model_id?: number; permission_mode?: "default" | "accept_edits" | "plan" | "readonly"; goal_text?: string }) =>
    post<SessionOut>("/sessions", data),
  getSession: (id: number) => get<SessionOut>(`/sessions/${id}`),
  updateSession: (id: number, data: { title?: string; model_id?: number; pinned?: boolean; status?: string; permission_mode?: "default" | "accept_edits" | "plan" | "readonly" }) =>
    patch<SessionOut>(`/sessions/${id}`, data),
  /** 删除 = 归档 */
  deleteSession: (id: number) => del<{ ok: boolean }>(`/sessions/${id}`),
  // ── 目标模式（plan-671）──
  getSessionGoal: (id: number) => get<GoalOut>(`/sessions/${id}/goal`),
  setSessionGoal: (id: number, text: string) => post<GoalOut>(`/sessions/${id}/goal`, { text }),
  cancelSessionGoal: (id: number) => del<GoalOut>(`/sessions/${id}/goal`),
  /** 用户确认目标完成（goal_complete 工具的同义用户路径） */
  completeSessionGoal: (id: number) => del<GoalOut>(`/sessions/${id}/goal?complete=true`),
  forkSession: (id: number) => post<SessionOut>(`/sessions/${id}/fork`),
  renameSession: (id: number, title: string) =>
    post<SessionOut>(`/sessions/${id}/rename?title=${encodeURIComponent(title)}`),
  createWorktree: (id: number, branch?: string) =>
    post<{ ok: boolean; path: string; branch: string | null }>(
      `/sessions/${id}/worktree${branch ? `?branch=${encodeURIComponent(branch)}` : ""}`),
  removeWorktree: (id: number) => del<{ ok: boolean }>(`/sessions/${id}/worktree`),

  // ── 轮次（turn）──
  createTurn: (body: TurnCreateBody) => post<TurnOut>("/turns", body),
  /** plan-547: 向运行中的 turn 注入用户消息（下次 LLM 调用前传达，不新开 turn） */
  injectTurnInput: (turnId: number, body: { request_id?: string; content: string; attachments?: Record<string, unknown>[] }) =>
    post<{ ok: boolean; queued: boolean; error?: string }>(`/turns/${turnId}/inputs`, body),
  listTurns: (sessionId: number) => get<TurnOut[]>(`/turns/sessions/${sessionId}`),
  cancelTurn: (turnId: number) => post<{ ok: boolean }>(`/turns/${turnId}/cancel`),
  resumeTurn: (turnId: number) => post<TurnOut>(`/turns/${turnId}/resume`),
  rollbackTurn: (turnId: number, params: RollbackParams = {}) => {
    const q = params.restore_to_composer === false ? "?restore_to_composer=false" : "";
    return post<RollbackResult>(`/turns/${turnId}/rollback${q}`);
  },
  rollbackPreview: (turnId: number) => get<RollbackPreviewOut>(`/turns/${turnId}/rollback_preview`),
  getTurnSnapshot: (turnId: number) => get<TurnSnapshotOut>(`/turns/${turnId}/snapshot`),

  // ── 变更审核（v11）──
  getTurnChanges: (turnId: number) => get<FileChangeOut[]>(`/turns/${turnId}/changes`),
  getFileDiff: (turnId: number, path: string) =>
    get<FileDiffOut>(`/turns/${turnId}/changes/diff?path=${encodeURIComponent(path)}`),
  reviewFiles: (turnId: number, paths: string[], reviewed: boolean) =>
    put<{ ok: boolean; updated: number }>(`/turns/${turnId}/reviews`, { paths, reviewed }),

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
    get<Array<{ agent_id: number; name: string; turn_id: number | null; task_id: number | null; task_title: string | null; status: string }>>(`/turns/sessions/${sessionId}/subagents`),
  listSessionTasks: (sessionId: number) => get<TaskOut[]>(`/turns/sessions/${sessionId}/tasks`),
  /** v38 (plan-482): 确认/取消方案文档（不再涉及 group/steps）。 */
  confirmPlanTurn: (turnId: number, data: { accepted: boolean }) =>
    post<{ ok: boolean; permission_mode?: string }>(`/turns/${turnId}/plan/confirm`, data),
  retryTask: (turnId: number, taskId: number) =>
    post<{ ok: boolean }>(`/turns/${turnId}/tasks/${taskId}/retry`, {}),
  listSessionArtifacts: (sessionId: number) => get<ArtifactOut[]>(`/turns/sessions/${sessionId}/artifacts`),
  listSessionSnapshots: (sessionId: number) => get<TurnSnapshotOut[]>(`/turns/sessions/${sessionId}/snapshots`),
  listSessionAudit: (sessionId: number) => get<AuditLogOut[]>(`/turns/sessions/${sessionId}/audit`),
  getSessionUsage: (sessionId: number) => get<{
    total: number; context_window: number; message_count: number;
    input: number; cached_input: number; output: number; reasoning_output: number;
    agent_name: string;
    source?: string;  // v1.1: api_last=最后一次 API 真实占用 / est=本地估算
  }>(`/turns/sessions/${sessionId}/usage`),
  getUsageStats: (params?: { start?: string; end?: string; days?: number }) => {
    const q = new URLSearchParams();
    if (params?.start) q.set("start", params.start);
    if (params?.end) q.set("end", params.end);
    if (params?.days) q.set("days", String(params.days));
    const qs = q.toString();
    return get<UsageStatsOut>(`/usage/stats${qs ? `?${qs}` : ""}`);
  },

  // ── 定时任务 ──
  listScheduledTasks: () => get<ScheduledTaskOut[]>("/scheduled-tasks"),
  createScheduledTask: (data: { session_id: number; name: string; cron: string; prompt: string }) =>
    post<ScheduledTaskOut>("/scheduled-tasks", data),
  updateScheduledTask: (id: number, data: { name?: string; cron?: string; prompt?: string; enabled?: boolean }) =>
    patch<ScheduledTaskOut>(`/scheduled-tasks/${id}`, data),
  deleteScheduledTask: (id: number) => del<{ ok: boolean }>(`/scheduled-tasks/${id}`),

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
  createHook: (data: { event: string; command: string; matcher?: string; enabled?: boolean }) =>
    post<HookConfigOut>("/hooks", data),
  updateHook: (id: number, data: { command?: string; matcher?: string; enabled?: boolean }) =>
    patch<HookConfigOut>(`/hooks/${id}`, data),
  deleteHook: (id: number) => del<{ ok: boolean }>(`/hooks/${id}`),

  // ── 记忆 ──
  listMemories: (sessionId?: number) =>
    get<MemoryEntryOut[]>(`/memories${sessionId ? `?session_id=${sessionId}` : ""}`),
  deleteMemory: (id: number) => del<{ ok: boolean }>(`/memories/${id}`),
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
  scanProviderModels: (id: number) => post<{ models: ScannedModel[] }>(`/providers/${id}/scan`, {}),
  listProviderModels: (id: number) => get<ModelOut[]>(`/providers/${id}/models`),
  bulkSaveProviderModels: (id: number, models: Array<{
    name: string; is_active?: boolean; context_window?: number; is_multimodal?: boolean; reasoning_efforts?: string[];
  }>) => post<ModelOut[]>(`/providers/${id}/models`, { models }),

  // ── ta3（Ta+3 牛码）供应商（v23）──
  ta3LoginStart: (id: number) => post<{
    status: string; authorize_url?: string; state?: string; port?: number; expires_in?: number;
    account?: Record<string, unknown> | null;
  }>(`/providers/${id}/ta3/login/start`, {}),
  ta3LoginCancel: (id: number) => post<{ ok: boolean }>(`/providers/${id}/ta3/login/cancel`, {}),
  ta3LoginStatus: (id: number) => get<{ status: string; account?: Record<string, unknown> | null; error?: string | null }>(`/providers/${id}/ta3/login/status`),
  ta3Logout: (id: number) => post<{ ok: boolean }>(`/providers/${id}/ta3/logout`, {}),
  ta3Sync: (id: number) => post<{ synced: number; models: Array<{ name: string }> }>(`/providers/${id}/ta3/sync`, {}),

  // ── workbuddy（腾讯 CodeBuddy/WorkBuddy）供应商（v24）──
  workbuddyLoginStart: (id: number) => post<{
    status: string; auth_url?: string; state?: string; expires_in?: number;
    account?: Record<string, unknown> | null;
  }>(`/providers/${id}/workbuddy/login/start`, {}),
  workbuddyLoginCancel: (id: number) => post<{ ok: boolean }>(`/providers/${id}/workbuddy/login/cancel`, {}),
  workbuddyLoginStatus: (id: number) => get<{ status: string; account?: Record<string, unknown> | null; error?: string | null }>(`/providers/${id}/workbuddy/login/status`),
  workbuddyLogout: (id: number) => post<{ ok: boolean }>(`/providers/${id}/workbuddy/logout`, {}),
  workbuddySync: (id: number) => post<{ synced: number; models: Array<{ name: string }> }>(`/providers/${id}/workbuddy/sync`, {}),

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
  }) => patch<McpServerOut>(`/mcp-servers/${id}`, data),
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
  getAiRules: () => get<{ sources: Array<{ source: string; label: string; enabled: boolean }>; global_rules: string; workdir_rules: string }>("/settings/ai-rules"),
  setAiRules: (data: { enabled_sources?: string[]; global_rules?: string; workdir_rules?: string }) =>
    put<{ sources: Array<{ source: string; label: string; enabled: boolean }>; global_rules: string; workdir_rules: string }>("/settings/ai-rules", data),

  // ── 全局设置（v2.2: 设置中心持久化统一）──
  getGlobalSettings: () => get<GlobalSettingsOut>("/settings/global"),
  setGlobalSettings: (data: Partial<GlobalSettingsIn>) => put<GlobalSettingsOut>("/settings/global", data),

  // ── 子代理类型（v2.2: 对齐 zcode 3.13）──
  listSubagents: () => get<SubagentProfileOut[]>("/subagents"),
  createSubagent: (data: {
    name: string; description?: string; tools_whitelist?: string[];
    model_id?: number; system_prompt?: string; is_active?: boolean;
  }) => post<SubagentProfileOut>("/subagents", data),
  updateSubagent: (id: number, data: {
    name?: string; description?: string; tools_whitelist?: string[];
    model_id?: number; system_prompt?: string; is_active?: boolean;
  }) => patch<SubagentProfileOut>(`/subagents/${id}`, data),
  deleteSubagent: (id: number) => del<{ ok: boolean }>(`/subagents/${id}`),
};

export type {
  ProjectOut, SessionOut, TurnOut, MessageOut, TaskOut, ArtifactOut, ModelOut,
  ProviderOut, ScannedModel,
  ExecPolicyRuleOut, HookConfigOut, MemoryEntryOut, ScheduledTaskOut,
  RollbackPreviewFile, RollbackAffected, RollbackPreviewOut, FileChangeOut, FileDiffOut,
};

/** v2.2 (对齐 zcode 3.18): 全局设置（设置中心持久化统一，落 config.json） */
export interface GlobalSettingsOut {
  memory_enabled: boolean;
  global_rules: string;
  auto_compact_enabled: boolean;
  language: string;
  auto_approve_tools: boolean;
  force_approval_tools: string;
  session_token_budget: number;
  /** 常规面板补项 */
  terminal_shell: string;
  terminal_font: string;
  http_proxy: string;
  enhanced_search: boolean;
  show_todos: boolean;
  show_reasoning: boolean;
  /** v3.0 (plan-88): 计划模式允许访问工作区外路径 */
  plan_mode_allow_outside_access: boolean;
  /** v32 (plan-89): 沙箱模式（workspace-write / read-only / danger-full-access） */
  sandbox_mode: string;
  agent_max_steps?: number;
  browser_enabled?: boolean;
  browser_headless?: boolean;
}

export type GlobalSettingsIn = Partial<Omit<GlobalSettingsOut, never>>;

/** v3.0 (plan-88): 工具级规则候选清单项（PolicyPanel 工具下拉数据源） */
export interface ExecPolicyToolInfo {
  name: string;
  risk_level: string;
  description: string;
}

/** v2.2 (对齐 zcode 3.13): 子代理类型 */
export interface SubagentProfileOut {
  id: number;
  name: string;
  description: string | null;
  tools_whitelist: string[] | null;
  model_id: number | null;
  system_prompt: string | null;
  is_active: boolean;
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
}

/** 将后端返回的相对 url 转成可直接预览的绝对地址（图片缩略图/新窗口打开）。 */
export function resolveFileUrl(url: string): string {
  if (!url) return url;
  if (/^https?:\/\//.test(url)) return url;
  const baseStr = _directBase ?? BASE;
  const origin = baseStr.startsWith("http") ? new URL(baseStr).origin : window.location.origin;
  return url.startsWith("/") ? `${origin}${url}` : `${origin}/${url}`;
}
