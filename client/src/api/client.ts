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
  RollbackPreviewFile,
  RollbackPreviewOut,
  RollbackResult,
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
  MergeReportOut,
} from "@chatcoder/shared";

/** 后端 API 基址:桌面版直连 127.0.0.1:12973,网页版用相对路径走代理。
 * v6.4: 开发模式直连后端，绕过 vite 代理（Node 18+ Happy Eyeballs IPv6 问题）。
 * v2.1: 打包版端口由主进程探活后透传（getBackendPort），端口冲突自动换空闲端口。 */
const IS_ELECTRON = typeof window !== "undefined" && Boolean((window as Window).chatcoderAPI);
const IS_DEV = import.meta.env.DEV;

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
  const port = IS_ELECTRON ? await backendPort() : 12973;
  return `http://127.0.0.1:${port}/api`;
}

/** 默认基址：同步兜底（12973），实际请求前会用 directBase 修正 */
const BASE = (IS_ELECTRON || IS_DEV) ? "http://127.0.0.1:12973/api" : "/api";

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
      error?: { message?: string; code?: string };
      detail?: unknown;
    };
    if (body?.error?.message || body?.error?.code) {
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
  worktreeMergePreview: (worktreeProjectId: number, direction: WorktreeMergeDirection = "to_main") =>
    post<WorktreeMergePreview>(`/projects/worktrees/${worktreeProjectId}/merge/preview`, { direction }),
  worktreeMergeFile: (worktreeProjectId: number, path: string, direction: WorktreeMergeDirection = "to_main") =>
    post<{ ok: boolean; path: string; base: string | null; ours: string | null; theirs: string | null; binary?: boolean }>(
      `/projects/worktrees/${worktreeProjectId}/merge/file`, { path, direction }),
  worktreeMergeApply: (
    worktreeProjectId: number,
    files: Array<{ path: string; content?: string; deleted?: boolean }>,
    direction: WorktreeMergeDirection = "to_main",
  ) =>
    post<{ ok: boolean; committed: boolean; written: string[] }>(
      `/projects/worktrees/${worktreeProjectId}/merge/apply`, { files, direction }),
  worktreeMergeAi: (
    worktreeProjectId: number,
    path: string,
    hunk?: Record<string, unknown>,
    opts?: { direction?: WorktreeMergeDirection; modelId?: number | null },
  ) =>
    post<{ ok: boolean; suggestion?: string; error?: string; model?: string }>(
      `/projects/worktrees/${worktreeProjectId}/merge/ai`,
      { path, hunk, direction: opts?.direction ?? "to_main", model_id: opts?.modelId ?? null }),
  /** plan-308-1542 需求3-A：一键 AI 智能合并（进度经 WS merge.progress 广播）。 */
  worktreeMergeAiAll: (
    worktreeProjectId: number,
    opts?: { direction?: WorktreeMergeDirection; modelId?: number | null; sessionId?: number | null },
  ) =>
    post<{ ok: boolean; merge_id: string; resolved?: Record<string, string>; report?: MergeReportOut; engine?: string; preview?: WorktreeMergePreview }>(
      `/projects/worktrees/${worktreeProjectId}/merge/ai-all`,
      { direction: opts?.direction ?? "to_main", model_id: opts?.modelId ?? null, session_id: opts?.sessionId ?? null }),
  /** 合并前自动提交某一侧的未提交改动（side: worktree | main） */
  worktreeCommit: (worktreeProjectId: number, side: "worktree" | "main", message?: string) =>
    post<{ ok: boolean; committed: boolean; message: string }>(
      `/projects/worktrees/${worktreeProjectId}/commit`, { side, message }),
  // ── 插件市场（plan-282-1441 #6）──
  pluginMarketplace: () =>
    get<{ ok: boolean; items: PluginMarketItem[]; installedCount: number }>("/plugins/marketplace"),
  pluginInstalled: () => get<PluginMarketItem[]>("/plugins/installed"),
  pluginInstallDir: (path: string) =>
    post<{ ok: boolean; name: string; path: string; skills: number }>("/plugins/install-dir", { path }),
  pluginInstallGit: (repoUrl: string, market = "local") =>
    post<{ ok: boolean; name: string; path: string; skills: number }>("/plugins/install-git", { repo_url: repoUrl, market }),
  pluginSetEnabled: (name: string, enabled: boolean) =>
    patch<{ ok: boolean; name: string; enabled: boolean }>(
      `/plugins/${encodeURIComponent(name)}/enabled`, { enabled }),
  pluginUninstall: (name: string) =>
    del<{ ok: boolean; name: string }>(`/plugins/${encodeURIComponent(name)}`),
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
  arthasExec: (sessionId: number, command: string) =>
    post<Record<string, unknown>>("/debug/arthas/exec", { session_id: sessionId, command }),
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
  createSession: (data: { project_id: number; title?: string; model_id?: number; permission_mode?: string; goal_text?: string }) =>
    post<SessionOut>("/sessions", data),
  getSession: (id: number) => get<SessionOut>(`/sessions/${id}`),
  updateSession: (id: number, data: { title?: string; model_id?: number; pinned?: boolean; status?: string; permission_mode?: string }) =>
    patch<SessionOut>(`/sessions/${id}`, data),
  /** 删除会话。默认语义 = 归档（可恢复）；permanent=true 走物理删除（plan-282-1441 #4） */
  deleteSession: (id: number, permanent = false) =>
    del<{ ok: boolean }>(`/sessions/${id}${permanent ? "?permanent=true" : ""}`),
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
  cancelTurn: (turnId: number) => postCancel<{ ok: boolean }>(`/turns/${turnId}/cancel`),
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
  createHook: (data: { event: string; command: string; matcher?: string; enabled?: boolean }) =>
    post<HookConfigOut>("/hooks", data),
  updateHook: (id: number, data: { command?: string; matcher?: string; enabled?: boolean }) =>
    patch<HookConfigOut>(`/hooks/${id}`, data),
  deleteHook: (id: number) => del<{ ok: boolean }>(`/hooks/${id}`),

  // ── 记忆 ──
  // plan-230-1144 M4.1: 三层化——可按 scope/project 过滤、可提升作用域
  listMemories: (opts?: { sessionId?: number; scope?: string; projectId?: number; includeCandidate?: boolean }) => {
    const q = new URLSearchParams();
    if (opts?.sessionId != null) q.set("session_id", String(opts.sessionId));
    if (opts?.scope) q.set("scope", opts.scope);
    if (opts?.projectId != null) q.set("project_id", String(opts.projectId));
    if (opts?.includeCandidate === false) q.set("include_candidate", "false");
    const qs = q.toString();
    return get<MemoryEntryOut[]>(`/memories${qs ? `?${qs}` : ""}`);
  },
  deleteMemory: (id: number) => del<{ ok: boolean }>(`/memories/${id}`),
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
  setGlobalSettings: (data: Partial<GlobalSettingsIn>) => put<GlobalSettingsOut>("/settings/global", data),
  /** 问题7/8: 终端 Shell 存在性探测 + 候选等宽字体 */
  getTerminalOptions: () => get<TerminalOptionsOut>("/settings/terminal/options"),

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
  ProviderOut, ProviderCredentialOut, ScannedModel,
  WorktreeOut, WorktreeMergeFile, WorktreeMergePreview, WorktreeMergeDirection, MergeReportOut, PluginMarketItem,
  DbConnectionOut, DbPolicyOut, DebugStatusOut, RepoCandidate,
  DebugSettingsOut,
  ArthasStatusOut, ArthasEntryOut, ArthasProcessOut, ArthasConfigOut,
  ExecPolicyRuleOut, HookConfigOut, MemoryEntryOut, ScheduledTaskOut,
  RollbackPreviewFile, RollbackAffected, RollbackPreviewOut, FileChangeOut, FileDiffOut,
};

/** v2.2 (对齐 zcode 3.18): 全局设置（设置中心持久化统一，落 config.json） */
export interface GlobalSettingsOut {
  memory_enabled: boolean;
  global_rules: string;
  auto_compact_enabled: boolean;
  /** plan-278-1391: 上下文压缩触发阈值（0.50~0.95，默认 0.90） */
  auto_compact_threshold_ratio?: number;
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
  /** v45: 异常自动重试次数（0 = 不重试） */
  agent_retry_count?: number;
  /** v45: 每次重试前的等待秒数（逗号分隔，默认 10,20,30） */
  agent_retry_intervals?: string;
  browser_enabled?: boolean;
  browser_headless?: boolean;
}

export type GlobalSettingsIn = Partial<Omit<GlobalSettingsOut, never>>;

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
