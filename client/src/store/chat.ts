/** v2 会话状态管理（zustand）：项目 / 会话 / turn 任务驱动。 */
import { create } from "zustand";
import { api } from "../api/client";
import type { ArtifactOut, FileChangeOut, MessageOut, ProjectOut, RollbackAffected, RollbackPreviewFile, SessionOut, TaskOut, TurnOut } from "../api/client";
import { wsClient } from "../api/ws";
import type { ServerEventName } from "@chatcoder/shared/events";

/** 上下文占用详情（输入框圆环）。 */
export interface UsageDetail {
  input: number;
  cached_input: number;
  output: number;
  reasoning_output: number;
  total: number;
  context_window: number;
  agent_name: string;
  /** v2.2 (对齐 zcode 3.10): 7 类用量分类（system/history/tool_results/thinking/input） */
  breakdown?: Record<string, number>;
  /** v1.1: 占用口径（api_last=最后一次 API 真实占用 / est=本地估算） */
  source?: string;
}

/** v15: 模型自主维护的执行清单项（todo_write 工具事件投影）。 */
export interface TodoItem {
  content: string;
  activeForm?: string;
  status: "pending" | "in_progress" | "completed";
}

/** v2.2: 排队输入项（运行中发送的消息进入队列，turn 完成后自动续发）。 */
export interface QueuedInput {
  id: string;
  content: string;
  attachments?: Record<string, unknown>[];
  reasoningEffort?: string;
  mode?: "readonly" | "plan" | null;
}

/**
 * v2.2 会话状态分桶（对齐 zcode 方案 3.21.1）：
 * 单会话的全部可变状态收敛为一个 slice，切换会话时视图零重载、状态不串。
 * 视图字段（messages/turns/...）仍是当前会话的投影，组件选择器无需改动。
 */
export interface SessionSlice {
  messages: MessageOut[];
  turns: TurnOut[];
  tasks: TaskOut[];
  artifacts: ArtifactOut[];
  runningTurnId: number | null;
  isRunning: boolean;
  interruptedTurnId: number | null;
  streamingBuffers: Record<number, string>;
  thinkingBuffers: Record<number, string>;
  usage: UsageDetail | null;
  isCompacting: boolean;
  pendingApproval: { approvalId: string; detail: Record<string, unknown> } | null;
  pendingPlan: { task: string } | null;
  pendingPlanTurn: { turnId: number; task: string } | null;
  pendingSplit: { turnId: number; requestTaskId: number; groupTaskId: number; reasons: string[] } | null;
  reviewedFiles: Record<string, boolean>;
  rollbackPending: { turnId: number; files: RollbackPreviewFile[]; affected: RollbackAffected } | null;
  turnChanges: Record<number, FileChangeOut[]>;
  todos: TodoItem[] | null;
  todoPersisted: boolean;
  agentActivity: Record<number, string>;
  queuedInputs: QueuedInput[];
}

/** 视图 → slice 快照（切换会话前保存当前会话状态）。 */
function _snapshotSlice(s: ChatState): SessionSlice {
  return {
    messages: s.messages,
    turns: s.turns,
    tasks: s.tasks,
    artifacts: s.artifacts,
    runningTurnId: s.runningTurnId,
    isRunning: s.isRunning,
    interruptedTurnId: s.interruptedTurnId,
    streamingBuffers: s.streamingBuffers,
    thinkingBuffers: s.thinkingBuffers,
    usage: s.usage,
    isCompacting: s.isCompacting,
    pendingApproval: s.pendingApproval,
    pendingPlan: s.pendingPlan,
    pendingPlanTurn: s.pendingPlanTurn,
    pendingSplit: s.pendingSplit,
    reviewedFiles: s.reviewedFiles,
    rollbackPending: s.rollbackPending,
    turnChanges: s.turnChanges,
    todos: s.todos,
    todoPersisted: s.todoPersisted,
    agentActivity: s.agentActivity,
    queuedInputs: s.queuedInputs,
  };
}

/** slice → 视图投影（切换会话时恢复缓存，跳过重复 REST 拉取）。 */
function _sliceToView(slice: SessionSlice | undefined): Partial<ChatState> {
  if (!slice) return {};
  return { ...slice };
}

interface ChatState {
  projects: ProjectOut[];
  sessions: SessionOut[];
  currentProjectId: number | null;
  currentSessionId: number | null;
  /** v2.2 分桶：按 sessionId 隔离的会话状态（切换零重载 + 状态不串）。 */
  sessionState: Record<number, SessionSlice>;
  /** 当前会话全部消息（含 turn_id，供 timeline 分组）。 */
  messages: MessageOut[];
  turns: TurnOut[];
  tasks: TaskOut[];
  /** v12: 当前会话产物聚合（Artifact 表，含 title/summary/files）。 */
  artifacts: ArtifactOut[];
  /** 当前正在运行的 turnId（无则 null）。 */
  runningTurnId: number | null;
  isRunning: boolean;
  /** 中断后可续跑的 turn（status=interrupted）。 */
  interruptedTurnId: number | null;
  /** 流式文本缓冲：agentId -> 文本（token.delta 累积）。 */
  streamingBuffers: Record<number, string>;
  /** 思考缓冲：agentId -> 思考文本。 */
  thinkingBuffers: Record<number, string>;
  /** 上下文占用（最新 usage.update）。 */
  usage: UsageDetail | null;
  /** v6.5: 是否正在压缩上下文（用于页面反馈）。 */
  isCompacting: boolean;
  /** 待审批请求。 */
  pendingApproval: { approvalId: string; detail: Record<string, unknown> } | null;
  /** 回滚/撤销后回填输入框的草稿。 */
  composerDraft: string;
  /** v6: /plan 计划确认弹窗（plan turn 完成后触发，task 为待执行任务）。 */
  pendingPlan: { task: string } | null;
  /** v7: /plan 待确认的 plan turn（旧兼容字段）。 */
  pendingPlanTurn: { turnId: number; task: string } | null;
  /** v13: 后端任务拆分提案所在区块。 */
  pendingSplit: { turnId: number; requestTaskId: number; groupTaskId: number; reasons: string[] } | null;
  /** v6: 已审查的产物文件（path -> true），用于审查清单展示。 */
  reviewedFiles: Record<string, boolean>;
  /** v9: 回滚确认弹窗数据（点击回滚先预览，确认后执行）。v12: 含连带影响统计。 */
  rollbackPending: { turnId: number; files: RollbackPreviewFile[]; affected: RollbackAffected } | null;
  /** v11: turn 完成后的变更审核清单缓存（turnId -> FileChangeOut[]）。 */
  turnChanges: Record<number, FileChangeOut[]>;
  /** v15: 当前 turn 模型自主维护的执行清单（todo.updated 事件）。 */
  todos: TodoItem[] | null;
  /** v15: 清单是否已持久化到任务区块（已持久化时由任务卡片展示，内嵌卡片隐藏）。 */
  todoPersisted: boolean;
  /** v15: 子代理实时活动（agentId -> 最新工具调用摘要，来自 tool.call 事件）。 */
  agentActivity: Record<number, string>;
  /** v19: 子代理元信息（agentId -> 名称/turn/任务/状态）——消息流子代理卡片数据源。 */
  subagentMeta: Record<number, { name: string; turnId: number | null; taskId: number | null; status: string }>;
  /** v19: 子代理线程消息桶（threadId=agentId -> 落库消息），右面板完整会话数据源。 */
  subagentMessages: Record<number, MessageOut[]>;
  /** v19: 子代理流式缓冲（threadId -> 文本/思考），主消息流不再混入子代理内容。 */
  subagentStreams: Record<number, string>;
  subagentThinking: Record<number, string>;
  /** v19: 拉取会话子代理列表（REST 重建卡片，历史会话可用）。 */
  loadSessionSubagents: () => Promise<void>;
  /** v2.2: 消息流滚动目标（任务卡步骤点击穿透 / turn 导航）。 */
  scrollTarget: { threadId?: number; turnId?: number } | null;
  /** v18: 全局最近选择的思考深度档位（空态首页选择跨入会话后由 ComposerBox 承接）。 */
  lastReasoningEffort: string | null;
  /** v2.2 (对齐 zcode 3.8): 输入队列——运行中发送的消息排队，turn 完成后自动续发。 */
  queuedInputs: QueuedInput[];
  loading: boolean;
  error: string | null;
  wsConnected: boolean;

