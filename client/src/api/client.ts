/** 后端 API 客户端（v2：项目任务驱动架构）。
 *
 * 端点与 server/app/gateway/routers 一一对应，类型从 packages/shared 导入。
 */
import type {
  ArtifactOut,
  ConfigProfileOut,
  ExecPolicyRuleOut,
  FileChangeOut,
  FileDiffOut,
  HookConfigOut,
  MemoryEntryOut,
  MessageOut,
  ModelOut,
  ProjectOut,
  RollbackAffected,
  RollbackPreviewFile,
  RollbackPreviewOut,
  RollbackResult,
  ScheduledTaskOut,
  SessionOut,
  TaskOut,
  TurnOut,
  TurnSnapshotOut,
} from "@chatcoder/shared";

/** 后端 API 基址:桌面版直连 127.0.0.1:8000,网页版用相对路径走代理。
 * v6.4: 开发模式直连后端，绕过 vite 代理（Node 18+ Happy Eyeballs IPv6 问题）。 */
const IS_ELECTRON = typeof window !== "undefined" && Boolean((window as Window).chatcoderAPI);
const IS_DEV = import.meta.env.DEV;
const BASE = (IS_ELECTRON || IS_DEV) ? "http://127.0.0.1:8000/api" : "/api";

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
  const res = await fetch(`${BASE}${path}`, {
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
  const res = await fetchWithRetry(`${BASE}${path}`);
  if (!res.ok) {
    const detail = await res.text();
    throw new Error(errorMessage(res, detail));
  }
  return res.json();
}

async function patch<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetchWithRetry(`${BASE}${path}`, {
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
  const res = await fetchWithRetry(`${BASE}${path}`, { method: "DELETE" });
  if (!res.ok) {
    const detail = await res.text();
    throw new Error(errorMessage(res, detail));
  }
  return res.json();
}

async function put<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetchWithRetry(`${BASE}${path}`, {
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

export const api = {
  // ── 项目 ──
  listProjects: () => get<ProjectOut[]>("/projects"),
  createProject: (data: { path: string; name?: string; rules_docs?: string[]; auto_scan_rules?: boolean }) =>
    post<ProjectOut>("/projects", data),
  getProject: (id: number) => get<ProjectOut>(`/projects/${id}`),
  updateProject: (id: number, data: { name?: string; rules_docs?: string[]; auto_scan_rules?: boolean; pinned?: boolean; archived?: boolean }) =>
    patch<ProjectOut>(`/projects/${id}`, data),
  deleteProject: (id: number) => del<{ ok: boolean }>(`/projects/${id}`),
  scanProjectRules: (id: number) => get<string[]>(`/projects/${id}/scan-rules`),
  getProjectTree: (id: number, depth = 3) => get<ProjectTreeOut>(`/projects/${id}/tree?depth=${depth}`),
  readProjectFile: (id: number, path: string) =>
    get<{ path: string; content: string; size: number; truncated: boolean; language: string | null }>(
      `/projects/${id}/read-file?path=${encodeURIComponent(path)}`,
    ),

  // ── 会话 ──
  listSessions: (projectId?: number) =>
    get<SessionOut[]>(`/sessions${projectId ? `?project_id=${projectId}` : ""}`),
  createSession: (data: { project_id: number; title?: string; model_id?: number }) =>
    post<SessionOut>("/sessions", data),
  getSession: (id: number) => get<SessionOut>(`/sessions/${id}`),
  updateSession: (id: number, data: { title?: string; model_id?: number; pinned?: boolean; status?: string }) =>
    patch<SessionOut>(`/sessions/${id}`, data),
  /** 删除 = 归档 */
  deleteSession: (id: number) => del<{ ok: boolean }>(`/sessions/${id}`),
  forkSession: (id: number) => post<SessionOut>(`/sessions/${id}/fork`),
  renameSession: (id: number, title: string) =>
    post<SessionOut>(`/sessions/${id}/rename?title=${encodeURIComponent(title)}`),
  createWorktree: (id: number, branch?: string) =>
    post<{ ok: boolean; path: string; branch: string | null }>(
      `/sessions/${id}/worktree${branch ? `?branch=${encodeURIComponent(branch)}` : ""}`),
  removeWorktree: (id: number) => del<{ ok: boolean }>(`/sessions/${id}/worktree`),

  // ── 轮次（turn）──
  createTurn: (body: TurnCreateBody) => post<TurnOut>("/turns", body),
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
  listSessionMessages: (sessionId: number) => get<MessageOut[]>(`/turns/sessions/${sessionId}/messages`),
  listSessionTasks: (sessionId: number) => get<TaskOut[]>(`/turns/sessions/${sessionId}/tasks`),
  listSessionArtifacts: (sessionId: number) => get<ArtifactOut[]>(`/turns/sessions/${sessionId}/artifacts`),
  listSessionSnapshots: (sessionId: number) => get<TurnSnapshotOut[]>(`/turns/sessions/${sessionId}/snapshots`),
  listSessionAudit: (sessionId: number) => get<AuditLogOut[]>(`/turns/sessions/${sessionId}/audit`),
  getSessionUsage: (sessionId: number) => get<{
    total: number; context_window: number; message_count: number;
    input: number; cached_input: number; output: number; reasoning_output: number;
    agent_name: string;
  }>(`/turns/sessions/${sessionId}/usage`),

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
  createExecPolicyRule: (data: { session_id?: number; command_pattern: string; decision: string; justification?: string }) =>
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
    name: string; provider?: string; base_url?: string; intelligence_level?: number;
    context_window?: number; source_type?: string; is_active?: boolean; is_multimodal?: boolean;
    api_format?: string; api_key?: string; reasoning_efforts?: string[];
  }) => post<ModelOut>("/models", data),
  updateModel: (id: number, data: Record<string, unknown>) => patch<ModelOut>(`/models/${id}`, data),
  deleteModel: (id: number) => del<{ ok: boolean }>(`/models/${id}`),

  // ── 诊断 ──
  runDiagnostics: () => get<DiagnosticsOut>("/diagnostics"),
  updateCheck: () => get<UpdateCheckOut>("/update-check"),

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
    url?: string; is_active?: boolean;
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

  // ── 文件上传 ──
  uploadFile: async (file: File): Promise<{
    type: string; content: string; data_url: string | null;
    filename: string; size: number; mime: string | null;
  }> => {
    const formData = new FormData();
    formData.append("file", file);
    const res = await fetch(`${BASE}/upload`, { method: "POST", body: formData });
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
};

export type {
  ProjectOut, SessionOut, TurnOut, MessageOut, TaskOut, ArtifactOut, ModelOut,
  ExecPolicyRuleOut, HookConfigOut, MemoryEntryOut, ScheduledTaskOut,
  RollbackPreviewFile, RollbackAffected, RollbackPreviewOut, FileChangeOut, FileDiffOut,
};
