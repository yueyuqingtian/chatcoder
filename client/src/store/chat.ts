/** v2 会话状态管理（zustand）：项目 / 会话 / turn 任务驱动。 */
import { create } from "zustand";
import { api } from "../api/client";
import type { ArtifactOut, AttachmentInfo, FileChangeOut, MessageOut, ModelOut, ProjectOut, ProviderOut, RollbackAffected, RollbackPreviewFile, SessionOut, TaskOut, TurnOut } from "../api/client";
import { wsClient, globalWsClient } from "../api/ws";
import type { ServerEventName } from "@chatcoder/shared/events";
import type { CompactSummaryPayload } from "@chatcoder/shared/events";

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
  /** plan-547: 点击"立即发送"后的注入中状态（等待 user_input.injected 事件确认后移除） */
  flushing?: boolean;
}

/** v42: "立即发送"注入分割标记。
 * 注入时刻 AI 恰在流式输出时，该流式段（跨界段）尚未落库、落库后 id 会大于
 * 注入消息--按 id 序渲染会掉到注入消息下方。注入时快照流式中的 agent，
 * 其当前段落库后绑定 crossoverId，渲染时该段固定前移到注入消息上方：
 * 注入消息成为时间分割点（上方=注入前内容含跨界段，下方=注入后新输出）。 */
export interface InjectMark {
  /** 注入消息所属 turn */
  turnId: number;
  /** 注入的用户消息 id（渲染分割点） */
  injectId: number;
  /** 注入时刻正在流式输出的 agent（跨界段落库绑定后清空） */
  pendingAgents: number[];
  /** 注入时刻流式中的段完成落库后的消息 id（前移到注入消息上方）；null=尚未落库 */
  crossoverId: number | null;
}

/** 浏览器引用贴条（元素标注 / 网页截图 / DOM 快照 / 控制台求值）。 */
export interface BrowserRef {
  id: string;
  kind: "element" | "screenshot" | "dom" | "console";
  url: string;
  pageTitle: string;
  selector?: string;
  bbox?: { x: number; y: number; width: number; height: number };
  styleDigest?: string;
  text?: string;
  note?: string;
  thumbUrl?: string;
  createdAt?: number;
}

/** 计划卡片持久化信息（按时间线固定展示，执行时不消失） */
export interface PlanCardInfo {
  turnId: number;
  task: string;
  planDocPath: string;
  status: "awaiting_confirmation" | "confirmed" | "cancelled" | "completed" | "superseded";
  createdAt?: number;
  /** plan-604: 锚定消息 id（方案汇报正文；缺省兜底 turn 内最后一条 AI 消息）——锚点及之前为规划段，计划卡渲染在规划段末尾 */
  anchorMsgId?: number | null;
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
  /** plan-95/v38: turnId 标记计划卡归属 turn；planDocPath 为后端广播的实际文档路径。
   *  task.proposed 与旧 /plan 流程统一由本状态渲染确认卡（不再有独立 pendingSplit）。 */
  pendingPlan: { task: string; turnId?: number; planDocPath?: string } | null;
  pendingPlanTurn: { turnId: number; task: string } | null;
  /** 计划卡片按 turnId 持久化字典：按时间线固定展示，执行与完成后不消失 */
  plansByTurn: Record<number, PlanCardInfo>;
  reviewedFiles: Record<string, boolean>;
  rollbackPending: { turnId: number; files: RollbackPreviewFile[]; affected: RollbackAffected } | null;
  turnChanges: Record<number, FileChangeOut[]>;
  todos: TodoItem[] | null;
  todoPersisted: boolean;
  agentActivity: Record<number, string>;
  queuedInputs: QueuedInput[];
  /** v42: 注入分割标记（会话级，跨 turn 在 turn.started 清空） */
  injectMarks: InjectMark[];
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
    plansByTurn: s.plansByTurn || {},
    reviewedFiles: s.reviewedFiles,
    rollbackPending: s.rollbackPending,
    turnChanges: s.turnChanges,
    todos: s.todos,
    todoPersisted: s.todoPersisted,
    agentActivity: s.agentActivity,
    queuedInputs: s.queuedInputs,
    injectMarks: s.injectMarks,
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
  models: ModelOut[];
  providers: ProviderOut[];
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
  /** v35: turn 级瞬态状态提示（重试/恢复），来自 turn.status 广播，不落库；null=无。 */
  turnStatus: string | null;
  /** v30: 压缩进度信息（compact.started 载荷，渲染"压缩中"卡片用）。 */
  compactingInfo: { usedTokens?: number; contextWindow?: number; ratio?: number } | null;
  /** v30: 最近一次压缩结果（compact.summary 载荷，压缩完成后消息流渲染摘要卡）。 */
  lastCompact: CompactSummaryPayload | null;
  /** 待审批请求。 */
  pendingApproval: { approvalId: string; detail: Record<string, unknown> } | null;
  /** 回滚/撤销后回填输入框的草稿（v40 按 key 隔离：key="home"|"new"|sessionId，
   * 仅 draftKey 匹配的 Composer 实例消费一次，避免跨会话/首页串扰）。 */
  composerBackfill: { key: string; text: string; attachments: AttachmentInfo[] } | null;
  /** 浏览器标注/截图贴条列表。 */
  composerBrowserRefs: BrowserRef[];
  /** v6/v38: 计划确认状态（方案文档生成后等待确认，含任务标题、归属 turnId 与文档路径）。 */
  pendingPlan: { task: string; turnId?: number; planDocPath?: string } | null;
  /** v7: /plan 待确认的 plan turn（旧兼容字段）。 */
  pendingPlanTurn: { turnId: number; task: string } | null;
  /** 计划卡片按 turnId 持久化字典：按时间线固定展示，执行与完成后不消失 */
  plansByTurn: Record<number, PlanCardInfo>;
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
  /** plan-546: 全局最近使用的模型 id（新空态首页默认模型 = 最近一次会话/首页所用模型）。 */
  lastModelId: number | null;
  /** v2.2 (对齐 zcode 3.8): 输入队列——运行中发送的消息排队，turn 完成后自动续发。 */
  queuedInputs: QueuedInput[];
  /** v42: 注入分割标记（见 InjectMark） */
  injectMarks: InjectMark[];
  loading: boolean;
  error: string | null;
  wsConnected: boolean;