  // 动作
  loadBootstrap: () => Promise<void>;
  createProject: (path: string, name?: string) => Promise<ProjectOut | null>;
  selectProject: (projectId: number) => Promise<void>;
  createSession: (projectId: number, title?: string) => Promise<number | null>;
  switchSession: (sessionId: number, fromHist?: boolean) => Promise<void>;
  /** 会话前进/后退历史（侧栏 logo 区与折叠态标题栏共用，zcode 顶部导航箭头） */
  sessionHist: number[];
  sessionHistIdx: number;
  histGo: (dir: -1 | 1) => void;
  deleteSession: (sessionId: number) => Promise<void>;
  renameSession: (sessionId: number, title: string) => Promise<void>;
  forkSession: (sessionId: number) => Promise<void>;
sendTurn: (content: string, attachments?: Record<string, unknown>[], reasoningEffort?: string, mode?: "readonly" | "plan" | null) => Promise<void>;
cancelTurn: () => Promise<void>;
  forceStop: () => Promise<void>;
resumeTurn: () => Promise<void>;
  rollbackTurn: (turnId: number, restoreToComposer?: boolean) => Promise<void>;
  /** v9: 请求回滚预览（拉取文件级回滚对比），返回是否成功（失败时 error 已设置）。 */
  requestRollbackPreview: (turnId: number) => Promise<boolean>;
  /** v9: 确认执行回滚（预览弹窗点确认后调用）。 */
  confirmRollback: (restoreToComposer?: boolean) => Promise<void>;
  /** v9: 取消回滚（关闭弹窗）。 */
  cancelRollback: () => void;
  confirmPlan: (task: string) => Promise<void>;
  confirmTaskSplit: (accepted: boolean, steps?: Array<{ task_id?: number; title: string }>) => Promise<void>;
  /** v15: 重试失败/已取消的步骤。 */
  retryTask: (taskId: number) => Promise<void>;
  dismissPlan: () => void;
  respondApproval: (approvalId: string, approved: boolean, remember?: boolean, answer?: Record<string, unknown>) => void;
  markFileReviewed: (path: string, reviewed: boolean) => void;
  /** v11: 拉取指定 turn 的变更审核清单。 */
  loadTurnChanges: (turnId: number) => Promise<void>;
  /** v11: 批量审核（乐观更新 + PUT 持久化，失败回滚并 toast）。 */
  reviewFiles: (turnId: number, paths: string[], reviewed: boolean) => Promise<void>;
  refreshMessages: () => Promise<void>;
  refreshTurns: () => Promise<void>;
  refreshTasks: () => Promise<void>;
  /** v12: 刷新当前会话产物聚合（随 refreshTasks 一并拉取）。 */
  refreshArtifacts: () => Promise<void>;
  setComposerDraft: (text: string) => void;
  clearError: () => void;
  addMessage: (msg: MessageOut) => void;
  /** v2.2: 请求消息流滚动到某子代理线程首条消息（任务卡步骤穿透）。 */
  requestScrollTo: (target: { threadId?: number; turnId?: number }) => void;
  clearScrollTarget: () => void;
  /** v2.2: 更新/删除排队输入（patch=null 表示删除）。 */
  updateQueuedInput: (id: string, patch: Partial<QueuedInput> | null) => void;
  /** v2.2: turn 结束后自动续发队列头（内部调用）。 */
  _drainQueue: () => Promise<void>;
  handleWs: (event: string, payload: Record<string, unknown>) => void;
}

let _sendingGuard = false;
let _wsUnsub: (() => void) | null = null;
let _heartbeatTimer: ReturnType<typeof setTimeout> | null = null;
const HEARTBEAT_TIMEOUT = 60_000; // 60s 无事件超时兜底复位
let _stopGuardTs = 0; // v1.1: 用户点击停止后 5 秒内，忽略后端残留的 running 状态回跳
let _stoppingTurnId: number | null = null;

/** 启动/重置心跳计时器：60s 内无任何 WS 事件则强制复位 isRunning */
function _startHeartbeat() {
  if (_heartbeatTimer) clearTimeout(_heartbeatTimer);
  _heartbeatTimer = setTimeout(() => {
    _heartbeatTimer = null;
    const st = useChatStore.getState();
    if (!st.isRunning) return;
    void st.refreshTurns().catch(() => {
      useChatStore.setState({ isRunning: false, runningTurnId: null });
    }).finally(() => {
      if (useChatStore.getState().isRunning) _startHeartbeat();
    });
  }, HEARTBEAT_TIMEOUT);
}

/** 清除心跳计时器 */
function _clearHeartbeat() {
  if (_heartbeatTimer) {
    clearTimeout(_heartbeatTimer);
    _heartbeatTimer = null;
  }
}

/**
 * 性能优化（rAF 批量合并）：token.delta / thinking.delta 高频到达时，
 * 先累积到模块级 pending 缓冲，每帧仅一次 set() 合并进 store，
 * 将"每 token 一次全量渲染"降为"每帧一次"，大幅减少 React 重渲染次数。
 */
let _pendingToken: Record<number, string> = {};
let _pendingThinking: Record<number, string> = {};
// v19: agentId -> threadId（子代理流式内容分桶到 subagentStreams/subagentThinking）
let _pendingThread: Record<number, number> = {};
let _streamDoneText: Record<string, string> = {};
let _flushScheduled = false;

function _clearPendingDeltas() {
  _pendingToken = {};
  _pendingThinking = {};
  _pendingThread = {};
  _streamDoneText = {};
}

function _clearPendingFor(agentId: number) {
  delete _pendingToken[agentId];
  delete _pendingThinking[agentId];
  delete _pendingThread[agentId];
}

/** 清除某 agent/thread 的流完成标记：消息落库后允许同 turn 内的下一段思考/正文继续实时流式 */
function _clearStreamDoneFor(agentOrThread: number) {
  delete _streamDoneText[`thinking:${agentOrThread}`];
  delete _streamDoneText[`token:${agentOrThread}`];
}