  // 动作
  loadBootstrap: () => Promise<void>;
  loadModels: () => Promise<void>;
  createProject: (path: string, name?: string) => Promise<ProjectOut | null>;
  selectProject: (projectId: number) => Promise<void>;
  createSession: (projectId: number, title?: string, opts?: { model_id?: number | null; permission_mode?: "default" | "accept_edits" | "plan" | "readonly"; goal_text?: string | null }) => Promise<number | null>;
  switchSession: (sessionId: number, fromHist?: boolean) => Promise<void>;
  /** 会话前进/后退历史（侧栏 logo 区与折叠态标题栏共用，zcode 顶部导航箭头） */
  sessionHist: number[];
  sessionHistIdx: number;
  histGo: (dir: -1 | 1) => void;
  deleteSession: (sessionId: number) => Promise<void>;
  renameSession: (sessionId: number, title: string) => Promise<void>;
  forkSession: (sessionId: number) => Promise<void>;
/** plan-547: 返回新 turn id（null=未创建，如运行中入队/发送失败），供队列续发失败回队判断。 */
sendTurn: (content: string, attachments?: Record<string, unknown>[], reasoningEffort?: string, mode?: "readonly" | "plan" | null, modelId?: number | null) => Promise<number | null>;
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
  /** v38 (plan-482): 确认/取消方案文档（不再涉及 group/steps 编辑）。 */
  confirmPlanTurn: (accepted: boolean) => Promise<void>;
  /** v15: 重试失败/已取消的步骤。 */
  retryTask: (taskId: number) => Promise<void>;
  dismissPlan: () => void;
  respondApproval: (approvalId: string, approved: boolean, remember?: boolean, answer?: Record<string, unknown>, rememberScope?: "session" | "global") => void;
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
  setComposerDraft: (key: string, text: string) => void;
  appendComposerDraft: (text: string) => void;
  /** 添加浏览器标注引用。 */
  addComposerBrowserRef: (ref: BrowserRef) => void;
  /** 移除单个浏览器标注引用。 */
  removeComposerBrowserRef: (id: string) => void;
  /** 清空所有浏览器标注引用。 */
  clearComposerBrowserRefs: () => void;
  clearError: () => void;
  addMessage: (msg: MessageOut) => void;
  /** v2.2: 请求消息流滚动到某子代理线程首条消息（任务卡步骤穿透）。 */
  requestScrollTo: (target: { threadId?: number; turnId?: number }) => void;
  clearScrollTarget: () => void;
  /** v2.2: 更新/删除排队输入（patch=null 表示删除）。 */
  updateQueuedInput: (id: string, patch: Partial<QueuedInput> | null) => void;
  /** plan-547: 立即发送排队项——经注入 API 在运行 turn 的下次 LLM 调用前传达给 AI。 */
  flushQueuedInput: (id: string) => Promise<void>;
  /** v2.2: turn 结束后自动续发队列头（内部调用）。 */
  _drainQueue: () => Promise<void>;
  handleWs: (event: string, payload: Record<string, unknown>) => void;
  /** v37: 订阅全局状态通道（跨会话运行态/活动时间），侧栏实时更新。 */
  connectGlobalEvents: () => void;
  /** v37: 断开全局通道订阅（应用卸载时调用）。 */
  disconnectGlobalEvents: () => void;
}

let _sendingGuard = false;
let _drainingQueue = false;
let _wsUnsub: (() => void) | null = null;
/** v37: 全局通道订阅的清理函数（与 App 生命周期绑定） */
let _globalWsUnsub: (() => void) | null = null;
let _heartbeatTimer: ReturnType<typeof setTimeout> | null = null;
const HEARTBEAT_TIMEOUT = 60_000; // 60s 无事件超时兜底复位
let _stoppingTurnId: number | null = null;
/** plan-546: bootstrap 自动选中会话仅允许冷启动首次执行；
 * 此后 currentSessionId=null 表示用户主动停留空态首页（新建任务），
 * 任何后续 loadBootstrap（退出设置侧栏重挂载/设置面板刷新等）不得再把用户拉进会话。 */
let _autoSelectedOnce = false;

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
      // plan-547: 心跳判定 turn 已结束（结束事件丢失场景）后续发排队输入
      else void useChatStore.getState()._drainQueue();
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

/** v38 (plan-482): task.proposed 处理中按 request_task_id 取请求任务标题作卡片文案。 */
function _pendingPlanTaskIdToTitle(s: { tasks: TaskOut[] }, requestTaskId: number): string {
  const t = s.tasks.find((x) => x.id === requestTaskId);
  return t?.title || "任务执行计划";
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
    turnStatus: null,
    compactingInfo: null,
    lastCompact: null,
    pendingApproval: null,
    pendingPlan: null,
    pendingPlanTurn: null,
    plansByTurn: {},
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
    injectMarks: [],
  };
}