function _scheduleDeltaFlush() {
  if (_flushScheduled) return;
  _flushScheduled = true;
  requestAnimationFrame(() => {
    _flushScheduled = false;
    const tok = _pendingToken;
    const thk = _pendingThinking;
    const thr = _pendingThread;
    _pendingToken = {};
    _pendingThinking = {};
    _pendingThread = {};
    const tokKeys = Object.keys(tok);
    const thkKeys = Object.keys(thk);
    if (tokKeys.length === 0 && thkKeys.length === 0) return;
    useChatStore.setState((s) => {
      const next: Partial<ChatState> = {};
      // v19: 子代理（thread_id != null）流式内容进独立桶，主消息流不混入
      if (tokKeys.length > 0) {
        const streaming = { ...s.streamingBuffers };
        const subStreams = { ...s.subagentStreams };
        for (const k of tokKeys) {
          const aid = Number(k);
          const tid = thr[aid];
          if (tid != null) subStreams[tid] = (subStreams[tid] || "") + tok[aid];
          else streaming[aid] = (streaming[aid] || "") + tok[aid];
        }
        next.streamingBuffers = streaming;
        next.subagentStreams = subStreams;
      }
      if (thkKeys.length > 0) {
        const thinking = { ...s.thinkingBuffers };
        const subThinking = { ...s.subagentThinking };
        for (const k of thkKeys) {
          const aid = Number(k);
          const tid = thr[aid];
          if (tid != null) subThinking[tid] = (subThinking[tid] || "") + thk[aid];
          else thinking[aid] = (thinking[aid] || "") + thk[aid];
        }
        next.thinkingBuffers = thinking;
        next.subagentThinking = subThinking;
      }
      return next;
    });
  });
}

/** 有序追加：后端按 id 升序投递，常规追加 O(1)；仅乱序兜底时排序插入。 */
function _appendOrdered(messages: MessageOut[], msg: MessageOut): MessageOut[] {
  const last = messages[messages.length - 1];
  const lastId = last ? Number(last.id) || 0 : 0;
  if ((Number(msg.id) || 0) >= lastId) return [...messages, msg];
  return [...messages, msg].sort((a, b) => (Number(a.id) || 0) - (Number(b.id) || 0));
}

/**
 * v14: 判断乐观用户消息与后端真实用户消息是否同一条。
 * 文本一致即可；仅附件（空文本）消息比较附件 file_id 集合，
 * 保证「只发文件」的消息也能被乐观消息替换而非重复追加。
 */
function _sameUserContent(a: Record<string, unknown> | undefined, b: Record<string, unknown> | undefined): boolean {
  const ta = typeof a?.text === "string" ? a.text : "";
  const tb = typeof b?.text === "string" ? b.text : "";
  if (ta || tb) return ta === tb;
  const fa = Array.isArray(a?.attachments)
    ? (a.attachments as { file_id?: unknown }[]).map((x) => String(x.file_id ?? "")).join(",")
    : "";
  const fb = Array.isArray(b?.attachments)
    ? (b.attachments as { file_id?: unknown }[]).map((x) => String(x.file_id ?? "")).join(",")
    : "";
  return fa !== "" && fa === fb;
}

/** 统一清理所有会话级字段（§9.1 #18：防止切换会话后残留）。v2.2: 并入分桶重置。 */
function _resetSessionState(): Partial<ChatState> {
  _clearHeartbeat();
  _clearPendingDeltas();
  return {
    messages: [],
    turns: [],
    tasks: [],
    artifacts: [],
    runningTurnId: null,
    isRunning: false,
    interruptedTurnId: null,
    streamingBuffers: {},
    thinkingBuffers: {},
    usage: null,
    isCompacting: false,
    pendingApproval: null,
    pendingPlan: null,
    pendingPlanTurn: null,
    pendingSplit: null,
    reviewedFiles: {},
    rollbackPending: null,
    turnChanges: {},
    todos: null,
    todoPersisted: false,
    agentActivity: {},
    subagentMeta: {},
    subagentMessages: {},
    subagentStreams: {},
    subagentThinking: {},
    scrollTarget: null,
    queuedInputs: [],
  };
}

export const useChatStore = create<ChatState>((set, get) => ({
  projects: [],
  sessions: [],
  currentProjectId: null,
  currentSessionId: null,
  sessionHist: [],
  sessionHistIdx: -1,
  histGo: (dir) => {
    const { sessionHist, sessionHistIdx } = get();
    const next = sessionHistIdx + dir;
    if (next < 0 || next >= sessionHist.length) return;
    set({ sessionHistIdx: next });
    void get().switchSession(sessionHist[next], true);
  },
  sessionState: {},
  messages: [],
  turns: [],
  tasks: [],
  artifacts: [],
  runningTurnId: null,
  isRunning: false,
  interruptedTurnId: null,
  streamingBuffers: {},
  thinkingBuffers: {},
  usage: null,
  isCompacting: false,
  pendingApproval: null,
  pendingPlan: null,
  pendingPlanTurn: null,
  pendingSplit: null,
  reviewedFiles: {},
  rollbackPending: null,
  turnChanges: {},
  todos: null,
  todoPersisted: false,
  agentActivity: {},
  subagentMeta: {},
  subagentMessages: {},
  subagentStreams: {},
  subagentThinking: {},
  scrollTarget: null,
  queuedInputs: [],
  composerDraft: "",
  lastReasoningEffort: null,
  loading: false,
  error: null,
  wsConnected: false,

  loadBootstrap: async () => {
    set({ loading: true, error: null });
    try {
      const [projects, sessions] = await Promise.all([api.listProjects(), api.listSessions()]);
      // 自动选中第一个活跃项目/会话，并清理已归档或已删除的旧项目 ID。
      const activeProjects = projects.filter((p) => !p.archived);
      const activeSessions = sessions.filter((s) => s.status !== "archived");
      const current = get();
      const currentProject = activeProjects.find((p) => p.id === current.currentProjectId);
      const sessionProject = current.currentSessionId == null
        ? null
        : activeProjects.find((p) => p.id === sessions.find((s) => s.id === current.currentSessionId)?.project_id);
      set({
        projects,
        sessions,
        loading: false,
        currentProjectId: currentProject?.id ?? sessionProject?.id ?? activeProjects[0]?.id ?? null,
      });
      if (!get().currentSessionId && activeSessions.length > 0) {
        const first = activeSessions[0];
        const proj = activeProjects.find((p) => p.id === first.project_id) || null;
        set({
          currentProjectId: proj?.id ?? null,
          currentSessionId: first.id,
          ...(proj ? { projects: [proj, ...projects.filter((x) => x.id !== proj.id)] } : {}),
        });
        await get().switchSession(first.id);
      } else if (activeProjects.length > 0 && !get().currentProjectId) {
        set({ currentProjectId: activeProjects[0].id });
      }
    } catch (e) {
      set({ loading: false, error: String(e) });
    }
  },

  createProject: async (path, name) => {
    try {
      const project = await api.createProject({ path, name });
      set((s) => ({ projects: [project, ...s.projects], currentProjectId: project.id }));
      return project;
    } catch (e) {
      set({ error: String(e) });
      return null;
    }
  },

  selectProject: async (projectId) => {
    // v2.2: 切项目前快照当前会话（与 switchSession 保持一致的分桶语义）
    const prev = get();
    if (prev.currentSessionId != null) {
      const slice = _snapshotSlice(prev);
      set((s) => ({ sessionState: { ...s.sessionState, [prev.currentSessionId as number]: slice } }));
    }
    set({ currentProjectId: projectId, currentSessionId: null, ..._resetSessionState() });
    try {
      const sessions = await api.listSessions(projectId);
      set({ sessions });
      const active = sessions.find((s) => s.status !== "archived");
      if (active) await get().switchSession(active.id);
    } catch (e) {
      set({ error: String(e) });
    }
  },

  createSession: async (projectId, title) => {
    try {
      const session = await api.createSession({ project_id: projectId, title });
      set((s) => ({ sessions: [session, ...s.sessions] }));
      await get().switchSession(session.id);
      return session.id;
    } catch (e) {
      set({ error: String(e) });
      return null;
    }
  },

  switchSession: async (sessionId, fromHist) => {
    // 导航历史入栈（histGo 触发的切换跳过，避免重复入栈）
    if (!fromHist) {
      const h = get().sessionHist;
      const idx = get().sessionHistIdx;
      if (h[idx] !== sessionId) {
        const stack = [...h.slice(0, idx + 1), sessionId];
        set({ sessionHist: stack, sessionHistIdx: stack.length - 1 });
      }
    }
    // 清理旧 WS 连接和 handler（§9.1 #1②：防止 handler 累积）
    wsClient.disconnect();
    if (_wsUnsub) { _wsUnsub(); _wsUnsub = null; }

    // v2.2 分桶：切走前把当前视图快照存回旧会话 slice（后台会话状态不丢）
    const prev = get();
    const prevId = prev.currentSessionId;
    if (prevId != null && prevId !== sessionId) {
      const slice = _snapshotSlice(prev);
      set((s) => ({ sessionState: { ...s.sessionState, [prevId]: slice } }));
    }

    const cached = get().sessionState[sessionId];
    set({
      currentSessionId: sessionId,
      ..._resetSessionState(),
      // v2.2: 有缓存直接恢复视图（零重载切换），无缓存走 REST 首次加载
      ..._sliceToView(cached),
    });
    const session = get().sessions.find((s) => s.id === sessionId);
    if (session?.project_id) set({ currentProjectId: session.project_id });

    // WS 连接 + 事件监听（保存 cleanup 函数）
    wsClient.connect(sessionId);
    _wsUnsub = wsClient.on((ev) => {
      const payload = ev.payload as Record<string, unknown>;
      get().handleWs(ev.event, payload);
    });

    if (cached) {
      // v2.2: 缓存命中——零重载切换，静默补齐运行状态与任务（后台期间可能已变化）
      if (cached.isRunning || cached.runningTurnId) {
        _startHeartbeat();
      }
      void get().refreshTurns();
      void get().refreshTasks();
      // v1.1: 缓存切换也要刷新占用（后台会话可能已运行/压缩过）
      void api.getSessionUsage(sessionId).then((u) => {
        if (get().currentSessionId !== sessionId) return;
        set({ usage: {
          input: u.input, cached_input: u.cached_input, output: u.output,
          reasoning_output: u.reasoning_output, total: u.total,
          context_window: u.context_window, agent_name: u.agent_name,
          source: u.source,
        } });
      }).catch(() => {});
      // v19: 缓存切换同样重建子代理卡片
      void get().loadSessionSubagents();
      return;
    }

    set({ loading: true, error: null });
    try {
      const [messages, turns, tasks] = await Promise.all([
        api.listSessionMessages(sessionId),
        api.listTurns(sessionId),
        api.listSessionTasks(sessionId),
      ]);
      const running = turns.find((t) => t.status === "running");
      const interrupted = turns.find((t) => t.status === "interrupted");
      set({
        messages, turns, tasks, loading: false,
        runningTurnId: running?.id ?? null,
        isRunning: Boolean(running),
        interruptedTurnId: interrupted?.id ?? null,
      });

      // 加载会话 token 占用估算（解决重启后 usage 为 null 显示 0% 问题）
      try {
        const usage = await api.getSessionUsage(sessionId);
        set({ usage: {
          input: usage.input,
          cached_input: usage.cached_input,
          output: usage.output,
          reasoning_output: usage.reasoning_output,
          total: usage.total,
          context_window: usage.context_window,
          agent_name: usage.agent_name,
          source: usage.source, // v1.1: 口径标注（api_last / est）
        }});
      } catch { /* usage 加载失败不阻塞 */ }

      // v11: 重开/刷新会话后恢复已完成 turn 的审核卡片（审核状态持久化在后端）。
      // 仅取最近 10 个已完成 turn，避免请求过多。
      for (const t of turns.filter((x) => x.status === "completed").slice(-10)) {
        get().loadTurnChanges(t.id);
      }

      // 如果有运行中的 turn，启动心跳超时兜底（§9.1 #3）
      if (running) _startHeartbeat();
      // v19: 重建子代理卡片（历史会话刷新后可点击进右面板）
      void get().loadSessionSubagents();
    } catch (e) {
      set({ loading: false, error: String(e) });
    }
  },

  deleteSession: async (sessionId) => {
    try {
      await api.deleteSession(sessionId);
      const sessions = await api.listSessions();
      set((s) => ({
        sessions,
        // v2.2: 同步清理已删除会话的分桶缓存
        ...(s.sessionState[sessionId]
          ? { sessionState: Object.fromEntries(Object.entries(s.sessionState).filter(([k]) => Number(k) !== sessionId)) }
          : {}),
      }));
      if (get().currentSessionId === sessionId) {
        const active = sessions.find((s) => s.status !== "archived" && s.id !== sessionId);
        if (active) await get().switchSession(active.id);
        else set({ currentSessionId: null, currentProjectId: null });
      }
    } catch (e) {
      set({ error: String(e) });
    }
  },

  renameSession: async (sessionId, title) => {
    try {
      const updated = await api.renameSession(sessionId, title);
      set((s) => ({ sessions: s.sessions.map((x) => (x.id === sessionId ? updated : x)) }));
    } catch (e) {
      set({ error: String(e) });
    }
  },

  forkSession: async (sessionId) => {
    try {
      const fork = await api.forkSession(sessionId);
      set((s) => ({ sessions: [fork, ...s.sessions] }));
      await get().switchSession(fork.id);
    } catch (e) {
      set({ error: String(e) });
    }
  },

  sendTurn: async (content, attachments, reasoningEffort, mode) => {
    const { currentSessionId, isRunning } = get();
    if (!currentSessionId) return;
    // v14: 支持「只发附件不带文字」——只要 content 或 attachments 有其一即可发送
    const hasText = Boolean(content && content.trim());
    const hasAtts = Array.isArray(attachments) && attachments.length > 0;
    if (!hasText && !hasAtts) return;
    // v2.2: 输入队列——运行中发送的消息进入队列，当前 turn 完成后自动续发
    if (_sendingGuard) {
      // 并发保护（如 turn 结束事件连续触发）：放回队头不丢失
      set((s) => ({
        queuedInputs: [{
          id: `q-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
          content, attachments, reasoningEffort, mode,
        }, ...s.queuedInputs],
      }));
      return;
    }
    if (isRunning) {
      set((s) => ({
        queuedInputs: [...s.queuedInputs, {
          id: `q-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
          content,
          attachments,
          reasoningEffort,
          mode,
        }],
      }));
      return;
    }
    _sendingGuard = true;
    // 立即添加用户消息到本地状态，实现即时显示（流式体验）
    const optimisticUserMsg: MessageOut = {
      id: Date.now(), // 临时 ID，后续会被 WebSocket 的真实消息覆盖（addMessage 有去重）
      session_id: currentSessionId,
      turn_id: null, // turn_id 稍后由 WebSocket 事件回填
      thread_id: null,
      sender_type: "user",
      sender_id: null,
      msg_type: "text",
      content: hasAtts ? { text: content || "", attachments } : { text: content },
      token_usage: 0,
      created_at: new Date().toISOString(),
    };
    get().addMessage(optimisticUserMsg);
    try {
      const turn = await api.createTurn({ session_id: currentSessionId, content, attachments, reasoning_effort: reasoningEffort, mode });
      _clearPendingDeltas(); // v6.5: 新 turn 清掉上一轮残留的完成标记，保证思考/正文从头实时流式
      set((s) => ({
        turns: [...s.turns, turn],
        runningTurnId: turn.id,
        isRunning: true,
        interruptedTurnId: null,
        // v7: /plan 不再立即弹确认框——记录待确认 plan turn，
        // 等后端真正生成 plan 文档并 turn.completed 后才弹出确认弹窗
        ...(mode === "plan" ? { pendingPlanTurn: { turnId: turn.id, task: content } } : {}),
      }));
      _startHeartbeat();
    } catch (e) {
      set({ error: String(e), isRunning: false, pendingPlan: null, pendingPlanTurn: null });
    } finally {
      _sendingGuard = false;
    }
  },

  confirmPlan: async (task) => {
    const { currentSessionId } = get();
    if (!currentSessionId || !task.trim()) return;
    // 清空确认状态后，按计划执行任务（正常模式，agent 将读取 ai/chatcoder-plan.md 执行）
    set({ pendingPlan: null });
    await get().sendTurn(task);
  },

  dismissPlan: () => set({ pendingPlan: null, pendingPlanTurn: null }),

  confirmTaskSplit: async (accepted, steps) => {
    const pending = get().pendingSplit;
    if (!pending) return;
    try {
      await api.confirmTaskPlan(pending.turnId, pending.groupTaskId, { accepted, steps });
      set({ pendingSplit: null });
      await get().refreshTasks();
      if (!accepted) await get().refreshTurns();
    } catch (e) {
      set({ error: String(e) });
    }
  },

  retryTask: async (taskId) => {
    const task = get().tasks.find((t) => t.id === taskId);
    if (!task || task.turn_id == null) return;
    try {
      await api.retryTask(task.turn_id, taskId);
      await get().refreshTasks();
    } catch (e) {
      set({ error: String(e) });
    }
  },

  respondApproval: (approvalId, approved, remember = false, answer) => {
    // v2.2 (对齐 zcode 3.12/3.14): remember=true 生成"始终允许"规则；
    // answer 为 ask_user_question 的结构化回答
    wsClient.send("approval.response", {
      approval_id: approvalId, approved, remember,
      ...(answer ? { answer } : {}),
    });
    // 本地立即关闭横幅，避免等待广播返回造成的 UI 延迟
    set({ pendingApproval: null });
  },

  markFileReviewed: (path, reviewed) => {
    set((s) => {
      const next = { ...s.reviewedFiles };
      if (reviewed) next[path] = true;
      else delete next[path];
      return { reviewedFiles: next };
    });
  },

  // v11: 拉取该 turn 的变更审核清单（含后端持久化审核状态）。失败静默，下次完成/切会话时重试。
  loadTurnChanges: async (turnId) => {
    try {
      const changes = await api.getTurnChanges(turnId);
      set((s) => ({ turnChanges: { ...s.turnChanges, [turnId]: changes } }));
    } catch { /* 非关键路径，忽略 */ }
  },

  // v11: 批量审核——乐观更新本地状态后 PUT 持久化；失败回滚并 toast 提示。
  reviewFiles: async (turnId, paths, reviewed) => {
    const prev = get().turnChanges[turnId] ?? [];
    set((s) => ({
      turnChanges: {
        ...s.turnChanges,
        [turnId]: (s.turnChanges[turnId] ?? []).map((c) =>
          paths.includes(c.path) ? { ...c, reviewed } : c,
        ),
      },
    }));
    try {
      await api.reviewFiles(turnId, paths, reviewed);
    } catch (e) {
      set((s) => ({ turnChanges: { ...s.turnChanges, [turnId]: prev } }));
      set({ error: String(e) });
    }
  },

   cancelTurn: async () => {
    const { runningTurnId } = get();
    if (!runningTurnId || _stoppingTurnId === runningTurnId) return;
    _stopGuardTs = Date.now();
    _stoppingTurnId = runningTurnId;
    try { await api.cancelTurn(runningTurnId); }
    catch (e) { set({ error: String(e) }); }
    finally {
      if (_stoppingTurnId === runningTurnId) _stoppingTurnId = null;
      const sid = get().currentSessionId;
      if (sid) { try { await get().refreshTurns(); } catch { /* ignore */ } }
    }
  },
  forceStop: async () => {
    const { runningTurnId, currentSessionId } = get();
    if (!runningTurnId || _stoppingTurnId === runningTurnId) return;
    _stopGuardTs = Date.now();
    _stoppingTurnId = runningTurnId;
    try { await api.cancelTurn(runningTurnId); } catch { /* 后端可能已结束 */ }
    finally {
      if (_stoppingTurnId === runningTurnId) _stoppingTurnId = null;
      if (currentSessionId) { try { await get().refreshTurns(); } catch { /* ignore */ } }
    }
  },resumeTurn: async () => {
    const { interruptedTurnId, currentSessionId } = get();
    if (!interruptedTurnId || !currentSessionId) return;
    try {
      const turn = await api.resumeTurn(interruptedTurnId);
      set((s) => ({
        turns: s.turns.map((t) => (t.id === turn.id ? turn : t)),
        runningTurnId: turn.id, isRunning: true, interruptedTurnId: null,
      }));
      _startHeartbeat();
    } catch (e) {
      set({ error: String(e) });
    }
  },

  rollbackTurn: async (turnId, restoreToComposer = true) => {
    const { currentSessionId } = get();
    if (!currentSessionId) return;
    try {
      // 若该 turn 正在运行，先取消
      if (get().runningTurnId === turnId) {
        await api.cancelTurn(turnId);
        set({ runningTurnId: null, isRunning: false });
      }
      const result = await api.rollbackTurn(turnId, { restore_to_composer: restoreToComposer });
      const [messages, turns, tasks] = await Promise.all([
        api.listSessionMessages(currentSessionId),
        api.listTurns(currentSessionId),
        api.listSessionTasks(currentSessionId),
      ]);
      set({
        messages, turns, tasks,
        isRunning: false, runningTurnId: null,
        // 回填原文到输入框，供用户修改后重发
        ...(restoreToComposer && result.user_message ? { composerDraft: result.user_message } : {}),
      });
    } catch (e) {
      set({ error: String(e) });
    }
  },

  // v9: 回滚前先预览（文件级 before/after 对比），经用户确认后再执行，避免误伤手动改动。
  requestRollbackPreview: async (turnId) => {
    const { currentSessionId } = get();
    if (!currentSessionId) return false;
    try {
      // 若该 turn 正在运行，先取消
      if (get().runningTurnId === turnId) {
        await api.cancelTurn(turnId);
        set({ runningTurnId: null, isRunning: false });
      }
      const preview = await api.rollbackPreview(turnId);
      set({ rollbackPending: { turnId, files: preview.files, affected: preview.affected } });
      return true;
    } catch (e) {
      set({ error: String(e) });
      return false;
    }
  },

  confirmRollback: async (restoreToComposer = true) => {
    const pending = get().rollbackPending;
    if (!pending) return;
    const turnId = pending.turnId;
    set({ rollbackPending: null });
    await get().rollbackTurn(turnId, restoreToComposer);
  },

  cancelRollback: () => set({ rollbackPending: null }),

  loadSessionSubagents: async () => {
    const { currentSessionId } = get();
    if (!currentSessionId) return;
    try {
      const list = await api.listSessionSubagents(currentSessionId);
      set((s) => {
        const meta = { ...s.subagentMeta };
        for (const it of list) {
          const prev = meta[it.agent_id];
          meta[it.agent_id] = {
            name: it.name || prev?.name || `子代理 #${it.agent_id}`,
            turnId: it.turn_id ?? prev?.turnId ?? null,
            taskId: it.task_id ?? prev?.taskId ?? null,
            // 实时事件已标记 running 时不被 REST 旧状态回退
            status: prev?.status === "running" && it.status === "pending" ? "running" : (it.status || prev?.status || "running"),
          };
        }
        return { subagentMeta: meta };
      });
    } catch { /* 非阻塞 */ }
  },

  refreshMessages: async () => {
    const { currentSessionId } = get();
    if (!currentSessionId) return;
    try {
      const messages = await api.listSessionMessages(currentSessionId);
      if (get().currentSessionId !== currentSessionId) return;
       set({ messages });
    } catch (e) {
      set({ error: String(e) });
    }
  },

  refreshTurns: async () => {
    const { currentSessionId } = get();
    if (!currentSessionId) return;
    try {
      const turns = await api.listTurns(currentSessionId);
      if (get().currentSessionId !== currentSessionId) return;
       const running = turns.find((t) => t.status === "running");
      const interrupted = turns.find((t) => t.status === "interrupted");
      set({
        turns,
        runningTurnId: running?.id ?? null,
        isRunning: Boolean(running),
        interruptedTurnId: interrupted?.id ?? null,
      });
    } catch (e) {
      set({ error: String(e) });
    }
  },

  refreshTasks: async () => {
    const { currentSessionId } = get();
    if (!currentSessionId) return;
    try {
      const tasks = await api.listSessionTasks(currentSessionId);
      if (get().currentSessionId !== currentSessionId) return;
       const visible = tasks.filter((task) => !task.is_hidden);
      const proposedGroup = visible.find((task) => task.kind === "group" && task.status === "proposed");
      const requestTask = proposedGroup
        ? visible.find((task) => task.id === proposedGroup.parent_task_id)
        : undefined;
      set({
        tasks,
        ...(proposedGroup && requestTask && proposedGroup.turn_id != null
          ? { pendingSplit: {
              turnId: proposedGroup.turn_id,
              requestTaskId: requestTask.id,
              groupTaskId: proposedGroup.id,
              reasons: [],
            } }
          : {}),
      });
    } catch (e) {
      set({ error: String(e) });
    }
    get().refreshArtifacts();
  },

  refreshArtifacts: async () => {
    const { currentSessionId } = get();
    if (!currentSessionId) return;
    try {
      const artifacts = await api.listSessionArtifacts(currentSessionId);
      if (get().currentSessionId !== currentSessionId) return;
       set({ artifacts });
    } catch (e) {
      set({ error: String(e) });
    }
  },

  setComposerDraft: (text) => set({ composerDraft: text }),
  clearError: () => set({ error: null }),

  requestScrollTo: (target) => set({ scrollTarget: { ...target } }),
  clearScrollTarget: () => set({ scrollTarget: null }),

  updateQueuedInput: (id, patch) => {
    set((s) => ({
      queuedInputs: patch === null
        ? s.queuedInputs.filter((q) => q.id !== id)
        : s.queuedInputs.map((q) => (q.id === id ? { ...q, ...patch } : q)),
    }));
  },

  _drainQueue: async () => {
    const { queuedInputs, isRunning, currentSessionId } = get();
    if (!currentSessionId || isRunning || _sendingGuard) return;
    const next = queuedInputs[0];
    if (!next) return;
    // 出队后立即续发（sendTurn 内部会重新入队若仍运行中）
    set((s) => ({ queuedInputs: s.queuedInputs.slice(1) }));
    await get().sendTurn(next.content, next.attachments, next.reasoningEffort, next.mode);
  },

  addMessage: (msg) => {
    set((state) => {
      // 去重：相同 ID 直接跳过
      if (state.messages.some((m) => m.id === msg.id)) return {};
      // 处理乐观消息替换：后端真实用户消息到达时，移除同内容同 sender 的临时乐观消息
      // 乐观消息 ID 是 Date.now()（很大），真实消息 ID 较小
      const isUserMsg = msg.sender_type === "user";
      if (isUserMsg) {
        const optimisticIdx = state.messages.findIndex((m) =>
          m.sender_type === "user" &&
          m.id > 1_000_000_000_000 && // 乐观消息特征：超大临时 ID
          _sameUserContent(m.content, msg.content) // v14: 文本或附件 file_id 一致即可替换
        );
        if (optimisticIdx >= 0) {
          const newMessages = [...state.messages];
          newMessages.splice(optimisticIdx, 1, msg); // 原位替换，保持顺序
          return { messages: newMessages };
        }
      }
      // 按 ID 升序插入（后端按 id asc 投递，常规直接追加 O(1)，仅乱序兜底排序）
      return { messages: _appendOrdered(state.messages, msg) };
    });
  },

  handleWs: (event, payload) => {
    const st = get();
    // 运行中收到任何 WS 事件时重置心跳（§9.1 #3：60s 无事件超时兜底）
    if (st.isRunning) _startHeartbeat();
    // v2.1: 事件名契约化（packages/shared/src/events.ts 穷举），default 走 never 编译期兜底
    const ev = event as ServerEventName;
    switch (ev) {
      case "message.created": {
        // 后端 payload 结构为 { msg: MessageOut }，需解包
        const rawMsg = (payload.msg ?? payload) as MessageOut;
        if (rawMsg.id != null) rawMsg.id = Number(rawMsg.id);
        if (rawMsg.content == null) rawMsg.content = {};
        // v1.3: 合并 addMessage 和清空 buffer 为一次 set()，避免中间态闪烁
        const sid = Number(rawMsg.sender_id);
        if (sid) _clearPendingFor(sid);
        const isThinking = Boolean((rawMsg.content as Record<string, unknown>).thinking);
        // v19: 子代理线程消息进独立桶（右面板完整会话数据源），不混入主消息流
        const rawThread = rawMsg.thread_id != null ? Number(rawMsg.thread_id) : null;
        if (rawThread != null) {
          set((state) => {
            const bucket = state.subagentMessages[rawThread] ?? [];
            if (bucket.some((m) => m.id === rawMsg.id)) return {};
            const subStreams = { ...state.subagentStreams };
            const subThinking = { ...state.subagentThinking };
            delete subStreams[rawThread];
            delete subThinking[rawThread];
            _clearStreamDoneFor(rawThread);
            return {
              subagentMessages: { ...state.subagentMessages, [rawThread]: _appendOrdered(bucket, rawMsg) },
              subagentStreams: subStreams,
              subagentThinking: subThinking,
            };
          });
          break;
        }
        set((state) => {
          // 去重
          if (state.messages.some((m) => m.id === rawMsg.id)) return {};
          // 处理乐观消息替换
          const isUserMsg = rawMsg.sender_type === "user";
          let newMessages: MessageOut[];
          if (isUserMsg) {
            const optimisticIdx = state.messages.findIndex((m) =>
              m.sender_type === "user" &&
              m.id > 1_000_000_000_000 &&
              _sameUserContent(m.content, rawMsg.content)
            );
            if (optimisticIdx >= 0) {
              newMessages = [...state.messages];
              newMessages.splice(optimisticIdx, 1, rawMsg);
            } else {
              newMessages = _appendOrdered(state.messages, rawMsg);
            }
          } else {
            newMessages = _appendOrdered(state.messages, rawMsg);
          }
          // 清空对应 agent 的流式 buffer，与消息落库同一次渲染完成
          const newStreaming = sid ? { ...state.streamingBuffers } : state.streamingBuffers;
          const newThinking = sid ? { ...state.thinkingBuffers } : state.thinkingBuffers;
          if (sid) {
            _clearStreamDoneFor(sid); // 落库后允许同 turn 的下一段思考/正文继续流式
            if (isThinking) {
              delete newThinking[sid];
            } else {
              delete newStreaming[sid];
            }
          }
          return { messages: newMessages, streamingBuffers: newStreaming, thinkingBuffers: newThinking };
        });
        break;
      }
      case "turn.started":
        // v15: 新 turn 清空上一轮的清单与活动记录
        _clearPendingDeltas(); // v6.5: 清理上一轮残留 delta/完成标记，思考从第一个 token 实时显示
        set({ runningTurnId: Number(payload.turn_id), isRunning: true, agentActivity: {}, todos: null, todoPersisted: false });
        // v19: 重建子代理卡片（历史 turn 的子代理经 REST 恢复）
        void get().loadSessionSubagents();
        break;
      case "turn.updated": {
        const turnId = Number(payload.turn_id);
        const status = String(payload.status ?? "");
        set((s) => {
          const ended = status === "completed" || status === "failed" || status === "cancelled" || status === "interrupted" || status === "blocked" || status === "awaiting_confirmation";
          // plan turn 异常结束（未生成 plan）时清除待确认记录
          const clearPlanTurn = ended && status !== "completed" && s.pendingPlanTurn?.turnId === turnId;
          return {
            turns: s.turns.map((t) => (t.id === turnId ? { ...t, status } : t)),
            // v1.1: 停止保护窗内忽略后端残留的 running 回跳（用户点击停止后 5 秒）
            ...(status === "running"
              ? (Date.now() - _stopGuardTs > 5000
                  ? { runningTurnId: turnId, isRunning: true }
                  : {})
              : {}),
            ...(ended ? { runningTurnId: null, isRunning: false } : {}),
            ...(status === "interrupted" ? { interruptedTurnId: turnId, runningTurnId: null, isRunning: false } : {}),
            ...(clearPlanTurn ? { pendingPlanTurn: null } : {}),
          };
        });
        // v2.2: turn 异常/取消等结束态也续发排队输入（仅非运行态）
        const endedNow = ["completed", "failed", "cancelled", "interrupted", "blocked", "awaiting_confirmation"].includes(String(payload.status ?? ""));
        if (endedNow) void get()._drainQueue();
        break;
      }
      case "turn.completed": {
        const turnId = Number(payload.turn_id);
        _clearPendingDeltas();
        set((s) => {
          // v13: 任务拆分提案由 task.proposed 驱动；保留旧 /plan 弹窗兼容历史事件。
          let pendingPlan = s.pendingPlan;
          let pendingPlanTurn = s.pendingPlanTurn;
          if (pendingPlanTurn && pendingPlanTurn.turnId === turnId) {
            pendingPlan = { task: pendingPlanTurn.task };
            pendingPlanTurn = null;
          }
          return {
            turns: s.turns.map((t) => (t.id === turnId ? { ...t, status: "completed", summary: (payload.summary as string) ?? t.summary } : t)),
            runningTurnId: null, isRunning: false,
            streamingBuffers: {}, thinkingBuffers: {},  // v1.3: turn 结束兜底清空
            subagentStreams: {}, subagentThinking: {},  // v19: 子代理流式缓冲同样兜底清空
            pendingPlan,
            pendingPlanTurn,
            // v1.1: 本地摘除会话转圈标记（双保险，与 session.completed 同写法）
            sessions: s.sessions.map((x) => (x.id === s.currentSessionId ? { ...x, has_running: false } : x)),
          };
        });
        get().refreshMessages();
        get().refreshTasks();
        // v11: turn 完成后拉取变更审核清单（写盘变更 + 持久化审核状态）
        get().loadTurnChanges(turnId);
        // v2.2: turn 结束 → 自动续发排队输入
        void get()._drainQueue();
        break;
      }
      case "turn.interrupted": {
        const turnId = Number(payload.turn_id);
        set({ interruptedTurnId: turnId, runningTurnId: null, isRunning: false, streamingBuffers: {}, thinkingBuffers: {} });
        get().refreshMessages();
        break;
      }
      case "turn.rolled_back": {
        // v11: 已回滚 turn 的变更已撤销，清掉其审核卡片
        const rollbackTurnId = Number((payload as { turn_id?: unknown }).turn_id ?? 0);
        set((s) => {
          if (!rollbackTurnId || !s.turnChanges[rollbackTurnId]) return {};
          const next = { ...s.turnChanges };
          delete next[rollbackTurnId];
          return { turnChanges: next };
        });
        get().refreshMessages();
        get().refreshTurns();
        get().refreshTasks();
        break;
      }
      case "agent.completed": {
        // v19: 子代理完成 → 卡片状态 done
        const doneAid = Number(payload.agent_id ?? 0);
        if (doneAid && get().subagentMeta[doneAid]) {
          set((s) => ({ subagentMeta: { ...s.subagentMeta, [doneAid]: { ...s.subagentMeta[doneAid], status: "done" } } }));
        }
        get().refreshTasks();
        break;
      }
      case "agent.started": {
        // v19: 子代理启动 → 消息流卡片实时出现（engine 直启与 spawn_subagent 路径均广播）
        const aid = Number(payload.agent_id ?? 0);
        const kind = String(payload.kind ?? "");
        if (aid && kind === "sub") {
          const turnId = payload.turn_id != null ? Number(payload.turn_id) : null;
          const taskId = payload.task_id != null ? Number(payload.task_id) : null;
          const name = String(payload.name ?? "") || `子代理 #${aid}`;
          set((s) => ({
            subagentMeta: {
              ...s.subagentMeta,
              [aid]: { name, turnId: turnId ?? s.subagentMeta[aid]?.turnId ?? null, taskId: taskId ?? s.subagentMeta[aid]?.taskId ?? null, status: "running" },
            },
          }));
        }
        break;
      }
      case "agent.updated":
        // 子代理活动状态由 TaskSummaryPanel/工具卡消费，此处无需全局状态
        break;
      case "task.planned":
        // 计划模式产物：刷新任务与消息即可
        get().refreshTasks();
        get().refreshMessages();
        break;
      case "api.retry":
        // 模型繁忙重试提示（v2.1: 前端 Composer 上方横幅消费）
        break;
      case "config.changed":
      case "scheduled.triggered":
        // 跨会话辅助事件：当前无全局 UI 动作
        break;
      case "thinking.delta": {
        const aid = Number(payload.agent_id);
        const delta = String(payload.delta ?? "");
        const tid = payload.thread_id != null ? Number(payload.thread_id) : null;
        if (aid && delta && !_streamDoneText[`thinking:${tid ?? aid}`]) {
          _pendingThinking[aid] = (_pendingThinking[aid] || "") + delta;
          // v19: 记录 thread_id，flush 时按主/子分桶

          if (tid != null) _pendingThread[aid] = tid;
          _scheduleDeltaFlush();
        }
        break;
      }
      case "thinking.done": {
        const aid = Number(payload.agent_id);
        const full = typeof payload.full_text === "string" ? payload.full_text : null;
        if (aid && full != null) {
          const tid = payload.thread_id != null ? Number(payload.thread_id) : null;
          _clearPendingFor(aid);
          if (tid != null) set((s) => ({ subagentThinking: { ...s.subagentThinking, [tid]: full } }));
          else set((s) => ({ thinkingBuffers: { ...s.thinkingBuffers, [aid]: full } }));
          _streamDoneText[`thinking:${tid ?? aid}`] = full;
        }
        break;
      }
      case "token.delta": {
        const aid = Number(payload.agent_id);
        const delta = String(payload.delta ?? "");
        const tid = payload.thread_id != null ? Number(payload.thread_id) : null;
        if (aid && delta && !_streamDoneText[`token:${tid ?? aid}`]) {
          _pendingToken[aid] = (_pendingToken[aid] || "") + delta;

          if (tid != null) _pendingThread[aid] = tid;
          _scheduleDeltaFlush();
        }
        break;
      }
      case "token.done": {
        const aid = Number(payload.agent_id);
        const full = typeof payload.full_text === "string" ? payload.full_text : null;
        if (aid && full != null) {
          const tid = payload.thread_id != null ? Number(payload.thread_id) : null;
          _clearPendingFor(aid);
          if (tid != null) set((s) => ({ subagentStreams: { ...s.subagentStreams, [tid]: full } }));
          else set((s) => ({ streamingBuffers: { ...s.streamingBuffers, [aid]: full } }));
          _streamDoneText[`token:${tid ?? aid}`] = full;
        }
        break;
      }
      case "tool.call": {
        // v15: 记录子代理最新工具调用，任务卡片进行中步骤行内展示实时活动
        const agentId = Number(payload.agent_id ?? 0);
        const tool = String(payload.tool ?? "");
        if (agentId && tool) {
          const preview = String(payload.args_preview ?? "").replace(/\s+/g, " ").slice(0, 60);
          set((s) => ({ agentActivity: { ...s.agentActivity, [agentId]: preview ? `${tool} ${preview}` : tool } }));
        }
        break;
      }
      case "tool.result":
        // 工具调用由 message.created 落库驱动展示；此处仅触发消息刷新兜底
        break;
      case "todo.updated": {
        // v15: 模型自主维护的执行清单（todo_write）
        const items = Array.isArray(payload.todos) ? payload.todos : [];
        set({
          todos: items
            .filter((t): t is Record<string, unknown> => typeof t === "object" && t !== null)
            .map((t) => ({
              content: String(t.content ?? ""),
              activeForm: t.activeForm ? String(t.activeForm) : undefined,
              status: (["pending", "in_progress", "completed"].includes(String(t.status)) ? String(t.status) : "pending") as TodoItem["status"],
            }))
            .filter((t) => t.content),
          todoPersisted: Boolean(payload.persisted),
        });
        break;
      }
      case "task.proposed": {
        const turnId = Number(payload.turn_id ?? 0);
        const requestTaskId = Number(payload.request_task_id ?? 0);
        const groupTaskId = Number(payload.group_task_id ?? 0);
        const reasons = Array.isArray(payload.reasons) ? payload.reasons.map(String) : [];
        set({ pendingSplit: { turnId, requestTaskId, groupTaskId, reasons }, isRunning: false, runningTurnId: null });
        // 提案事件只携带轻量步骤摘要，完整字段通过 REST 拉取，避免协议中泄露执行细节。
        get().refreshTasks();
        break;
      }
      case "task.updated": {
        const taskId = Number(payload.task_id);
        const status = String(payload.status ?? "");
        const note = payload.note != null ? String(payload.note) : null;
        // v19: 子代理状态随任务状态同步（in_progress→running / done / failed / cancelled）
        if (status) {
          const mapped = status === "in_progress" ? "running"
            : status === "done" ? "done"
            : status === "failed" ? "failed"
            : status === "cancelled" ? "failed"
            : null;
          if (mapped) {
            set((s) => {
              const hit = Object.entries(s.subagentMeta).find(([, m]) => m.taskId === taskId);
              if (!hit) return {};
              const aid = Number(hit[0]);
              return { subagentMeta: { ...s.subagentMeta, [aid]: { ...s.subagentMeta[aid], status: mapped } } };
            });
          }
        }
        set((s) => {
          const exists = s.tasks.some((t) => t.id === taskId);
          if (exists) {
            return {
              tasks: s.tasks.map((t) => (t.id === taskId ? { ...t, status: status || t.status, ...(note ? { note } : {}) } : t)),
            };
          }
          return {};
        });
        // v7: 新任务（如运行中创建）实时拉取完整列表，保证任务步骤/进度即时可见
        if (!get().tasks.some((t) => t.id === taskId)) {
          get().refreshTasks();
        }
        break;
      }
      case "usage.update": {
        // v19: 圆环仅统计主代理占用——子代理 usage 不覆盖（避免多步任务数字来回跳变）
        const agentKind = String(payload.agent_kind ?? "main");
        if (agentKind === "sub") break;
        // v6.5: total 用 prompt_tokens（真实当前上下文占用），而非 total_tokens(prompt+completion)。
        // prompt_tokens 包含 system+history+tools+当前输入，是"窗口被占用了多少"的真实值。
        // 后端已将 total_tokens 字段也设为 prompt_tokens，这里取 prompt_tokens 更明确。
        const promptTokens = Number(payload.prompt_tokens ?? payload.input_tokens ?? 0);
        const detail: UsageDetail = {
          input: promptTokens,
          cached_input: Number(payload.cached_input_tokens ?? 0),
          output: Number(payload.completion_tokens ?? payload.output_tokens ?? 0),
          reasoning_output: Number(payload.reasoning_tokens ?? 0),
          total: promptTokens,
          context_window: Number(payload.context_window ?? 0),
          agent_name: String(payload.agent_name ?? "main"),
          breakdown: (payload.breakdown as Record<string, number> | undefined) ?? undefined,
        };
        set({ usage: detail });
        break;
      }
      case "compact.started": {
        // v6.5: 压缩开始，前端显示"正在压缩上下文"反馈
        set({ isCompacting: true });
        break;
      }
      case "compact.completed": {
        // v6.5: 压缩完成，关闭反馈提示
        set({ isCompacting: false });
        break;
      }
      case "approval.request":
        set({ pendingApproval: { approvalId: String(payload.approval_id ?? ""), detail: (payload.detail || {}) as Record<string, unknown> } });
        break;
      case "approval.response":
        // 审批结果广播回前端（含其他窗口），关闭本地审批横幅
        set({ pendingApproval: null });
        break;
      case "session.completed": {
        const sid = Number((payload as { session_id?: unknown }).session_id ?? 0);
        // v1.1: 本地立即摘除转圈标记，避免等待 REST
        set((s) => ({
          isRunning: false, runningTurnId: null,
          sessions: s.sessions.map((x) => (x.id === sid ? { ...x, has_running: false } : x)),
        }));
        get().refreshTurns();
        get().refreshTasks();
        break;
      }
      case "error":
        set({ error: String((payload as { message?: string }).message ?? "未知错误") });
        break;
      case "ack":
        break;
      case "sync.response": {
        const count = Number(payload.count ?? 0);
        if (count > 0) {
          _clearPendingDeltas();
          set({ streamingBuffers: {}, thinkingBuffers: {} });
          void get().refreshMessages();
          void get().refreshTurns();
          void get().refreshTasks();
        }
        break;
      }
      default: {
        // 编译期穷举检查：后端新增事件未在 events.ts 登记时此处报错
        const _exhaustive: never = ev;
        void _exhaustive;
        break;
      }
    }
    void st;
  },
}));