export const useChatStore = create<ChatState>((set, get) => ({
  projects: [],
  sessions: [],
  models: [],
  providers: [],
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
  turnStatus: null,
  compactingInfo: null,
  lastCompact: null,
  pendingApproval: null,
  pendingPlan: null,
  pendingPlanTurn: null,
  plansByTurn: {},
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
  injectMarks: [],
  composerBackfill: null,
  composerBrowserRefs: [],
  lastReasoningEffort: null,
  lastModelId: null,
  loading: false,
  error: null,
  wsConnected: false,

  loadModels: async () => {
    try {
      const [models, providers] = await Promise.all([
        api.listModels().catch(() => []),
        api.listProviders().catch(() => []),
      ]);
      if (models.length > 0 || providers.length > 0) {
        set({ models, providers });
      }
    } catch {}
  },

  loadBootstrap: async () => {
    set({ loading: true, error: null });
    try {
      const [projects, sessions, models, providers] = await Promise.all([
        api.listProjects(),
        api.listSessions(),
        api.listModels().catch(() => []),
        api.listProviders().catch(() => []),
      ]);
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
        models: models.length > 0 ? models : get().models,
        providers: providers.length > 0 ? providers : get().providers,
        loading: false,
        currentProjectId: currentProject?.id ?? sessionProject?.id ?? activeProjects[0]?.id ?? null,
      });
      // plan-546: 自动选中仅冷启动首次；之后 null=用户停留在空态首页，不再自动跳入会话
      if (!_autoSelectedOnce && !get().currentSessionId && activeSessions.length > 0) {
        _autoSelectedOnce = true;
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

  createSession: async (projectId, title, opts) => {
    try {
      // plan-547: 模型与权限模式在创建时一次落准（避免事后 updateSession 竞态与 UI 不同步）
      // plan-676: 首页目标同样随创建一次落准
      const session = await api.createSession({
        project_id: projectId,
        title,
        model_id: opts?.model_id ?? undefined,
        permission_mode: opts?.permission_mode ?? undefined,
        goal_text: opts?.goal_text ?? undefined,
      });
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
    // plan-546: 会话模型作为"最近使用模型"，供新空态首页默认承接
    if (session?.model_id != null) set({ lastModelId: session.model_id });

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
      // plan-547: 切回会话若已空闲且存在排队输入，立即续发（切走期间 turn 结束事件已丢失）
      if (!get().isRunning) void get()._drainQueue();
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

      // v11/v23: 恢复变更审核数据——∪(含 artifact 消息的 turn, 最近 10 个已完成 turn)。
      // 贴条需展示「从开始到现在」的未审核分组；artifact 消息精准标记有变更的历史 turn，
      // 避免为无变更 turn 发起无效请求。
      const changedTurnIds = new Set<number>();
      for (const m of messages) {
        if (m.msg_type === "artifact" && m.turn_id != null) changedTurnIds.add(m.turn_id);
      }
      for (const t of turns.filter((x) => x.status === "completed" || x.status === "awaiting_confirmation").slice(-10)) {
        changedTurnIds.add(t.id);
      }
      for (const id of changedTurnIds) {
        get().loadTurnChanges(id);
      }

      // v38 (plan-533/538): 从历史轮次中重建 plansByTurn 计划卡字典。
      // plan-624 修订: completed turn 恢复注册——普通模式写计划文档的 turn（如
      // "调研与规划已完成"）状态就是 completed，删除分支会让这些历史卡片全部消失；
      // 但 completed 仅在消息真实引用计划文档路径时注册（普通执行 turn 不误挂），
      // awaiting_confirmation/confirmed 为确认流程专属状态，路径未命中也用约定路径兜底。
      const recoveredPlans: Record<number, PlanCardInfo> = {};
      const planPathRe = /ai\/chatcoder-plan-[^\s)"'<>]*\.md/i;
      // plan-604: 方案文档路径被 AI 消息引用处即方案汇报正文——锚定该消息
      // （与 task.proposed 实时锚定同源），卡片位置刷新前后不漂移。
      // plan-624: 只认 text 消息——thinking 是内部推理，其文本也会提前引用计划
      // 文档路径（如"先更新计划文档 ai/chatcoder-plan-92.md"），命中会把锚点
      // 拉到 turn 开头，卡片插到规划段前面（紧跟「已工作」计时条）。
      // 取最后一条命中而非第一条：方案正文/执行汇报都是收尾 text，锚点落在
      // turn 末尾引用处，卡片渲染在规划段/执行内容之后，不再插到过程中间。
      const findTurnPlan = (turnId: number): { path: string; msgId: number | null } | null => {
        let out: { path: string; msgId: number } | null = null;
        for (const m of messages) {
          if (m.turn_id !== turnId || m.sender_type === "user") continue;
          if (m.msg_type === "thinking" || (m.content as Record<string, unknown> | undefined)?.thinking === true) continue;
          const text = typeof m.content?.text === "string" ? m.content.text : "";
          const hit = text.match(planPathRe);
          if (hit) out = { path: hit[0], msgId: m.id };
        }
        return out;
      };
        // plan-644: 状态映射--后端 turn.plan_status 为真值源（全生命周期
        // proposed/confirmed/done/cancelled/superseded）；旧数据无该字段时
        // 沿用 turn.status 推断。plan_doc_path 主路径（全生命周期卡片恢复，
        // 含执行完成与被新方案取代的轮次），旧数据回退正则+约定名兜底。
        const planStatusToCard: Record<string, PlanCardInfo["status"]> = {
          proposed: "awaiting_confirmation",
          confirmed: "confirmed",
          done: "confirmed",
          cancelled: "cancelled",
          superseded: "superseded",
        };
      for (const t of turns) {
        const found = findTurnPlan(t.id);
        const planDocPath = t.plan_doc_path || found?.path
          || (t.status === "awaiting_confirmation" || t.status === "confirmed"
              ? `ai/chatcoder-plan-${sessionId}-${t.id}.md`
              : null);
        if (!planDocPath) continue;
        const planMsgId = found?.msgId ?? null;
        const reqTask = tasks.find((tk) => tk.turn_id === t.id && tk.kind === "request");
        // 锚点兜底：消息中未见路径引用（默认约定路径命中）时退回 turn 内最后一条 text 消息
        // （plan-624: 同样只认 text，与 findTurnPlan 口径一致）
        const anchorMsgId = planMsgId ?? messages.reduce<number | null>((acc, m) => (
          m.turn_id === t.id && m.sender_type !== "user"
          && m.msg_type === "text" && (m.content as Record<string, unknown> | undefined)?.thinking !== true
          && (acc == null || m.id > acc) ? m.id : acc
        ), null);
        recoveredPlans[t.id] = {
          turnId: t.id,
          task: reqTask?.title || t.summary || "任务执行计划",
          planDocPath,
          status: (t.plan_status && planStatusToCard[t.plan_status]) || (t.status === "awaiting_confirmation" ? "awaiting_confirmation" : "confirmed"),
          createdAt: t.started_at ? new Date(t.started_at).getTime() : Date.now(),
          anchorMsgId,
        };
      }
      if (Object.keys(recoveredPlans).length > 0) {
        set((s) => ({ plansByTurn: { ...recoveredPlans, ...s.plansByTurn } }));
      }

      // 如果有运行中的 turn，启动心跳超时兜底（§9.1 #3）
      if (running) _startHeartbeat();
      // v19: 重建子代理卡片（历史会话刷新后可点击进右面板）
      void get().loadSessionSubagents();
      // plan-547: 切回会话若已空闲且存在排队输入，立即续发
      if (!get().isRunning) void get()._drainQueue();
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

  sendTurn: async (content, attachments, reasoningEffort, mode, modelId) => {
    const { currentSessionId, isRunning } = get();
    if (!currentSessionId) return null;
    // 空态新会话可能在 WS 建连前就收到首条消息，先本地投影标题，避免等待事件丢失。
    const currentSession = get().sessions.find((x) => x.id === currentSessionId);
    if (currentSession && !currentSession.title && get().messages.length === 0 && content.trim()) {
      const title = content.trim().replace(/\n/g, " ").slice(0, 30);
      set((s) => ({ sessions: s.sessions.map((x) => (x.id === currentSessionId ? { ...x, title } : x)) }));
    }
    // v14: 支持「只发附件不带文字」——只要 content 或 attachments 有其一即可发送
    const hasText = Boolean(content && content.trim());
    const hasAtts = Array.isArray(attachments) && attachments.length > 0;
    if (!hasText && !hasAtts) return null;
    // v2.2: 输入队列——运行中发送的消息进入队列，当前 turn 完成后自动续发
    if (_sendingGuard) {
      // 并发保护（如 turn 结束事件连续触发）：放回队头不丢失
      set((s) => ({
        queuedInputs: [{
          id: `q-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
          content, attachments, reasoningEffort, mode,
        }, ...s.queuedInputs],
      }));
      return null;
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
      return null;
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
    // v26/v38: 新消息开始 = 旧方案提案失效（但旧计划卡片仍保留在 plansByTurn 时间线上，不被随意覆盖）
    const prevPending = get().pendingPlan;
    set((s) => ({
      isRunning: true,
      pendingPlan: null,
      pendingPlanTurn: null,
      ...(prevPending?.turnId != null && s.plansByTurn[prevPending.turnId]
        ? {
            plansByTurn: {
              ...s.plansByTurn,
              [prevPending.turnId]: {
                ...s.plansByTurn[prevPending.turnId],
                // 若之前未确认，标记为已调整/取消；已确认/已完成的保持原状态不变
                status: s.plansByTurn[prevPending.turnId].status === "awaiting_confirmation" ? "cancelled" : s.plansByTurn[prevPending.turnId].status,
              },
            },
          }
        : {}),
      sessions: s.sessions.map((x) => (x.id === currentSessionId
        ? { ...x, has_running: true, last_activity_at: new Date().toISOString() }
        : x)),
    }));
    _startHeartbeat();
    try {
      const turn = await api.createTurn({ session_id: currentSessionId, content, attachments, reasoning_effort: reasoningEffort, mode, model_id: modelId ?? undefined });
      _clearPendingDeltas(); // v6.5: 新 turn 清掉上一轮残留的完成标记，保证思考/正文从头实时流式
      set((s) => ({
        turns: [...s.turns, turn],
        runningTurnId: turn.id,
        isRunning: true,
        interruptedTurnId: null,
        // 发送消息后，乐观将当前会话置为运行中转圈状态
        sessions: s.sessions.map((x) => (x.id === currentSessionId ? { ...x, has_running: true } : x)),
        // v7: /plan 不再立即弹确认框——记录待确认 plan turn，
        // 等后端真正生成 plan 文档并 turn.completed 后才弹出确认弹窗
        ...(mode === "plan" ? { pendingPlanTurn: { turnId: turn.id, task: content } } : {}),
      }));
      _startHeartbeat();
      return turn.id;
    } catch (e) {
      // v23: 发送失败回退乐观运行态（左侧转圈同步摘除）
      _clearHeartbeat();
      set((s) => ({
        error: String(e), isRunning: false, runningTurnId: null, pendingPlan: null, pendingPlanTurn: null,
        sessions: s.sessions.map((x) => (x.id === currentSessionId ? { ...x, has_running: false } : x)),
      }));
      return null;
    } finally {
      _sendingGuard = false;
    }
  },

  confirmPlan: async (task) => {
    const { currentSessionId } = get();
    if (!currentSessionId || !task.trim()) return;
    // 确认执行 = 授权完全访问：先切换会话权限再发送执行 turn，避免 plan 权限拦截写盘
    try {
      await api.updateSession(currentSessionId, { permission_mode: "accept_edits" });
      set((s) => ({ sessions: s.sessions.map((x) => (x.id === currentSessionId ? { ...x, permission_mode: "accept_edits" } : x)) }));
    } catch { /* 权限切换失败不阻断发送 */ }
    const pending = get().pendingPlan;
    if (pending?.turnId != null) {
      const tid = pending.turnId;
      set((s) => ({
        plansByTurn: {
          ...s.plansByTurn,
          [tid]: {
            ...s.plansByTurn[tid],
            turnId: tid,
            task: pending.task,
            planDocPath: pending.planDocPath || s.plansByTurn[tid]?.planDocPath || "",
            status: "confirmed",
          },
        },
      }));
    }
    set({ pendingPlan: null });
    // v42: 计划确认执行 → 输入框显式切回完全访问（唯一允许的自动切换，其余情况不再自动改模式）
    if (typeof window !== "undefined") {
      window.dispatchEvent(new CustomEvent("chatcoder:composer-mode", { detail: { mode: "default" } }));
    }
    await get().sendTurn(task);
  },

  dismissPlan: () => {
    const pending = get().pendingPlan;
    if (pending?.turnId != null) {
      const tid = pending.turnId;
      set((s) => ({
        plansByTurn: {
          ...s.plansByTurn,
          [tid]: {
            ...s.plansByTurn[tid],
            turnId: tid,
            task: pending.task,
            planDocPath: pending.planDocPath || s.plansByTurn[tid]?.planDocPath || "",
            status: "cancelled",
          },
        },
      }));
    }
    set({ pendingPlan: null, pendingPlanTurn: null });
    const { currentSessionId } = get();
    if (currentSessionId != null) {
      const slice = _snapshotSlice(get());
      set((s) => ({ sessionState: { ...s.sessionState, [currentSessionId]: slice } }));
    }
  },

  /** v38 (plan-482): 确认/取消方案文档（不再涉及 group/steps）。 */
  confirmPlanTurn: async (accepted) => {
    const pending = get().pendingPlan;
    if (!pending || pending.turnId == null) return;
    const tid = pending.turnId;
    // 更新 plansByTurn 对应卡片的状态为 confirmed 或 cancelled，保留卡片在时间线上
    set((s) => ({
      plansByTurn: {
        ...s.plansByTurn,
        [tid]: {
          ...s.plansByTurn[tid],
          turnId: tid,
          task: pending.task,
          planDocPath: pending.planDocPath || s.plansByTurn[tid]?.planDocPath || "",
          status: accepted ? "confirmed" : "cancelled",
        },
      },
      pendingPlan: null,
      pendingPlanTurn: null,
    }));
    const { currentSessionId } = get();
    if (currentSessionId != null) {
      const slice = _snapshotSlice(get());
      set((s) => ({ sessionState: { ...s.sessionState, [currentSessionId]: slice } }));
    }
    try {
      const res = await api.confirmPlanTurn(pending.turnId, { accepted });
      const pm = res?.permission_mode;
      if (pm === "accept_edits" || pm === "plan" || pm === "readonly" || pm === "default") {
        if (currentSessionId != null) {
          set((s) => ({ sessions: s.sessions.map((x) => (x.id === currentSessionId ? { ...x, permission_mode: pm } : x)) }));
        }
      }
      // v42: 计划「确认执行」→ 输入框显式切回完全访问（唯一允许的自动切换，其余情况不再自动改模式）
      if (accepted && typeof window !== "undefined") {
        window.dispatchEvent(new CustomEvent("chatcoder:composer-mode", { detail: { mode: "default" } }));
      }
      await get().refreshTasks();
      if (!accepted) await get().refreshTurns();
    } catch (e) {
      const msg = String(e);
      if (msg.includes("已处理或不在待确认状态")) {
        await get().refreshTasks();
        await get().refreshTurns();
      } else {
        set({ error: msg });
      }
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

  respondApproval: (approvalId, approved, remember = false, answer, rememberScope = "session") => {
    // v2.2 (对齐 zcode 3.12/3.14): remember=true 生成"始终允许"规则；
    // v3.0 (plan-88): rememberScope 区分会话级/全局规则（session / global）
    // answer 为 ask_user_question 的结构化回答
    wsClient.send("approval.response", {
      approval_id: approvalId, approved, remember,
      remember_scope: rememberScope,
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
    const { runningTurnId } = get();
    if (!runningTurnId || _stoppingTurnId === runningTurnId) return;
    const turnId = runningTurnId;
    _stoppingTurnId = turnId;
    _clearHeartbeat();
    set((s) => ({
      runningTurnId: null,
      isRunning: false,
      interruptedTurnId: turnId,
      turns: s.turns.map((t) => (t.id === turnId ? { ...t, status: "interrupted" } : t)),
      streamingBuffers: {},
      thinkingBuffers: {},
    }));
    void api.cancelTurn(turnId).catch(() => { /* 后端可能已结束 */ });
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
      // v2.2: 后端原样返回 content.attachments，结构即 AttachmentInfo；校验 file_id 后回填
      const restoredAttachments: AttachmentInfo[] = Array.isArray(result.user_attachments)
        ? result.user_attachments
            .filter((a): boolean => Boolean(a && typeof (a as { file_id?: unknown }).file_id === "string"))
            .map((a) => a as unknown as AttachmentInfo)
        : [];
      const [messages, turns, tasks] = await Promise.all([
        api.listSessionMessages(currentSessionId),
        api.listTurns(currentSessionId),
        api.listSessionTasks(currentSessionId),
      ]);
      set({
        messages, turns, tasks,
        isRunning: false, runningTurnId: null,
        // 回填原文与附件到「本会话」输入框，供用户修改后重发（v40: 按 key 隔离，不串扰首页/其它会话）
        ...(restoreToComposer && (result.user_message || restoredAttachments.length > 0)
          ? {
              composerBackfill: {
                key: String(currentSessionId),
                text: result.user_message ?? "",
                attachments: restoredAttachments,
              },
            }
          : {}),
      });
      // v38: 回滚完成后同步快照到 sessionState 分桶，避免切走再切回时闪现已撤销消息
      const slice = _snapshotSlice(get());
      set((s) => ({ sessionState: { ...s.sessionState, [currentSessionId]: slice } }));
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
      const msg = String(e);
      // plan-95: 快照缺失（历史数据/极端竞态）转为可理解提示，不裸抛「该 turn 无快照」
      if (msg.includes("无快照")) {
        set({ error: "该消息暂无可回滚的内容（快照缺失），可直接修改后重新发送" });
      } else {
        set({ error: msg });
      }
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
            // 终态单向不可逆：本地已 done/failed 不被 REST 滞后的 running/pending 回退
            status: (prev?.status === "done" || prev?.status === "failed") && (it.status === "running" || it.status === "pending")
              ? prev.status
              : (prev?.status === "running" && it.status === "pending" ? "running" : (it.status || prev?.status || "running")),
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
      // v38 (plan-482): 系统不再预拆分（无 proposed group），任务卡由 todo_write 清单驱动
      set({ tasks });
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

  // v40: 回填通道按 key（"home"/"new"/sessionId）隔离——仅目标输入框实例消费一次
  setComposerDraft: (key, text) => set({ composerBackfill: { key, text, attachments: [] } }),
  appendComposerDraft: (text) => {
    const toAppend = (text || "").trim();
    if (!toAppend) return;
    const sid = get().currentSessionId;
    const key = sid != null ? String(sid) : "home";
    const prev = get().composerBackfill;
    const base = prev && prev.key === key ? prev : { key, text: "", attachments: [] as AttachmentInfo[] };
    const merged = base.text.trim() ? `${base.text.trim()}\n\n${toAppend}` : toAppend;
    set({ composerBackfill: { ...base, text: merged } });
    if (typeof window !== "undefined") {
      window.dispatchEvent(new CustomEvent("chatcoder:focus-composer"));
    }
  },
  addComposerBrowserRef: (ref) => set((s) => ({ composerBrowserRefs: [...s.composerBrowserRefs, ref] })),
  removeComposerBrowserRef: (id) => set((s) => ({ composerBrowserRefs: s.composerBrowserRefs.filter((r) => r.id !== id) })),
  clearComposerBrowserRefs: () => set({ composerBrowserRefs: [] }),
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

  /** plan-547: 立即发送——注入运行中的 turn（下次 LLM 调用前传达）；
   * 无运行 turn 时等效出队直发；注入失败保留队列走 turn 结束续发兜底。 */
  flushQueuedInput: async (id) => {
    const st = get();
    const q = st.queuedInputs.find((x) => x.id === id);
    if (!q || q.flushing) return;
    if (st.runningTurnId == null) {
      set((s) => ({ queuedInputs: s.queuedInputs.filter((x) => x.id !== id) }));
      await get().sendTurn(q.content, q.attachments, q.reasoningEffort, q.mode);
      return;
    }
    set((s) => ({
      queuedInputs: s.queuedInputs.map((x) => (x.id === id ? { ...x, flushing: true } : x)),
    }));
    try {
      await api.injectTurnInput(st.runningTurnId, {
        request_id: q.id,
        content: q.content,
        attachments: q.attachments,
      });
      // 注入成功由 user_input.injected 事件从队列移除（避免双发）
    } catch (e) {
      set((s) => ({
        queuedInputs: s.queuedInputs.map((x) => (x.id === id ? { ...x, flushing: false } : x)),
      }));
      set({ error: `立即发送失败：${String(e)}（已保留队列，任务结束后自动发送）` });
    }
  },

  _drainQueue: async () => {
    if (_drainingQueue) return;
    _drainingQueue = true;
    try {
      // plan-547: 循环 drain——发送成功后 isRunning=true 自然退出；
      // 发送失败（返回 null）时队头放回并停止，等待下次触发，消息不丢。
      for (;;) {
        const { queuedInputs, isRunning, currentSessionId, pendingPlan } = get();
        // 计划等待确认不是结束态，不能提前消费后续消息。
        if (!currentSessionId || isRunning || pendingPlan || _sendingGuard) return;
        const next = queuedInputs[0];
        if (!next) return;
        set((s) => ({ queuedInputs: s.queuedInputs.slice(1) }));
        const turnId = await get().sendTurn(next.content, next.attachments, next.reasoningEffort, next.mode);
        if (turnId == null) {
          set((s) => ({ queuedInputs: [next, ...s.queuedInputs] }));
          return;
        }
      }
    } finally {
      _drainingQueue = false;
    }
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
          // v42: 注入分割标记（见 InjectMark）——注入的用户消息快照流式中的 agent；
          // 该流式段（思考/正文）落库时绑定 crossoverId，渲染层据此做时间分割
          let newMarks = state.injectMarks;
          if (isUserMsg && state.isRunning && rawMsg.turn_id != null && rawMsg.turn_id === state.runningTurnId) {
            // 注入判定：本条之前该 turn 已有用户消息（首条为触发消息，后续为注入）
            const hadUser = state.messages.some(
              (m) => m.sender_type === "user" && m.turn_id === state.runningTurnId,
            );
            const pendingAgents = [
              ...Object.entries(state.streamingBuffers).filter(([, v]) => v).map(([k]) => Number(k)),
              ...Object.entries(state.thinkingBuffers).filter(([, v]) => v).map(([k]) => Number(k)),
            ];
            if (hadUser && pendingAgents.length > 0) {
              newMarks = [...state.injectMarks, {
                turnId: state.runningTurnId,
                injectId: rawMsg.id,
                pendingAgents,
                crossoverId: null,
              }];
            }
          } else if (!isUserMsg && sid) {
            // AI 思考/正文落库 -> 若存在注入时刻流式中的 pending 标记，本条即跨界段
            const mt = String(rawMsg.msg_type ?? "");
            const isStreamMsg = mt === "text" || mt === "thinking" || isThinking;
            if (state.isRunning && rawMsg.turn_id === state.runningTurnId && isStreamMsg
              && state.injectMarks.some((mk) => mk.crossoverId == null && mk.pendingAgents.includes(sid))) {
              newMarks = state.injectMarks.map((mk) =>
                mk.crossoverId == null && mk.pendingAgents.includes(sid)
                  ? { ...mk, crossoverId: rawMsg.id, pendingAgents: [] }
                  : mk,
              );
            }
          }
          return {
            messages: newMessages,
            streamingBuffers: newStreaming,
            thinkingBuffers: newThinking,
            injectMarks: newMarks,
          };
        });
        break;
      }
      case "turn.started": {
        const startedTurnId = Number(payload.turn_id);
        const activeSid = Number(payload.session_id ?? 0) || get().currentSessionId;
        // v2.2: 只让"当前运行 turn 或更新的 turn"接管运行态——断线补发/迟到事件中
        // 旧 turn 的 turn.started 不得覆盖新 turn（同一会话内 turn id 单调递增）。
        const takesOver = (s: ChatState) => s.runningTurnId == null || startedTurnId >= s.runningTurnId;
        if (takesOver(get())) _clearPendingDeltas(); // v6.5: 清理上一轮残留 delta/完成标记，思考从第一个 token 实时显示
        set((s) => {
          // 旧 turn 的迟到 turn.started 不接管运行态，也不清当前 turn 的清单/活动记录
          if (!takesOver(s)) return {};
          return {
            // v15: 新 turn 清空上一轮的清单与活动记录
            runningTurnId: startedTurnId,
            isRunning: true,
            agentActivity: {},
            todos: null,
            todoPersisted: false,
            // v35: 新 turn 开始时清掉上一轮残留的重试状态提示
            turnStatus: null,
            // v42: 新 turn 清空上一 turn 的注入分割标记（渲染回归纯 id 序）
            injectMarks: [],
            // v26: 新 turn 开始 = 旧方案提案失效，隐藏旧方案卡片（task.proposed 后再展示新卡片）
            pendingPlan: null,
            pendingPlanTurn: null,
            sessions: s.sessions.map((x) => (x.id === activeSid ? { ...x, has_running: true } : x)),
          };
        });
        // v19: 重建子代理卡片（历史 turn 的子代理经 REST 恢复）
        void get().loadSessionSubagents();
        // v23: 新 turn 开始即刷新任务列表——request 任务/新拆分步骤立刻进入任务摘要与贴条
        void get().refreshTasks();
        break;
      }
      case "user_input.injected": {
        // plan-547: 注入成功确认——按 request_id 移除排队项，turn 结束后不再续发同一条
        const injectedReqId = String(payload.request_id ?? "");
        if (injectedReqId) {
          set((s) => ({ queuedInputs: s.queuedInputs.filter((q) => q.id !== injectedReqId) }));
        }
        break;
      }
      case "turn.updated": {
        const turnId = Number(payload.turn_id);
        const status = String(payload.status ?? "");
        const activeSid = Number(payload.session_id ?? 0) || get().currentSessionId;
        set((s) => {
          const ended = status === "completed" || status === "failed" || status === "cancelled" || status === "interrupted" || status === "blocked" || status === "awaiting_confirmation";
          if (ended && _stoppingTurnId === turnId) _stoppingTurnId = null;
          // plan turn 异常结束（未生成 plan）时清除待确认记录
          const clearPlanTurn = ended && status !== "completed" && s.pendingPlanTurn?.turnId === turnId;
          // v2.2: 仅当结束的 turn 就是当前运行中的 turn（或当前无运行 turn）时才复位运行态。
          // 迟到/断线补发的旧 turn 结束事件不得清掉新 turn 的运行态（否则出现
          // "会话已显示完成态、实际消息流还在刷新"的错乱）。
          const clearsRunning = ended && (s.runningTurnId == null || s.runningTurnId === turnId);
          return {
            turns: s.turns.map((t) => (t.id === turnId
              ? { ...t, status, ...(ended && !t.completed_at ? { completed_at: new Date().toISOString() } : {}) }
              : t)),
            // v35: turn 结束时清掉残留的重试状态提示
            ...(ended && s.turnStatus != null ? { turnStatus: null } : {}),
            // 停止请求完成前忽略后端残留的 running 回跳，避免按钮再次恢复为运行态。
            ...(status === "running" && _stoppingTurnId !== turnId && (s.runningTurnId == null || s.runningTurnId === turnId)
              ? {
                  runningTurnId: turnId,
                  isRunning: true,
                  sessions: s.sessions.map((x) => (x.id === activeSid ? { ...x, has_running: true } : x)),
                }
              : {}),
            ...(clearsRunning
              ? {
                  runningTurnId: null,
                  isRunning: false,
                  sessions: s.sessions.map((x) => (x.id === activeSid ? { ...x, has_running: false } : x)),
                }
              : {}),
            ...(status === "interrupted"
              ? { interruptedTurnId: turnId, ...(clearsRunning ? { runningTurnId: null, isRunning: false } : {}) }
              : {}),
            ...(clearPlanTurn ? { pendingPlanTurn: null } : {}),
          };
        });
        // v2.2: turn 异常/取消等结束态也续发排队输入（仅非运行态）
        const endedNow = ["completed", "failed", "cancelled", "interrupted", "blocked"].includes(String(payload.status ?? ""));
        if (endedNow) void get()._drainQueue();
        // v26: plan 模式 turn 完成（awaiting_confirmation，方案文档已写盘）也刷新变更清单，
        // 否则输入框上方"文件变更"贴条缺失方案文档（非 git 仓库同样适用）。
        if (endedNow || status === "awaiting_confirmation") {
          void get().loadTurnChanges(turnId);
        }
        break;
      }
      case "turn.completed": {
        const turnId = Number(payload.turn_id);
        const turnCompletedSid = Number(payload.session_id ?? 0) || get().currentSessionId;
        // v2.2: 仅当前运行 turn 完成时才清残留 delta（迟到/旧 turn 的 completed 不干扰新 turn 流式）
        if (get().runningTurnId == null || get().runningTurnId === turnId) _clearPendingDeltas();
        set((s) => {
          // v38 (plan-482): 方案文档已由 task.proposed 驱动；遗留 plan turn 结束时
          // 仅清理 pendingPlanTurn 标记（不再有 proposed group 兜底）。
          let pendingPlan = s.pendingPlan;
          let pendingPlanTurn = s.pendingPlanTurn;
          if (pendingPlanTurn && pendingPlanTurn.turnId === turnId) {
            pendingPlanTurn = null;
          }
          // v2.2: 仅当完成的就是当前运行 turn（或当前无运行 turn）才复位运行态/清流式缓冲，
          // 旧 turn 的迟到 completed（断线补发/队列场景）不得串到新 turn 上。
          const clearsRunning = s.runningTurnId == null || s.runningTurnId === turnId;
          return {
            turns: s.turns.map((t) => (t.id === turnId
              ? {
                  ...t,
                  status: "completed",
                  summary: (payload.summary as string) ?? t.summary,
                  completed_at: t.completed_at ?? new Date().toISOString(),
                }
              : t)),
            ...(clearsRunning
              ? {
                  runningTurnId: null, isRunning: false,
                  streamingBuffers: {}, thinkingBuffers: {},  // v1.3: turn 结束兜底清空
                  subagentStreams: {}, subagentThinking: {},  // v19: 子代理流式缓冲同样兜底清空
                  // v1.1/v37: 本地摘除会话转圈标记（按事件携带的 session_id 精确复位，切会话不串）
                  sessions: s.sessions.map((x) => (x.id === turnCompletedSid ? { ...x, has_running: false } : x)),
                }
              : {}),
            pendingPlan,
            pendingPlanTurn,
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
        // v2.2: 仅当被中断的就是当前运行 turn 才复位运行态（迟到事件同理不串扰）
        const clearsRunning = get().runningTurnId == null || get().runningTurnId === turnId;
        set((s) => ({
          interruptedTurnId: turnId,
          turns: s.turns.map((t) => (t.id === turnId
            ? { ...t, status: "interrupted", completed_at: t.completed_at ?? new Date().toISOString() }
            : t)),
          ...(clearsRunning ? { runningTurnId: null, isRunning: false, streamingBuffers: {}, thinkingBuffers: {} } : {}),
        }));
        get().refreshMessages();
        get().refreshTasks();
        void get().loadSessionSubagents();
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
      case "turn.status": {
        // v35: turn 级瞬态状态（重试/恢复提示），流式状态行展示；text 为空串表示清除
        const statusText = typeof payload.text === "string" ? payload.text : null;
        set({ turnStatus: statusText && statusText.length > 0 ? statusText : null });
        break;
      }
      case "config.changed":
      case "scheduled.triggered":
        // 跨会话辅助事件：当前无全局 UI 动作
        break;
      case "thinking.delta": {
        const aid = Number(payload.agent_id);
        const delta = String(payload.delta ?? "");
        const tid = payload.thread_id != null ? Number(payload.thread_id) : null;
        // v35: 流式恢复 = 重试已成功，自动清除状态行提示
        if (get().turnStatus != null) set({ turnStatus: null });
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
        // v35: 流式恢复 = 重试已成功，自动清除状态行提示
        if (get().turnStatus != null) set({ turnStatus: null });
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
      case "file.change": {
        // v24: 写盘实时广播——立即拉取该 turn 最新变更清单（含持久化审核状态），
        // 使输入框贴条"文件变更"在任务执行期间实时刷新。
        const changeTurnId = Number(payload.turn_id ?? 0);
        if (changeTurnId) void get().loadTurnChanges(changeTurnId);
        break;
      }
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
        // v38 (plan-482): 事件仅表示"方案文档已生成、等待用户确认"，
        // 统一走 pendingPlan（含文档路径），由 PlanCard 渲染确认卡。
        const turnId = Number(payload.turn_id ?? 0);
        const planDocPath = payload.plan_doc_path != null ? String(payload.plan_doc_path) : undefined;
        set((s) => {
          const clearsRunning = s.runningTurnId == null || s.runningTurnId === turnId;
          const taskTitle = _pendingPlanTaskIdToTitle(s, Number(payload.request_task_id ?? 0));
          // plan-547/624: 锚定到该 turn 最后一条 text 消息（方案汇报正文），计划卡紧跟其后渲染。
          // 只认 text 不认 thinking——思考文本也会引用计划路径，会把锚点拉到 turn 开头、
          // 卡片插到规划段开头（与历史恢复 findTurnPlan 口径一致）。
          const anchorMsgId = s.messages.reduce<number | null>((acc, m) => (
            m.turn_id === turnId && m.sender_type !== "user"
            && m.msg_type === "text" && (m.content as Record<string, unknown> | undefined)?.thinking !== true
            && (acc == null || m.id > acc) ? m.id : acc
          ), null);
          const planInfo: PlanCardInfo = {
            turnId,
            task: taskTitle,
            planDocPath: planDocPath || `ai/chatcoder-plan-${s.currentSessionId}.md`,
            status: "awaiting_confirmation",
            createdAt: Date.now(),
            anchorMsgId,
          };
          return {
            pendingPlan: { task: taskTitle, turnId, planDocPath },
            plansByTurn: { ...s.plansByTurn, [turnId]: planInfo },
            ...(clearsRunning ? { isRunning: false, runningTurnId: null } : {}),
            turns: s.turns.map((t) => (t.id === turnId ? { ...t, status: "awaiting_confirmation" } : t)),
          };
        });
        if (turnId) void get().loadTurnChanges(turnId);
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
        // v30: 记录触发占用信息，消息流顶部渲染"压缩中"卡片
        set({
          isCompacting: true,
          compactingInfo: {
            usedTokens: Number(payload.used_tokens ?? 0) || undefined,
            contextWindow: Number(payload.context_window ?? 0) || undefined,
            ratio: Number(payload.ratio ?? 0) || undefined,
          },
        });
        break;
      }
      case "compact.summary": {
        // v30: 压缩落库完成，记录阴影定价结果（供压缩完成卡片展示）
        set({ lastCompact: payload as CompactSummaryPayload });
        break;
      }
      case "compact.completed": {
        // v6.5: 压缩完成，关闭反馈提示；v30: 刷新消息流拿到 SUMMARY checkpoint 消息
        set((s) => ({
          isCompacting: false,
          compactingInfo: null,
          // compact.summary 事件可能先于 completed 到达；completed 兜底记录结果
          lastCompact: s.lastCompact ?? ((payload as Partial<CompactSummaryPayload>).compaction_id ? payload as CompactSummaryPayload : null),
        }));
        void get().refreshMessages();
        break;
      }
      case "approval.request":
        set({ pendingApproval: { approvalId: String(payload.approval_id ?? ""), detail: (payload.detail || {}) as Record<string, unknown> } });
        break;
      case "approval.response":
        // 审批结果广播回前端（含其他窗口），关闭本地审批横幅
        set({ pendingApproval: null });
        break;
      case "session.updated": {
        const sid = Number(payload.session_id ?? 0);
        const title = typeof payload.title === "string" ? payload.title : null;
        // v2.2 (plan-88): 执行结束后后端恢复 plan 模式 → 同步 permission_mode，
        // ComposerCore 据此把输入框切回「计划模式」。
        const pm = payload.permission_mode;
        const permissionMode: "plan" | "default" | "accept_edits" | "readonly" | null =
          pm === "plan" || pm === "default" || pm === "accept_edits" || pm === "readonly" ? pm : null;
        if (sid > 0 && (title || permissionMode)) {
          set((s) => ({
            sessions: s.sessions.map((x) => (x.id === sid
              ? { ...x, ...(title ? { title } : {}), ...(permissionMode ? { permission_mode: permissionMode } : {}) }
              : x)),
          }));
        }
        break;
      }
      case "session.completed": {
        const sid = Number((payload as { session_id?: unknown }).session_id ?? 0);
        // v1.1: 本地立即摘除转圈标记，避免等待 REST
        set((s) => {
          // v2.2: 仅当完成的就是当前会话才复位视图运行态——
          // 后台会话的 session.completed 只更新其列表标记，不串扰当前会话。
          const isCurrent = sid === s.currentSessionId;
          return {
            ...(isCurrent ? { isRunning: false, runningTurnId: null } : {}),
            sessions: s.sessions.map((x) => (x.id === sid ? { ...x, has_running: false } : x)),
          };
        });
        // 仅当前会话需要立即刷新 turn/task（后台会话切回时由 switchSession 的 refresh 补齐）
        if (sid === get().currentSessionId) {
          get().refreshTurns();
          get().refreshTasks();
        }
        void api.getSession(sid).then((updated) => {
          set((s) => ({ sessions: s.sessions.map((x) => (x.id === sid ? updated : x)) }));
        }).catch(() => { /* 标题刷新失败不影响已完成的会话 */ });
        break;
      }
      case "goal.updated":
      case "goal.completed":
      case "goal.continued":
      case "goal.stopped": {
        // plan-671: 目标模式事件（会话通道，作用于当前会话）——
        // goal.continued 后随后的 turn.started 接管运行态，此处只同步目标状态。
        const sid = get().currentSessionId;
        if (sid == null) break;
        const goalStatus = String(payload.status ?? "");
        const turnsUsed = Number(payload.turns_used ?? 0);
        set((s) => ({
          sessions: s.sessions.map((x) => (x.id === sid
            ? {
                ...x,
                ...(goalStatus === "active" || goalStatus === "completed" || goalStatus === "cancelled"
                  ? { goal_status: goalStatus } : {}),
                ...(typeof payload.text === "string" ? { goal_text: payload.text } : {}),
                ...(payload.turns_used != null ? { goal_turns_used: turnsUsed } : {}),
              }
            : x)),
        }));
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

  connectGlobalEvents: () => {
    if (_globalWsUnsub) return;
    globalWsClient.connect();
    _globalWsUnsub = globalWsClient.on((ev) => {
      const p = ev.payload as Record<string, unknown>;
      const sid = Number(p.session_id ?? 0);
      if (!sid) return;
      const event = ev.event as ServerEventName;
      if (event === "session.completed") {
        const ts = typeof p.last_activity_at === "string" ? p.last_activity_at : null;
        set((s) => {
          if (!s.sessions.some((x) => x.id === sid)) return {};
          return {
            sessions: s.sessions.map((x) => (x.id === sid
              ? { ...x, has_running: false, ...(ts ? { last_activity_at: ts } : {}) }
              : x)),
            ...(s.currentSessionId === sid ? { isRunning: false, runningTurnId: null } : {}),
          };
        });
        return;
      }
      if (event === "session.updated") {
        const title = typeof p.title === "string" ? p.title : null;
        const ts = typeof p.last_activity_at === "string" ? p.last_activity_at : null;
        const pm = p.permission_mode;
        const permissionMode = pm === "plan" || pm === "default" || pm === "accept_edits" || pm === "readonly" ? pm : null;
        if (!title && !ts && !permissionMode) return;
        set((s) => {
          if (!s.sessions.some((x) => x.id === sid)) return {};
          return {
            sessions: s.sessions.map((x) => (x.id === sid
              ? { ...x, ...(title ? { title } : {}), ...(ts ? { last_activity_at: ts } : {}),
                  ...(permissionMode ? { permission_mode: permissionMode } : {}) }
              : x)),
          };
        });
        return;
      }
      if (event === "message.created") {
        const msg = p.msg as Record<string, unknown> | undefined;
        const ts = typeof msg?.created_at === "string" ? msg.created_at : null;
        if (!ts) return;
        set((s) => {
          if (!s.sessions.some((x) => x.id === sid)) return {};
          return {
            sessions: s.sessions.map((x) => (x.id === sid ? { ...x, last_activity_at: ts } : x)),
          };
        });
      }
    });
  },

  disconnectGlobalEvents: () => {
    if (_globalWsUnsub) {
      _globalWsUnsub();
      _globalWsUnsub = null;
    }
    globalWsClient.disconnect();
  },
}));
