/** v2 会话状态管理（zustand）：项目 / 会话 / turn 任务驱动。 */
import { create } from "zustand";
import { api } from "../api/client";
import { ApiError } from "../api/client";
import type { ArtifactOut, ArthasEntryOut, AttachmentInfo, ComposerRefOut, DebugStatusOut, FileChangeOut, MergeReportOut, MessageOut, ModelOut, ProjectOut, ProviderOut, RollbackAffected, RollbackPreviewFile, SessionOut, TaskOut, TurnOut } from "../api/client";
import { wsClient, globalWsClient } from "../api/ws";
import { normalizeApprovalMode, normalizeDraftMode } from "./drafts";
import { isBusy } from "../perf/bus";
import { registerReconcileTask, RECONCILE_ORDER } from "../perf/reconcile";
import type { ServerEventName } from "@chatcoder/shared/events";
import type { CompactSummaryPayload } from "@chatcoder/shared/events";

/** v7: 全局最近选择思考深度的 localStorage 持久化 key——重启后仍能恢复上次选择 */
const LAST_REASONING_KEY = "chatcoder.lastReasoningEffort";

function loadLastReasoning(): string | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = localStorage.getItem(LAST_REASONING_KEY);
    return raw || null;
  } catch {
    return null;
  }
}

function saveLastReasoning(value: string | null) {
  if (typeof window === "undefined") return;
  try {
    if (value) localStorage.setItem(LAST_REASONING_KEY, value);
    else localStorage.removeItem(LAST_REASONING_KEY);
  } catch {
    /* localStorage 不可用时静默忽略，仅影响重启后默认档位回退 */
  }
}

/** v7: 导出——ComposerCore 在 changeReasoning/发送时持久化全局最近思考深度 */
export function persistLastReasoning(value: string | null) {
  saveLastReasoning(value);
}

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

/** v7(H): 运行中工具结果实时视图（tool.result 事件投影，含 change_stat） */
export interface RunningToolResult {
  ok: boolean;
  output_preview?: string;
  change_stat?: { path: string; additions: number; deletions: number };
  duration_ms?: number;
}

/** v2.2: 排队输入项（运行中发送的消息进入队列，turn 完成后自动续发）。
 *  plan-230-1144 M2: mode 放宽为 string——自定义权限模式名需随队列项透传。 */
export interface QueuedInput {
  id: string;
  content: string;
  attachments?: Record<string, unknown>[];
  reasoningEffort?: string;
  mode?: string | null;
  /** plan-547: 点击"立即发送"后的注入中状态（等待 user_input.injected 事件确认后移除） */
  flushing?: boolean;
}

/** plan-41-227: 原 v42「注入分割标记」（InjectMark）已整体移除。
 *
 *  它在注入消息（"立即发送"）落库时快照当时流式中的 agent，等该段落库后由渲染层
 *  前移到注入消息上方做时间分割；实测造成位置两跳（落库瞬间上跳、turn 结束回落到 id 序）。
 *  现在注入消息的时间线位置**只由落库 id 序决定**（后端广播已带 turn_id）。 */

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
  /** 完整上传结果：发送时必须转为消息附件，否则标注/截图内容不会传给 AI。 */
  attachment?: AttachmentInfo;
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
/** plan-330-1648 M7: 子代理元信息（agentId → 名称/归属 turn/状态/错误），卡片与面板共用。 */
export interface SubagentMeta {
  name: string;
  turnId: number | null;
  taskId: number | null;
  status: string;
  /** v36 (plan-321-1600 M2): 面板头部展示——起止时间（算用时）、变更文件数、token 用量 */
  startedAt?: string | null;
  endedAt?: string | null;
  filesCount?: number;
  tokens?: number;
  /** plan-330-1648 M7: 失败/取消原因——主消息流卡片与子代理面板据此展示（不再只有一个红叉） */
  error?: string | null;
}

/** plan-75-332: 审批卡「解释」状态。
 *  按 approvalId 键控（与 questionDraft 同法）——切换页面/会话后返回仍可恢复；
 *  approval.request 到达时重置，审批结束/取消时清空。 */
export interface ApprovalExplainState {
  approvalId: string;
  status: "idle" | "loading" | "streaming" | "done" | "error";
  text: string;
  error: string | null;
  /** 实际使用的模型与思考深度（后端在 done 事件回传，供用户核对配置是否生效） */
  model?: string;
  reasoningEffort?: string;
}

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
  /** plan-282-1441（#8）：调试现场（按 web/java 分桶，随会话 slice 保存/恢复） */
  debugState: Record<string, DebugStatusOut> | null;
  pendingApproval: { approvalId: string; detail: Record<string, unknown> } | null;
  /** plan-238-1188: 提问作答草稿随会话 slice 一起保存/恢复，切走再回来不丢。 */
  questionDraft: { approvalId: string; stepIndex: number; answers: Record<string, string> } | null;
  /** plan-75-332: 审批卡解释内容（同样随切片保存/恢复）。 */
  approvalExplain: ApprovalExplainState | null;
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
  /** plan-282-1492：AI 清单所属 turn——判定"这份清单还活着吗"的判据（随会话分桶保存/恢复）。 */
  todosTurnId: number | null;
  todoPersisted: boolean;
  agentActivity: Record<number, string>;
  queuedInputs: QueuedInput[];
  /** v967: 切换瞬间尚未 flush 到 streaming/thinking 缓冲的增量尾巴（不随切走丢失） */
  pendingStreamDeltas: Record<number, string>;
  pendingThinkingDeltas: Record<number, string>;
  pendingThreads: Record<number, number>;
  /** plan-282-1421（第10项）：会话累计缓存统计（用于"平均缓存命中率"） */
  usageCacheTotals?: UsageCacheTotals;
  /** plan-330-1648 M6: 子代理面板数据随会话切片保存/恢复（切走再切回不丢面板内容）。 */
  subagentMessages: Record<number, MessageOut[]>;
  subagentStreams: Record<number, string>;
  subagentThinking: Record<number, string>;
  subagentMeta: Record<number, SubagentMeta>;
  /** v39: 后台子代理运行数——主会话保持“运行中”与「等待子代理结束…」提示的数据源。 */
  pendingSubagents: number;
}

/** plan-282-1421：会话累计缓存统计。
 *  会话级 usage 接口把 cached_input 写死为 0，只有运行时 usage.update 带真实值，
 *  因此由前端按会话累加，才能给出"实时命中率 + 平均命中率"两个口径。 */
export interface UsageCacheTotals {
  /** 累计输入 token（含缓存命中的部分） */
  inputSum: number;
  /** 累计缓存命中 token */
  cachedSum: number;
  /** 累计样本数（每次 usage.update 计一次，用于判断"是否有足够样本"） */
  samples: number;
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
    debugState: s.debugState,
    usageCacheTotals: s.usageCacheTotals,
    pendingApproval: s.pendingApproval,
    questionDraft: s.questionDraft,
    approvalExplain: s.approvalExplain,
    pendingPlan: s.pendingPlan,
    pendingPlanTurn: s.pendingPlanTurn,
    plansByTurn: s.plansByTurn || {},
    reviewedFiles: s.reviewedFiles,
    rollbackPending: s.rollbackPending,
    turnChanges: s.turnChanges,
    todos: s.todos,
    todosTurnId: s.todosTurnId,
    todoPersisted: s.todoPersisted,
    agentActivity: s.agentActivity,
    queuedInputs: s.queuedInputs,
    pendingStreamDeltas: { ..._pendingToken },
    pendingThinkingDeltas: { ..._pendingThinking },
    pendingThreads: { ..._pendingThread },
    // plan-330-1648 M6: 子代理面板数据（消息桶/流式缓冲/元信息）随切片保存——
    // 此前未纳入，切走再切回会发现子代理面板内容丢失。
    subagentMessages: s.subagentMessages,
    subagentStreams: s.subagentStreams,
    subagentThinking: s.subagentThinking,
    subagentMeta: s.subagentMeta,
    pendingSubagents: s.pendingSubagents,
  };
}

/** v967: 切回会话时把快照里的增量尾巴写回模块级 pending，并调度一次 flush 合并进缓冲。 */
function _restorePendingDeltas(slice: SessionSlice) {
  _pendingToken = { ...(slice.pendingStreamDeltas || {}) };
  _pendingThinking = { ...(slice.pendingThinkingDeltas || {}) };
  _pendingThread = { ...(slice.pendingThreads || {}) };
  _scheduleDeltaFlush();
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
  /** plan-282-1421（第10项）：会话累计缓存统计（"平均缓存命中率"数据源）。
   *  每次 usage.update（主代理）累加 input/cached，样本数用于判断是否值得展示。 */
  usageCacheTotals: UsageCacheTotals;
  /** v6.5: 是否正在压缩上下文（用于页面反馈）。 */
  isCompacting: boolean;
  /** plan-282-1441（#8）：调试现场状态（按 web / java 分桶，来自 debug.paused 事件）。
   *  调试面板与消息流卡片据此展示"停在哪一行 + 调用栈 + 变量"。 */
  debugState: Record<string, DebugStatusOut> | null;
  /** Arthas 现场诊断状态（来自 arthas.event 广播）：会话摘要 + 观测命中流水。
   *  右侧「调试」面板据此实时展示"AI 正在观测什么、看到了什么"。 */
  arthasState: {
    attached: boolean;
    pid?: number | null;
    http_port?: number | null;
    version?: string | null;
    main_class?: string;
    summary?: string;
    /** 最近命中的观测记录（倒序，面板顶部最新） */
    entries: ArthasEntryOut[];
  } | null;
  /** v35: turn 级瞬态状态提示（重试/恢复），来自 turn.status 广播，不落库；null=无。 */
  turnStatus: string | null;
  /** v30: 压缩进度信息（compact.started 载荷）。v42: 消息流不再渲染统计卡片，
   *  仅保留事件状态（保留字段以便后续面板复用/回归，不再抛给消息流尾部）。 */
  compactingInfo: { usedTokens?: number; contextWindow?: number; ratio?: number } | null;
  /** v30: 最近一次压缩结果（compact.summary 载荷，压缩完成后消息流渲染摘要卡）。 */
  lastCompact: CompactSummaryPayload | null;
  /** 待审批请求。 */
  pendingApproval: { approvalId: string; detail: Record<string, unknown> } | null;
  /** plan-238-1188: AI 提问向导的作答草稿（按 approvalId 键控）。
   *  提问卡渲染在 ComposerCore 内部（本地 state），切到设置页会卸载组件、
   *  已选答案随之丢失；上移到 store 后返回原会话可完整恢复。
   *  生命周期：approval.request 置 null（新提问）→ 作答过程中持续写回 →
   *  approval.response / respondApproval / cancelTurn 清空。 */
  questionDraft: { approvalId: string; stepIndex: number; answers: Record<string, string> } | null;
  /** plan-75-332: 审批卡「解释」状态（骨架 → 流式填充），按 approvalId 键控。
   *  生命周期：explainApproval 置 loading → delta 累加 → done/error 终态 →
   *  approval.request / approval.response / respondApproval / cancelTurn 清空。 */
  approvalExplain: ApprovalExplainState | null;
  /** 回滚/撤销后回填输入框的草稿（v40 按 key 隔离：key="home"|"new"|sessionId，
   * 仅 draftKey 匹配的 Composer 实例消费一次，避免跨会话/首页串扰）。 */
  composerBackfill: {
    key: string; text: string; attachments: AttachmentInfo[];
    /** S16（plan-41-197）：撤回回填的引用 chips（@文件 / $技能 / 连接器 / 插件）。
     *  此前只回填正文与附件，撤回后引用样式丢失（用户反馈）。 */
    refs?: Array<{ kind: string; value: string; label: string }>;
  } | null;
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
  /** plan-282-1492: 清单所属 turnId（todo.updated 载荷）——清单是否仍"活着"靠它判定。 */
  todosTurnId: number | null;
  /** v15: 清单是否已持久化到任务区块（已持久化时由任务卡片展示，内嵌卡片隐藏）。 */
  todoPersisted: boolean;
  /** v15: 子代理实时活动（agentId -> 最新工具调用摘要，来自 tool.call 事件）。 */
  agentActivity: Record<number, string>;
  /** v7(B): 运行中命令/工具的实时输出（call_key -> 累积文本，来自 tool.output 事件） */
  runningToolOutput: Record<string, string>;
  /** v7(H): 运行中工具结果实时视图（call_key -> 摘要，来自 tool.result 事件，含 change_stat） */
  runningToolResults: Record<string, RunningToolResult>;
  /** v19: 子代理元信息（agentId -> 名称/turn/任务/状态）——消息流子代理卡片数据源。 */
  subagentMeta: Record<number, SubagentMeta>;
  /** v39: 后台子代理运行数（>0 且主 turn 未运行时显示「等待子代理结束…」并保持会话转圈）。 */
  pendingSubagents: number;
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
  loading: boolean;
  /** 系统级错误（后端连接失败 / 启动加载失败 / 配置读写失败）→ 右上角 Toast。
   *  任务执行/会话运行类错误不得写这里（plan-308-1542 需求1）：它们只进 flowError。 */
  error: string | null;
  /** 任务执行/会话运行类错误 → **只**在消息流末尾渲染错误卡（不弹右上角 Toast）。
   *  来源：sendTurn 失败、turn.failed、计划确认失败、重试/回滚/审查失败等。 */
  flowError: { text: string; turnId: number | null; at: number } | null;
  /** plan-41-228：已被末尾错误卡「关闭」的 turn——该 turn 内落的 ERROR 消息不再渲染，
   *  避免同一条错误关闭后又以另一种样式冒出来。内存态：切换会话 / 刷新即清空。 */
  hiddenTurnErrors: number[];
  /** plan-308-1542 需求3-A：AI 自动合并的实时进度（merge.progress 广播累积）。
   *  仅在合并弹窗内渲染（像消息流一样实时追加行），done 时带汇总报告。 */
  mergeProgress: {
    mergeId: string | null;
    running: boolean;
    lines: Array<{ phase: string; text: string; at: number; ok?: boolean | null; path?: string | null; tool?: string | null }>;
    report: MergeReportOut | null;
  } | null;
  wsConnected: boolean;
  /** plan-278-1391: 添加同路径已归档项目时的提示（前端弹「恢复并打开」确认框）。 */
  archivedProjectPrompt: { projectId: number; name: string; path: string } | null;

  // 动作
  loadBootstrap: () => Promise<void>;
  loadModels: () => Promise<void>;
  createProject: (path: string, name?: string) => Promise<ProjectOut | null>;
  /** plan-278-1391: 恢复被归档项目并选中（确认「恢复并打开」后调用）。 */
  restoreArchivedProject: (projectId: number) => Promise<void>;
  /** plan-278-1391: 关闭归档项目提示（用户取消）。 */
  dismissArchivedProjectPrompt: () => void;
  selectProject: (projectId: number) => Promise<void>;
  createSession: (projectId: number, title?: string, opts?: { model_id?: number | null; permission_mode?: string; approval_mode?: string; goal_text?: string | null }) => Promise<number | null>;
  switchSession: (sessionId: number, fromHist?: boolean) => Promise<void>;
  /** 会话前进/后退历史（侧栏 logo 区与折叠态标题栏共用，zcode 顶部导航箭头） */
  sessionHist: number[];
  sessionHistIdx: number;
  histGo: (dir: -1 | 1) => void;
  deleteSession: (sessionId: number) => Promise<void>;
  renameSession: (sessionId: number, title: string) => Promise<void>;
  forkSession: (sessionId: number) => Promise<void>;
/** plan-547: 返回新 turn id（null=未创建，如运行中入队/发送失败），供队列续发失败回队判断。 */
sendTurn: (content: string, attachments?: Record<string, unknown>[], reasoningEffort?: string, mode?: string | null, modelId?: number | null, refs?: ComposerRefOut[]) => Promise<number | null>;
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
  /** v38 (plan-482): 确认/取消方案文档（不再涉及 group/steps 编辑）。
   *  turnId 可选：重启/历史恢复的计划卡没有 pendingPlan，需显式传入
   *  才能操作（否则按钮点击被静默短路，点了没反应）。 */
  confirmPlanTurn: (accepted: boolean, turnId?: number) => Promise<void>;
  /** v15: 重试失败/已取消的步骤。 */
  retryTask: (taskId: number) => Promise<void>;
  /** 取消方案（落库版）：= confirmPlanTurn(false)，重启后卡片按钮不复活。 */
  dismissPlan: (turnId?: number) => void;
  respondApproval: (approvalId: string, approved: boolean, remember?: boolean, answer?: Record<string, unknown>, rememberScope?: "session" | "global") => void;
  /** plan-238-1188: 提问向导作答草稿写回（null 清空）。 */
  setQuestionDraft: (draft: { approvalId: string; stepIndex: number; answers: Record<string, string> } | null) => void;
  /** plan-75-332: 请求 AI 解释当前审批项（骨架先显示，内容流式填充）。 */
  explainApproval: (approvalId: string) => void;
  /** plan-75-332: 清空解释状态（审批结束/取消/卡片卸载时调用）。 */
  resetApprovalExplain: () => void;
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
  /** plan-308-1542 需求1：把任务执行类错误投递到消息流（附带归属 turnId，可为 null）。 */
  setFlowError: (text: string, turnId?: number | null) => void;
  /** plan-308-1542 需求1：清除消息流错误卡（新一轮开始 / 用户关闭）。 */
  clearFlowError: () => void;
  /** plan-41-228：关闭末尾错误卡时联动隐藏对应 turn 内的 ERROR 消息（同一条错误只呈现一次）。 */
  dismissTurnError: (turnId: number | null) => void;
  /** plan-308-1542 需求3-A：写入/追加 AI 合并进度（merge.progress 事件消费点）。 */
  applyMergeProgress: (payload: Record<string, unknown>) => void;
  /** 重置合并进度（打开合并弹窗时）。 */
  resetMergeProgress: () => void;
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
const _stoppingTurnIds = new Set<number>();

/** v38: 统一的会话运行态变更。
 *
 * `running_started_at` 是侧栏「执行中任务」的排序键（最新开始执行的在上面）。
 * 它必须与 has_running 同步维护，且**运行期间保持首次置位的值不变**——否则每次
 * 事件都刷新时间戳，并发任务又会互相超车。置位时若已有值则沿用（幂等）。
 */
function setSessionRunning(x: SessionOut, running: boolean): SessionOut {
  if (running) {
    return {
      ...x,
      has_running: true,
      running_started_at: x.running_started_at || new Date().toISOString(),
    };
  }
  return { ...x, has_running: false, running_started_at: null };
}

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
/** plan-334-1661 S2：运行中工具的实时输出增量（call_key -> 文本）。
 *  原先每个 chunk 直接 set()，高频输出时每 chunk 一次 React 提交；现并入同一帧合流。 */
let _pendingToolOutput: Record<string, string> = {};
// v19: agentId -> threadId（子代理流式内容分桶到 subagentStreams/subagentThinking）
let _pendingThread: Record<number, number> = {};
let _streamDoneText: Record<string, string> = {};
let _flushScheduled = false;
// v967: 切回会话后短暂窗口（1s）内忽略 sync 重放的 delta 增量，避免旧增量累加到已恢复的缓冲上。
let _replayGuardUntil = 0;

function _clearPendingDeltas() {
  _pendingToken = {};
  _pendingThinking = {};
  _pendingThread = {};
  _pendingToolOutput = {};
  _streamDoneText = {};
}

/** plan-334-1661 S2：是否还有待 flush 的增量——高负载降级期据此决定是否继续排 rAF，避免空转。 */
function _hasPendingDelta(): boolean {
  return Object.keys(_pendingToken).length > 0
    || Object.keys(_pendingThinking).length > 0
    || Object.keys(_pendingToolOutput).length > 0;
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

/** 几何运动期判据：统一走 PerfBus（plan-329-1647 S3）。
 *  三个来源——主进程窗口运动（拖窗口 / 最大化补间 / 边缘 resize）、分隔条拖拽、
 *  面板折叠过渡——在 bus 里合并为单一 `isBusy()`，口径与 MessageFlow / TerminalPanel /
 *  ComposerCore / StreamingText 完全一致（此前这几处各读各的 DOM，且多数不认
 *  `panel-animating`）。
 *
 *  为何要门控 flush（问题2「深度前端性能优化」的核心矛盾）：几何变化的每一帧都要
 *  整树重排+绘制，而流式 flush 每帧一次 setState 会触发 markdown 重解析与虚拟列表
 *  重测——两者抢同一份帧预算，叠加时直接吃穿 16ms，表现为"任务执行期间切全屏/
 *  拖面板宽度时鼠标卡顿抖动"。运动期（通常 1~2 秒）暂停 flush，pending 在模块级
 *  继续累积，静止后的下一帧一次性追平：文字最多晚到几十毫秒，换来操作全程跟手。 */
function _geometryBusy(): boolean {
  try { return isBusy(); } catch { return false; }
}

/** S10b（plan-329-1647）：flush 高负载分级。
 *  流式高峰时每帧一次 setState 会与几何动画 / 消息流渲染抢帧预算；当上一次 flush 的
 *  同步耗时超过预算（FLUSH_COST_BUDGET_MS）时，把后续 flush 间隔放宽到约 30fps
 *  （HIGH_LOAD_GAP_MS），保持 HIGH_LOAD_HOLD_MS 后自动恢复逐帧；期间若 pending 已清空
 *  则立即停表，不做 rAF 空转。几何运动期的"完全暂停 + 结束追平"语义不变（见 _geometryBusy）。 */
let _lastFlushAt = 0;
let _highLoadUntil = 0;
const FLUSH_COST_BUDGET_MS = 6;
const HIGH_LOAD_GAP_MS = 33;
const HIGH_LOAD_HOLD_MS = 600;
let _flushWaitListening = false;
function _scheduleDeltaFlush() {
  if (_flushScheduled) return;
  _flushScheduled = true;
  const tick = () => {
    if (_geometryBusy()) {
      // 运动期间不做每帧 rAF 空转：pending 留在模块缓冲，只注册一次结束监听。
      if (!_flushWaitListening) {
        _flushWaitListening = true;
        const settle = () => {
          if (_geometryBusy()) return;
          window.removeEventListener("chatcoder:window-motion", onMotion);
          window.removeEventListener("pointerup", onPointerUp, true);
          window.removeEventListener("chatcoder:panel-drag-end", onPanelDragEnd);
          _flushWaitListening = false;
          requestAnimationFrame(tick);
        };
        const onMotion = (e: Event) => {
          if ((e as CustomEvent<{ active?: boolean }>).detail?.active === false) settle();
        };
        const onPointerUp = () => settle();
        const onPanelDragEnd = () => settle();
        window.addEventListener("chatcoder:window-motion", onMotion);
        window.addEventListener("pointerup", onPointerUp, true);
        window.addEventListener("chatcoder:panel-drag-end", onPanelDragEnd);
      }
      return;
    }
    // S10b：高负载降级期降低 flush 频率（逐帧 → 约 30fps）；无待 flush 内容时停表。
    const nowTs = performance.now();
    if (nowTs < _highLoadUntil && nowTs - _lastFlushAt < HIGH_LOAD_GAP_MS) {
      if (_hasPendingDelta()) {
        requestAnimationFrame(tick);
      } else {
        _flushScheduled = false;
      }
      return;
    }
    _flushScheduled = false;
    const tok = _pendingToken;
    const thk = _pendingThinking;
    const thr = _pendingThread;
    const tpo = _pendingToolOutput;
    _pendingToken = {};
    _pendingThinking = {};
    _pendingThread = {};
    _pendingToolOutput = {};
    const tokKeys = Object.keys(tok);
    const thkKeys = Object.keys(thk);
    const tpoKeys = Object.keys(tpo);
    if (tokKeys.length === 0 && thkKeys.length === 0 && tpoKeys.length === 0) return;
    const t0 = performance.now();
    useChatStore.setState((s) => {
      const next: Partial<ChatState> = {};
      // plan-334-1661 S2：运行中工具的实时输出与 token/thinking 共用同一次提交
      if (tpoKeys.length > 0) {
        const runningOutput = { ...s.runningToolOutput };
        for (const k of tpoKeys) runningOutput[k] = (runningOutput[k] || "") + tpo[k];
        next.runningToolOutput = runningOutput;
      }
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
    _lastFlushAt = performance.now();
    if (_lastFlushAt - t0 > FLUSH_COST_BUDGET_MS) _highLoadUntil = _lastFlushAt + HIGH_LOAD_HOLD_MS;
  };
  requestAnimationFrame(tick);
}

// RFL-6（S6）：注册进唯一收敛序列——order 80 流式追平。
// 收敛序列在 rAF 内按序执行，此处只需“叫一次调度”：_scheduleDeltaFlush 自身会合并并
// 在一帧内落库（见其注释），因此不会与其它收尾任务抢帧。
registerReconcileTask("chat-stream-flush", RECONCILE_ORDER.streamFlush,
  "流式缓冲追平", () => _scheduleDeltaFlush());

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

/** v967: 合并「快照已有消息」与「DB 拉取的最新消息」：按 id 去重、乐观消息 content 匹配替换、保持升序。 */
function _mergeMessages(existing: MessageOut[], fetched: MessageOut[]): MessageOut[] {
  const out = new Map<number, MessageOut>();
  // DB 权威结果优先；仅纳入已确认 id（≤1e12）
  for (const m of fetched) {
    const id = Number(m.id) || 0;
    if (id > 0 && id <= 1e12) out.set(id, m);
  }
  for (const m of existing) {
    const id = Number(m.id) || 0;
    if (id > 1e12) {
      // 乐观消息（user 占位）：尝试按 content 匹配 DB 中真实 user 消息替换
      const replaced = fetched.find((fm) =>
        fm.sender_type === "user" && _sameUserContent(fm.content, m.content));
      if (replaced) {
        const rid = Number(replaced.id) || 0;
        if (rid > 0) out.set(rid, replaced);
      } else {
        out.set(id, m); // 后端尚未落库：保留乐观占位
      }
    } else if (!out.has(id)) {
      out.set(id, m); // 未在 DB 结果的已确认消息（去重兜底）
    }
  }
  return [...out.values()].sort((a, b) => (Number(a.id) || 0) - (Number(b.id) || 0));
}

/** plan-41-198: 快照刷新的安全合并——DB 结果为准，但保留「请求发起后才在本地出现」的消息。
 *
 *  背景（用户反馈「排队消息自动发送后不显示，重新进会话又出现」）：
 *  turn.completed 先发 refreshMessages（DB 快照），紧接着 _drainQueue 续发排队消息
 *  （乐观插入本地 + WS 落库回填）。快照若在续发落库前取到，响应一到整体覆盖
 *  就把刚插入的消息抹掉——只有切走再回（走只增合并）才重新出现。
 *
 *  baseIds = 请求发起时本地已确认消息（id ≤ 1e12）的 id 集合，用于区分两种情况：
 *  · 不在 baseIds 且 DB 结果里没有 ⇒ 请求期间新增（乐观消息或刚落库），保留；
 *  · 在 baseIds 但 DB 结果里没有 ⇒ 确已删除（回滚等），丢弃——不可退化为只增不减。
 */
function _mergeRefreshed(existing: MessageOut[], fetched: MessageOut[], baseIds: Set<number>): MessageOut[] {
  const out = new Map<number, MessageOut>();
  for (const m of fetched) {
    const id = Number(m.id) || 0;
    if (id > 0 && id <= 1e12) out.set(id, m);
  }
  for (const m of existing) {
    const id = Number(m.id) || 0;
    if (id > 1e12) {
      // 乐观占位：DB 已落库则替换为真实消息，否则保留等待落库
      const replaced = fetched.find((fm) => fm.sender_type === "user" && _sameUserContent(fm.content, m.content));
      if (replaced && Number(replaced.id) > 0) out.set(Number(replaced.id), replaced);
      else out.set(id, m);
    } else if (!out.has(id) && !baseIds.has(id)) {
      out.set(id, m); // 请求期间新增（快照尚未包含）：保留，避免被覆盖丢失
    }
  }
  return [...out.values()].sort((a, b) => (Number(a.id) || 0) - (Number(b.id) || 0));
}

/** v967: cached 切回时刷新 messages（DB 最新），与快照去重合并——补齐切走期间已落库但未收到广播的消息。 */
async function refreshAndMergeMessages(sessionId: number): Promise<void> {
  const cur = useChatStore.getState();
  if (cur.currentSessionId !== sessionId) return;
  let fetched: MessageOut[] = [];
  try {
    fetched = await api.listSessionMessages(sessionId);
  } catch {
    return; // 拉取失败不阻塞切换（保留快照）
  }
  const now = useChatStore.getState();
  if (now.currentSessionId !== sessionId) return;
  useChatStore.setState({ messages: _mergeMessages(now.messages, fetched), loading: false });
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
    debugState: null,
    arthasState: null,
    // plan-308-1542 需求1：切换会话时清空消息流错误卡（避免串到别的会话）
    flowError: null,
    hiddenTurnErrors: [],
    // 合并进度属弹窗级瞬态，切会话一并清空
    mergeProgress: null,
    turnStatus: null,
    compactingInfo: null,
    lastCompact: null,
    pendingApproval: null,
    questionDraft: null,
    approvalExplain: null,
    pendingPlan: null,
    pendingPlanTurn: null,
    plansByTurn: {},
    reviewedFiles: {},
    rollbackPending: null,
    turnChanges: {},
    todos: null,
    todosTurnId: null,
    todoPersisted: false,
    agentActivity: {},
    runningToolOutput: {},
    runningToolResults: {},
    subagentMeta: {},
    subagentMessages: {},
    subagentStreams: {},
    subagentThinking: {},
    pendingSubagents: 0,
    scrollTarget: null,
    queuedInputs: [],
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
  debugState: null,
  arthasState: null,
  usageCacheTotals: { inputSum: 0, cachedSum: 0, samples: 0 },
  turnStatus: null,
  compactingInfo: null,
  lastCompact: null,
  pendingApproval: null,
  questionDraft: null,
  approvalExplain: null,
  pendingPlan: null,
  pendingPlanTurn: null,
  plansByTurn: {},
  reviewedFiles: {},
  rollbackPending: null,
  turnChanges: {},
  todos: null,
  todosTurnId: null,
  todoPersisted: false,
  agentActivity: {},
  runningToolOutput: {},
  runningToolResults: {},
  subagentMeta: {},
  subagentMessages: {},
  subagentStreams: {},
  subagentThinking: {},
  pendingSubagents: 0,
  scrollTarget: null,
  queuedInputs: [],
  composerBackfill: null,
  composerBrowserRefs: [],
  lastReasoningEffort: loadLastReasoning(),
  lastModelId: null,
  loading: false,
  error: null,
  flowError: null,
  hiddenTurnErrors: [],
  mergeProgress: null,
  wsConnected: false,
  /** plan-278-1391: 同路径项目已归档提示（默认无）。 */
  archivedProjectPrompt: null,

  loadModels: async () => {
    // allSettled：单边请求失败时保留旧值；成功的空结果必须生效，
    // 否则「删除/停用最后一个模型」后选择器仍展示陈旧列表。
    const [modelsRes, providersRes] = await Promise.allSettled([api.listModels(), api.listProviders()]);
    set({
      models: modelsRes.status === "fulfilled" ? modelsRes.value : get().models,
      providers: providersRes.status === "fulfilled" ? providersRes.value : get().providers,
    });
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
      // 清理已归档或已删除的旧项目 ID。
      const activeProjects = projects.filter((p) => !p.archived);
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
      // 用户要求：启动后默认停留空态首页（新建任务），不自动进入最近会话
      if (activeProjects.length > 0 && !get().currentProjectId) {
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
      // plan-278-1391: 同路径项目已归档 → 不做泛红报错，改为弹「恢复并打开」提示。
      // 后端返回 409 + detail{code:"project_archived", project_id, name}。
      if (e instanceof ApiError && (e.status === 409 || e.code === "project_archived")) {
        const d = (e.detail ?? {}) as { project_id?: number; name?: string; path?: string };
        const pid = Number(d.project_id ?? 0);
        if (pid > 0) {
          set({
            archivedProjectPrompt: {
              projectId: pid,
              name: String(d.name ?? ""),
              path: String(d.path ?? path),
            },
            error: null,
          });
          return null;
        }
      }
      set({ error: String(e) });
      return null;
    }
  },

  /** plan-278-1391: 恢复被归档项目并选中（用户确认「恢复并打开」）。 */
  restoreArchivedProject: async (projectId) => {
    try {
      await api.updateProject(projectId, { archived: false });
      set({ archivedProjectPrompt: null });
      await get().loadBootstrap();
      await get().selectProject(projectId);
    } catch (e) {
      set({ error: String(e), archivedProjectPrompt: null });
    }
  },

  /** plan-278-1391: 关闭归档项目提示（用户取消）。 */
  dismissArchivedProjectPrompt: () => set({ archivedProjectPrompt: null }),

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
      // plan-75-332: 权限模式 approval_mode 一并落准
      const session = await api.createSession({
        project_id: projectId,
        title,
        model_id: opts?.model_id ?? undefined,
        permission_mode: opts?.permission_mode ?? undefined,
        approval_mode: opts?.approval_mode ?? undefined,
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
        // v967: 恢复流式缓冲显示"正在恢复"；后续流式/落库事件会清除该状态提示
        if ((cached.streamingBuffers && Object.keys(cached.streamingBuffers).length > 0) ||
            (cached.thinkingBuffers && Object.keys(cached.thinkingBuffers).length > 0)) {
          set({ turnStatus: "正在恢复会话…" });
        }
      }
      void get().refreshTurns();
      void get().refreshTasks();
      // v967: 恢复切走瞬间未 flush 的增量尾巴（避免最后一段流式内容丢失）
      _restorePendingDeltas(cached);
      // v967: 短暂窗口内忽略 sync 重放的旧 delta 增量，避免重复累加到已恢复的缓冲
      _replayGuardUntil = Date.now() + 1000;
      // v967: 主动拉取 DB 最新消息并与快照去重合并——补齐切走期间已落库但未收到广播的消息
      void refreshAndMergeMessages(sessionId);
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
      // plan-282-1492: 首次加载（含重启进入）恢复"运行中"必须启动心跳兜底。
      // 此前只有缓存命中路径才启动心跳——若 DB 里那条 running 是上次异常退出遗留的
      // （后端已重启自愈 / turn 结束事件早已丢失），前端就会永远停在"执行中"，
      // 胶囊一直显示最后一步在跑。心跳 60s 无事件即 refreshTurns 纠偏。
      if (running) _startHeartbeat();

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
      // plan-865: 取第一条命中而非最后一条——执行汇报引用路径会把锚点推到 turn
      // 末尾导致卡片沉底；第一条命中即方案汇报位置（仅服务无 PLAN 消息的旧会话
      // 兜底，新会话由数据库 PLAN 消息按时间线渲染）。
      const findTurnPlan = (turnId: number): { path: string; msgId: number | null } | null => {
        let out: { path: string; msgId: number } | null = null;
        for (const m of messages) {
          if (m.turn_id !== turnId || m.sender_type === "user") continue;
          if (m.msg_type === "thinking" || (m.content as Record<string, unknown> | undefined)?.thinking === true) continue;
          const text = typeof m.content?.text === "string" ? m.content.text : "";
          const hit = text.match(planPathRe);
          if (hit && !out) { out = { path: hit[0], msgId: m.id }; break; }
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
        // plan-238-1188: 归属校验——卡片只挂到"真正产生该计划"的轮次。
        // 此前只要轮次正文出现过 plan 路径（例如执行汇报里写"已按
        // ai/chatcoder-plan-<sid>-1171.md 执行"）就给该轮登记计划卡，
        // 于是后续轮次（尤其上下文压缩后的长轮）会凭空冒出一张
        // "很早之前的计划卡"。认定依据：turn 自带 plan_status/plan_doc_path
        // （后端真值源），或路径正是本轮的约定文档名。
        const ownsPlan = Boolean(t.plan_status || t.plan_doc_path)
          || planDocPath === `ai/chatcoder-plan-${sessionId}-${t.id}.md`;
        if (!ownsPlan) continue;
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
      // plan-238-1188: 同时清理陈旧条目——只保留"本轮仍成立"的卡片
      // （recovered 覆盖的、pendingPlan 归属轮、或 turn 自带 plan 字段 /
      // 本轮约定文档名）。否则历史误登记的计划卡会在压缩刷新后继续复活。
      set((s) => {
        const kept: Record<number, PlanCardInfo> = {};
        for (const [k, v] of Object.entries(s.plansByTurn || {})) {
          const tid = Number(k);
          if (recoveredPlans[tid]) continue; // recovered 版本更权威，直接采用
          const owned = turns.find((t) => t.id === tid);
          const ownByFields = Boolean(owned && (owned.plan_status || owned.plan_doc_path));
          const ownByPath = (v as PlanCardInfo | undefined)?.planDocPath
            === `ai/chatcoder-plan-${sessionId}-${tid}.md`;
          if (ownByFields || ownByPath || s.pendingPlan?.turnId === tid) kept[tid] = v;
        }
        return { plansByTurn: { ...recoveredPlans, ...kept } };
      });

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
      // plan-41-233：右面板按会话分桶——会话删除后清掉它的桶（终端/浏览器实例随卸载回收）。
      // 动态 import 避免 chat ↔ panel 静态循环依赖（与 store/ui.ts 的转发写法一致）。
      void import("./panel")
        .then(({ usePanelStore }) => usePanelStore.getState().dropBucket(sessionId))
        .catch(() => { /* 清理失败不阻塞删除流程 */ });
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

  sendTurn: async (content, attachments, reasoningEffort, mode, modelId, refs) => {
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
        ? setSessionRunning({ ...x, last_activity_at: new Date().toISOString() }, true)
        : x)),
    }));
    _startHeartbeat();
    try {
      const turn = await api.createTurn({ session_id: currentSessionId, content, attachments, reasoning_effort: reasoningEffort, mode, model_id: modelId ?? undefined, refs });
      _clearPendingDeltas(); // v6.5: 新 turn 清掉上一轮残留的完成标记，保证思考/正文从头实时流式
      set((s) => ({
        turns: [...s.turns, turn],
        runningTurnId: turn.id,
        isRunning: true,
        interruptedTurnId: null,
        // 发送消息后，乐观将当前会话置为运行中转圈状态
        sessions: s.sessions.map((x) => (x.id === currentSessionId ? setSessionRunning(x, true) : x)),
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
        isRunning: false, runningTurnId: null, pendingPlan: null, pendingPlanTurn: null,
        sessions: s.sessions.map((x) => (x.id === currentSessionId ? setSessionRunning(x, false) : x)),
      }));
      // plan-308-1542 需求1：发送失败属任务执行类错误——只进消息流错误卡，不弹右上角 Toast。
      get().setFlowError(String(e), null);
      return null;
    } finally {
      _sendingGuard = false;
    }
  },

  confirmPlan: async (task) => {
    const { currentSessionId } = get();
    if (!currentSessionId || !task.trim()) return;
    // 确认执行 = 授权执行：先把会话切到 agent（智能体模式，plan-75-332：
    // 原 accept_edits 已取消），避免 plan 权限拦截写盘
    try {
      await api.updateSession(currentSessionId, { permission_mode: "agent" });
      set((s) => ({ sessions: s.sessions.map((x) => (x.id === currentSessionId ? { ...x, permission_mode: "agent" } : x)) }));
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

  dismissPlan: (turnId?: number) => {
    // 计划卡取消必须落库（复用确认接口 accepted=false 的"停止任务"语义）：
    // 此前只改本地 plansByTurn，数据库 plan_status 仍停在 proposed，
    // 重启/切回会话后卡片按真值恢复，"取消 / 确认执行"按钮复活。
    void get().confirmPlanTurn(false, turnId);
  },

  /** v38 (plan-482): 确认/取消方案文档（不再涉及 group/steps）。 */
  confirmPlanTurn: async (accepted, turnId) => {
    const pending = get().pendingPlan;
    // 计划卡动作不依赖 pendingPlan：显式 turnId 优先（重启/历史恢复的卡片
    // pendingPlan 为 null，只依赖它会让按钮点击静默短路、点了没反应）。
    const tid = turnId ?? pending?.turnId ?? null;
    if (tid == null) return;
    const card = get().plansByTurn[tid];
    const taskTitle = pending?.task ?? card?.task ?? "任务执行计划";
    const docPath = pending?.planDocPath || card?.planDocPath || "";
    // 更新 plansByTurn 对应卡片的状态为 confirmed 或 cancelled，保留卡片在时间线上
    set((s) => ({
      plansByTurn: {
        ...s.plansByTurn,
        [tid]: {
          ...s.plansByTurn[tid],
          turnId: tid,
          task: taskTitle,
          planDocPath: docPath || s.plansByTurn[tid]?.planDocPath || "",
          status: accepted ? "confirmed" : "cancelled",
        },
      },
      pendingPlan: null,
      pendingPlanTurn: null,
      // plan-282-1422: 确认执行不新建 turn（后端 execute_confirmed_plan 复用本 turn 继续跑），
      // 前端就地乐观进入执行态：否则「点确认 → turn.started 到达」窗口内本 turn 的行状态
      // 仍是 awaiting_confirmation，该 turn 首条用户消息（全局最近一条，操作行 is-latest
      // 常显）会异常常驻复制/回滚按钮，直到 turn.started 才收回。
      ...(accepted
        ? {
            isRunning: true,
            runningTurnId: tid,
            interruptedTurnId: null,
            turns: s.turns.map((t) => (t.id === tid ? { ...t, status: "running" } : t)),
            sessions: s.sessions.map((x) => (x.id === s.currentSessionId ? setSessionRunning(x, true) : x)),
          }
        : {}),
    }));
    if (accepted) _startHeartbeat();
    const { currentSessionId } = get();
    if (currentSessionId != null) {
      const slice = _snapshotSlice(get());
      set((s) => ({ sessionState: { ...s.sessionState, [currentSessionId]: slice } }));
    }
    try {
      const res = await api.confirmPlanTurn(tid, { accepted });
      const pm = res?.permission_mode;
      // plan-75-332: 后端返回的已是新语义（agent / plan / readonly），
      // 统一走 normalizeDraftMode 归一化（兼容旧版后端返回的 accept_edits）。
      if (pm === "agent" || pm === "plan" || pm === "readonly" || pm === "default" || pm === "accept_edits") {
        if (currentSessionId != null) {
          const nextMode = normalizeDraftMode(pm);
          set((s) => ({ sessions: s.sessions.map((x) => (x.id === currentSessionId ? { ...x, permission_mode: nextMode } : x)) }));
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
        // plan-282-1422: 确认请求失败——回退上面的乐观执行态，否则界面会一直停在"运行中"
        if (accepted) {
          _clearHeartbeat();
          set({ isRunning: false, runningTurnId: null });
        }
        // plan-308-1542 需求1：计划确认/执行失败属任务执行类——只进消息流
        get().setFlowError(msg, tid);
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
      // plan-308-1542 需求1：重试失败属任务执行类——只进消息流
      get().setFlowError(`重试失败：${String(e)}`, task.turn_id);
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
    // plan-75-332: 一并清掉解释内容（本次审批已结束，保留下来只会串到下个审批卡）
    set({ pendingApproval: null, questionDraft: null, approvalExplain: null });
  },

  /** plan-238-1188: 提问向导作答草稿写回（切设置页/换会话后仍可恢复）。 */
  setQuestionDraft: (draft) => set({ questionDraft: draft }),

  /** plan-75-332: 请求 AI 解释审批项。立即置 loading（UI 立刻出骨架），
   *  内容由 approval.explain.delta/done/error 事件补充。 */
  explainApproval: (approvalId) => {
    set({ approvalExplain: { approvalId, status: "loading", text: "", error: null } });
    wsClient.send("approval.explain", { approval_id: approvalId });
  },

  /** plan-75-332: 清空解释状态（含增量缓冲，避免下个审批卡复用残留文本）。 */
  resetApprovalExplain: () => { if (get().approvalExplain) set({ approvalExplain: null }); },

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

  // v11: 批量审核——乐观更新本地状态后 PUT 持久化；失败回滚并提示（消息流错误卡）。
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
      // plan-308-1542 需求1：审核写入失败属任务执行类——只进消息流
      get().setFlowError(`保存审核状态失败：${String(e)}`, turnId);
    }
  },

  cancelTurn: async () => {
    const { runningTurnId } = get();
    if (!runningTurnId || _stoppingTurnIds.has(runningTurnId)) return;
    const turnId = runningTurnId;
    _stoppingTurnIds.add(turnId);
    _clearHeartbeat();
    // 停止是用户明确的本地控制操作：先清理 UI，再等待后端信号/落库。
    set((s) => ({
      runningTurnId: null,
      isRunning: false,
      interruptedTurnId: turnId,
      turns: s.turns.map((t) => (t.id === turnId ? { ...t, status: "interrupted" } : t)),
      streamingBuffers: {},
      thinkingBuffers: {},
      subagentStreams: {},
      subagentThinking: {},
      // v43: 一并清空子代理等待态——停止会连带取消所有子代理（后端 cancel_turn），
      // 否则残留的 pendingSubagents>0 会在消息流尾部留一条「等待子代理结束…」兜底行。
      pendingSubagents: 0,
      pendingApproval: null,
      questionDraft: null,
      approvalExplain: null,
      sessions: s.sessions.map((x) => (x.id === s.currentSessionId ? setSessionRunning(x, false) : x)),
    }));
    // 取消接口有专用短超时；失败不覆盖已经完成的本地停止。
    void api.cancelTurn(turnId).catch(() => { /* 后端可能正在异步收尾 */ });
  },
  forceStop: async () => {
    // 保留兼容入口，统一走同一套乐观停止逻辑，避免两套状态清理漂移。
    await get().cancelTurn();
  },
  resumeTurn: async () => {
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
      // plan-1094: 回滚成功后立即清掉该 turn 及其之后的待审缓存——后端已物理删除
      // RollbackWrite(turn_id >= turn_id)，前端缓存残留会让贴条持续显示「N 待审」
      const nextChanges = { ...get().turnChanges };
      for (const k of Object.keys(nextChanges)) {
        if (Number(k) >= turnId) delete nextChanges[Number(k)];
      }
      // S16（plan-41-197）：撤回回填恢复引用 chips——发送时 refs 被拼装成
      // 「引用文件：/ 使用技能：/ 使用连接器：/ 使用插件：」可读行追加在正文后；
      // 撤回时解析回结构化 refs，输入框恢复 chips 样式（不再只剩纯文本）。
      const rawText = result.user_message ?? "";
      const restoredRefs: Array<{ kind: string; value: string; label: string }> = [];
      const bodyLines: string[] = [];
      const _kindMap: Record<string, string> = {
        "引用文件": "file", "使用技能": "skill", "使用连接器": "mcp", "使用插件": "plugin",
      };
      for (const line of rawText.split("\n")) {
        const m = line.match(/^(引用文件|使用技能|使用连接器|使用插件)：(.*)$/);
        if (!m) { bodyLines.push(line); continue; }
        const kind = _kindMap[m[1]];
        for (const part of m[2].split("、")) {
          const v = part.trim();
          if (!v) continue;
          const value = kind === "file" ? v.replace(/^@/, "") : kind === "skill" ? v.replace(/^\$/, "") : v;
          restoredRefs.push({ kind, value, label: value });
        }
      }
      const restoredText = bodyLines.join("\n").replace(/\n{3,}/g, "\n\n").trim();
      set({
        messages, turns, tasks,
        turnChanges: nextChanges,
        isRunning: false, runningTurnId: null,
        // 回填原文与附件到「本会话」输入框，供用户修改后重发（v40: 按 key 隔离，不串扰首页/其它会话）
        ...(restoreToComposer && (restoredText || restoredAttachments.length > 0 || restoredRefs.length > 0)
          ? {
              composerBackfill: {
                key: String(currentSessionId),
                text: restoredText,
                attachments: restoredAttachments,
                refs: restoredRefs,
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
            // v36 (plan-321-1600 M2): 头部展示字段（用时 / 文件数 / 用量）
            startedAt: it.created_at ?? prev?.startedAt ?? null,
            endedAt: it.updated_at ?? prev?.endedAt ?? null,
            filesCount: it.files_count ?? prev?.filesCount ?? 0,
            tokens: it.token_usage ?? prev?.tokens ?? 0,
            // 终态单向不可逆：本地已 done/failed 不被 REST 滞后的 running/pending 回退
            status: (prev?.status === "done" || prev?.status === "failed") && (it.status === "running" || it.status === "pending")
              ? prev.status
              : (prev?.status === "running" && it.status === "pending" ? "running" : (it.status || prev?.status || "running")),
          };
        }
        // v39: 依据合并后的状态统计运行中子代理数——切回会话/断线重连后
        // 「等待子代理结束…」状态可恢复（终态不回退，口径与卡片一致）。
        const pending = Object.values(meta).filter((m) =>
          m.status === "running" || m.status === "in_progress" || m.status === "pending").length;
        return { subagentMeta: meta, pendingSubagents: pending };
      });
    } catch { /* 非阻塞 */ }
  },

  refreshMessages: async () => {
    const { currentSessionId } = get();
    if (!currentSessionId) return;
    // plan-41-198: 请求发起前记录本地已确认消息 id——区分「请求期间新增」与「已被删除」
    const baseIds = new Set(
      get().messages.filter((m) => (Number(m.id) || 0) <= 1e12).map((m) => Number(m.id)),
    );
    try {
      const messages = await api.listSessionMessages(currentSessionId);
      if (get().currentSessionId !== currentSessionId) return;
      set((s) => ({ messages: _mergeRefreshed(s.messages, messages, baseIds) }));
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
      const stopping = new Set(turns.filter((t) => _stoppingTurnIds.has(t.id)).map((t) => t.id));
      const running = turns.find((t) => t.status === "running" && !stopping.has(t.id));
      const interrupted = turns.find((t) => t.status === "interrupted");
      set({
        turns: turns.map((t) => (stopping.has(t.id) && t.status === "running"
          ? { ...t, status: "interrupted" }
          : t)),
        runningTurnId: running?.id ?? null,
        isRunning: Boolean(running),
        interruptedTurnId: interrupted?.id ?? (stopping.size ? [...stopping][0] : null),
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
  setFlowError: (text, turnId = null) => set({ flowError: { text: String(text), turnId, at: Date.now() } }),
  clearFlowError: () => set({ flowError: null }),
  // plan-41-228：末尾错误卡关闭 → 同步隐藏该 turn 内的 ERROR 消息（同一条错误只呈现一次）
  dismissTurnError: (turnId) => {
    if (turnId == null || get().hiddenTurnErrors.includes(turnId)) return;
    set((s) => ({ hiddenTurnErrors: [...s.hiddenTurnErrors, turnId] }));
  },
  resetMergeProgress: () => set({ mergeProgress: null }),
  applyMergeProgress: (payload) => set((s) => {
    // plan-308-1542 需求3-A：把 merge.progress 累积成"像消息流一样"的进度行。
    const mergeId = String(payload.merge_id ?? "");
    const phase = String(payload.phase ?? "");
    const detail = String(payload.detail ?? payload.reason ?? "");
    const path = payload.path != null ? String(payload.path) : null;
    const tool = payload.tool != null ? String(payload.tool) : null;
    const prev = s.mergeProgress;
    // 新的 merge_id 到来即开启一轮新进度
    const base = prev && prev.mergeId === mergeId
      ? prev
      : { mergeId, running: true, lines: [], report: null };
    const okVal = typeof payload.ok === "boolean" ? payload.ok : null;
    // 进度行文本：阶段 + 文件 + 工具 + 说明（紧凑可读）
    const labelMap: Record<string, string> = {
      prepare: "准备", detect: "git 分析", file_start: "开始", tool: "工具",
      file_done: "完成", apply: "提交", done: "结束", error: "出错",
    };
    let text = `[${labelMap[phase] ?? phase}]`;
    if (payload.index != null && payload.total != null) text += ` ${payload.index}/${payload.total}`;
    if (path) text += ` ${path}`;
    if (tool) text += ` · ${tool}`;
    if (detail) text += ` — ${detail}`;
    if (payload.elapsed_ms != null) text += ` (${payload.elapsed_ms}ms)`;
    const lines = [...base.lines, { phase, text, at: Date.now(), ok: okVal, path, tool }];
    return {
      mergeProgress: {
        mergeId,
        running: phase !== "done" && phase !== "error",
        lines: lines.slice(-500),  // 上限防内存膨胀
        report: (payload.summary as MergeReportOut | null) ?? base.report,
      },
    };
  }),

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
        /* 【串会话修复】会话归属校验（兜底防线）。
         *
         * 链路：后端 message_service 按 session_id 定向广播 → WsClient 用 currentSessionId
         * 过滤旧连接 → 本处再按消息自带 session_id 复核。
         *
         * 为什么要第三道：wsClient 的过滤依赖"切换/重连时序"，一旦存在竞态
         * （切换瞬间旧 socket 已派发、或 sync.request 补齐的历史事件跨越了会话边界），
         * 别的会话的消息就会被 _appendOrdered 追加进当前 messages —— 表现为
         * "当前会话里冒出另一个会话的内容"。消息自带 session_id，直接按它丢弃最可靠。
         *
         * 子代理线程消息（thread_id != null）同样属于本会话，一并校验。 */
        const msgSessionId = rawMsg.session_id != null ? Number(rawMsg.session_id) : null;
        if (msgSessionId != null && msgSessionId !== get().currentSessionId) {
          break;
        }
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
            // plan-330-1648 M6: 落库清理流式缓冲时**保留未落库的尾部增量**——
            // 消息落库与 delta 到达存在竞态，旧实现直接 delete 会让已渲染的尾字丢失、
            // 或让后续 delta 重复追加（表现为面板文本闪烁/重复）。
            const _landedText = typeof (rawMsg.content as Record<string, unknown>)?.text === "string"
              ? String((rawMsg.content as Record<string, unknown>).text) : "";
            const _bufText = subStreams[rawThread] || "";
            if (_landedText && _bufText.startsWith(_landedText)) {
              const _tail = _bufText.slice(_landedText.length);
              if (_tail) subStreams[rawThread] = _tail;
              else delete subStreams[rawThread];
            } else {
              delete subStreams[rawThread];
            }
            // 思考缓冲仍按落库即清（思考块落库后不会再有同段增量）
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
          return {
            messages: newMessages,
            streamingBuffers: newStreaming,
            thinkingBuffers: newThinking,
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
            // v7(B/H): 新 turn 清空上一轮运行中的工具实时输出/结果缓存
            runningToolOutput: {},
            runningToolResults: {},
            todos: null,
            todosTurnId: null,
            todoPersisted: false,
            // plan-282-1421（第10项）：新 turn 重置缓存均值统计——
            // "平均缓存命中率"应是当前任务的滑动均值，跨 turn 累加会越来越钝。
            usageCacheTotals: { inputSum: 0, cachedSum: 0, samples: 0 },
            // v35: 新 turn 开始时清掉上一轮残留的重试状态提示
            turnStatus: null,
            // plan-308-1542 需求1：新一轮开始即清掉上一轮的消息流错误卡
            flowError: null,
            // v26: 新 turn 开始 = 旧方案提案失效，隐藏旧方案卡片（task.proposed 后再展示新卡片）
            pendingPlan: null,
            pendingPlanTurn: null,
            sessions: s.sessions.map((x) => (x.id === activeSid ? setSessionRunning(x, true) : x)),
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
          if (ended) _stoppingTurnIds.delete(turnId);
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
            ...(status === "running" && !_stoppingTurnIds.has(turnId) && (s.runningTurnId == null || s.runningTurnId === turnId)
              ? {
                  runningTurnId: turnId,
                  isRunning: true,
                  sessions: s.sessions.map((x) => (x.id === activeSid ? setSessionRunning(x, true) : x)),
                }
              : {}),
            ...(clearsRunning
              ? {
                  runningTurnId: null,
                  isRunning: false,
                  sessions: s.sessions.map((x) => (x.id === activeSid ? setSessionRunning(x, false) : x)),
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
                  sessions: s.sessions.map((x) => (x.id === turnCompletedSid ? setSessionRunning(x, false) : x)),
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
        // 正常完成才自动续发队列；用户主动 Stop 后队列保留，避免误发。
        if (!_stoppingTurnIds.has(turnId)) void get()._drainQueue();
        break;
      }
      case "turn.failed": {
        // v45: 执行失败——必须可视化：写入消息流错误卡 + 复位运行态 + 刷新消息
        // （后端已补错误消息；此前 failed 与中断共用事件导致静默终止）。
        // plan-308-1542 需求1：任务执行失败属"任务执行类错误"——只进消息流，不弹右上角 Toast。
        const turnId = Number(payload.turn_id);
        const failReason = String(
          (payload as { error?: unknown; summary?: unknown }).error
            ?? (payload as { summary?: unknown }).summary
            ?? "任务执行失败",
        );
        // v2.2: 仅当失败的就是当前运行 turn 才复位运行态（迟到事件同样不串扰）
        const clearsRunningFail = get().runningTurnId == null || get().runningTurnId === turnId;
        set((s) => ({
          turnStatus: null,
          turns: s.turns.map((t) => (t.id === turnId
            ? { ...t, status: "failed", summary: t.summary ?? failReason, completed_at: t.completed_at ?? new Date().toISOString() }
            : t)),
          ...(clearsRunningFail
            ? { runningTurnId: null, isRunning: false, streamingBuffers: {}, thinkingBuffers: {} }
            : {}),
        }));
        get().setFlowError(failReason, turnId);
        get().refreshMessages();
        get().refreshTurns();
        get().refreshTasks();
        void get().loadSessionSubagents();
        // 失败后不自动消费队列（避免连环失败刷屏），用户确认后手动继续。
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
        // interrupted 由用户停止，不自动消费队列。
        break;
      }
      case "turn.rolled_back": {
        // v11: 已回滚 turn 的变更已撤销，清掉其审核卡片
        // plan-1094: 回滚语义为撤销该 turn 及其之后的写盘（后端删除 turn_id >= turn_id
        // 的 RollbackWrite），缓存清理同口径 >=；该事件由后端 rollback_service 补广播
        const rollbackTurnId = Number((payload as { turn_id?: unknown }).turn_id ?? 0);
        set((s) => {
          if (!rollbackTurnId) return {};
          const next = { ...s.turnChanges };
          let changed = false;
          for (const k of Object.keys(next)) {
            if (Number(k) >= rollbackTurnId) {
              delete next[Number(k)];
              changed = true;
            }
          }
          return changed ? { turnChanges: next } : {};
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
      case "subagent.failed": {
        // plan-330-1648 M7: 子代理失败/取消 → 卡片与面板展示原因（不再只有一个红叉）
        const fAid = Number(payload.agent_id ?? 0);
        const fErr = typeof payload.error === "string" ? payload.error : "";
        const fStatus = String(payload.status ?? "failed");
        if (fAid) {
          set((s) => ({
            subagentMeta: {
              ...s.subagentMeta,
              [fAid]: {
                ...(s.subagentMeta[fAid] ?? {
                  name: `子代理 #${fAid}`, turnId: null, taskId: null, status: fStatus,
                }),
                status: fStatus,
                error: fErr || "未知原因",
              },
            },
          }));
        }
        get().refreshTasks();
        break;
      }
      case "subagent.pending": {
        // v39: 后台子代理运行数变化——>0 时主会话保持“运行中”并显示「等待子代理结束…」。
        // v44 修复（左侧转圈在子代理流程后消失）：=0 时**不再直接摘除运行标记**——
        // 主代理收工等待子代理、以及「子代理全部结束 → 唤醒轮启动」之间的窗口内，
        // isRunning 可能已是 false（如等待态），此时摘除会让转圈提前消失，
        // 且此后没有事件把它恢复（表现为“AI 还在跑但左侧不转圈”）。
        // 摘除统一交给权威事件：session.completed（含服务端空闲兜底补发）/
        // subagent.wakeup（唤醒轮接管）/ turn.started。
        const pSid = Number((payload as { session_id?: unknown }).session_id ?? 0);
        const pCount = Math.max(0, Number((payload as { pending?: unknown }).pending ?? 0));
        set((s) => {
          const isCurrent = pSid === s.currentSessionId;
          return {
            ...(isCurrent ? { pendingSubagents: pCount } : {}),
            sessions: s.sessions.map((x) => (x.id === pSid
              ? (pCount > 0 ? setSessionRunning(x, true) : x)
              : x)),
          };
        });
        break;
      }
      case "subagent.wakeup": {
        // v39: 子代理完成唤醒——服务端已创建新一轮（完成报告随该轮送达主代理）。
        // v44 修复：这里立即接管运行态，而不是只等随后的 turn.started——
        // 唤醒轮创建与 start_turn 之间存在延迟窗口，期间若无运行标记，
        // 左侧转圈会短暂/永久消失（turn.started 一旦丢失便再无恢复事件）。
        const wakeSid = Number((payload as { session_id?: unknown }).session_id ?? 0);
        const wakeTurnId = Number((payload as { turn_id?: unknown }).turn_id ?? 0) || null;
        if (wakeSid) {
          set((s) => ({
            ...(wakeSid === s.currentSessionId
              ? { isRunning: true, runningTurnId: s.runningTurnId ?? wakeTurnId }
              : {}),
            sessions: s.sessions.map((x) => (x.id === wakeSid ? setSessionRunning(x, true) : x)),
          }));
        }
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
        if (aid && delta && !_streamDoneText[`thinking:${tid ?? aid}`] && Date.now() >= _replayGuardUntil) {
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
        if (aid && delta && !_streamDoneText[`token:${tid ?? aid}`] && Date.now() >= _replayGuardUntil) {
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
      case "tool.output": {
        // v7(B): 运行中命令/工具的实时输出增量——按 call_key 累积，ToolTree 展开时实时渲染。
        // plan-334-1661 S2：改为与 token/thinking 同一条帧合流（原先每 chunk 一次 set，
        // 高频输出时每 chunk 触发一次 React 提交）；顺带获得几何运动期暂停与高负载降级。
        const ok = String(payload.call_key ?? "");
        const chunk = String(payload.chunk ?? "");
        if (ok && chunk) {
          _pendingToolOutput[ok] = (_pendingToolOutput[ok] || "") + chunk;
          _scheduleDeltaFlush();
        }
        break;
      }
      case "tool.result": {
        // v7(H): 实时消费工具结果（含 change_stat/duration_ms）——运行中即可展示写操作 +N-M；
        // 落库 message.created 后由 timeline 的权威 output/changeStat 覆盖，此处仅作过渡。
        const rk = String(payload.call_key ?? "");
        if (rk) {
          const prev = get().runningToolResults[rk];
          const nextResult: RunningToolResult = {
            ok: Boolean(payload.ok),
            output_preview: (typeof payload.output_preview === "string" ? payload.output_preview : prev?.output_preview),
            change_stat: (payload.change_stat && typeof (payload.change_stat as { additions?: unknown }).additions === "number"
              ? (payload.change_stat as RunningToolResult["change_stat"])
              : prev?.change_stat),
            duration_ms: (typeof payload.duration_ms === "number" ? payload.duration_ms : prev?.duration_ms),
          };
          set((s) => ({
            runningToolResults: { ...s.runningToolResults, [rk]: nextResult },
          }));
        }
        break;
      }
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
          // plan-282-1492: 记下清单归属 turn——首载（重启）后即便清单还在，
          // 也要先判断这一轮是否已经结束，避免把结束任务的残留清单当成进行中进度。
          todosTurnId: Number(payload.turn_id) || null,
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
          /* 【计划卡状态不可回退】修复"点过确认执行后，取消/确认按钮又冒出来"。
           *
           * 根因：本分支此前**无条件**把卡片写成 awaiting_confirmation。而 plan 模式
           * 的流程是「规划轮 → 用户确认 → 同一 turn 继续执行」（execute_confirmed_plan
           * 复用本 turn），执行阶段若再次广播 task.proposed（或断线重放补发），
           * 这张已经 confirmed 的卡片就被打回"待确认"——PlanCard 于是重新渲染出
           * 取消/确认按钮，用户会以为要再确认一次。
           *
           * 规则：已进入终态（confirmed / cancelled / superseded）的卡片，
           * 后续任何 proposed 都不得改写其状态；也不再把 pendingPlan 重新置起
           * （否则输入框上方的确认入口也会复活）。 */
          const existing = s.plansByTurn[turnId];
          const isTerminal = existing != null && existing.status !== "awaiting_confirmation";
          if (isTerminal) {
            return {
              ...(clearsRunning ? { isRunning: false, runningTurnId: null } : {}),
              turns: s.turns.map((t) => (t.id === turnId ? { ...t, status: "running" } : t)),
            };
          }
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
          /* plan-282-0: 透传占用口径来源——此前该字段定义了却从未赋值，
           * 圆环无法区分「API 真实占用」与「压缩后本地估算」，用户在压缩后看到的
           * 估算值（est_after_compact）与真实占用可能相差数倍（实测估算 14.4% /
           * 真实 69%），造成"压缩后占用很低"的错觉。 */
          source: String(payload.usage_source ?? "") || undefined,
        };
        // plan-282-1421（第10项）：累加会话缓存统计，供"平均缓存命中率"使用。
        // 仅统计真实 API 样本（cached_input>0 或 prompt>0 才有意义），避免估算样本污染均值。
        const prevTotals = get().usageCacheTotals;
        const counted = promptTokens > 0;
        set({
          usage: detail,
          usageCacheTotals: counted
            ? {
                inputSum: prevTotals.inputSum + promptTokens,
                cachedSum: prevTotals.cachedSum + detail.cached_input,
                samples: prevTotals.samples + 1,
              }
            : prevTotals,
        });
        break;
      }
      case "merge.progress": {
        /* plan-308-1542 需求3-A：AI 自动合并的实时进度。
         *
         * 后端 ai_merge_all 边执行边广播（prepare / detect / file_start / tool /
         * file_done / done），这里累积成进度行供合并弹窗渲染；
         * done 携带的 summary 作为汇总报告。
         * 会话归属校验：事件带 session_id 时只接受当前会话的（避免其它会话的合并串入）。
         */
        const mpSid = Number(payload.session_id ?? 0);
        if (mpSid && mpSid !== get().currentSessionId) break;
        get().applyMergeProgress(payload);
        break;
      }
      case "debug.paused": {
        /* plan-282-1441（#8）：调试命中事件。
         *
         * 后端 debug_service 在 AI 调试命中断点时广播本事件（含停在哪一行、
         * 调用栈、变量）。这里做两件事：
         *  1. 记入 store（供调试面板/消息流卡片读取当前调试现场）；
         *  2. 派发 window 事件，让已挂载的调试面板立即刷新（无需轮询）。
         *
         * 会话归属校验：调试状态按会话隔离，别的会话的命中事件不得影响当前视图。 */
        const dbgSid = Number(payload.session_id ?? 0);
        if (dbgSid && dbgSid !== get().currentSessionId) break;
        const target = String(payload.target || "web") as "web" | "java";
        const phase = String(payload.phase || "paused");
        set({
          debugState: {
            ...(get().debugState || {}),
            [target]: {
              connected: phase !== "stopped",
              target,
              breakpoints: Number(payload.breakpoints ?? get().debugState?.[target]?.breakpoints ?? 0),
              paused: phase === "paused",
              file: (payload.file as string | null) ?? null,
              line: payload.line != null ? Number(payload.line) : null,
              function: (payload.function as string | null) ?? null,
              hitCount: Number(payload.hitCount ?? 0),
              stack: (payload.stack as DebugStatusOut["stack"]) ?? [],
              variables: (payload.variables as DebugStatusOut["variables"]) ?? [],
              reason: (payload.reason as string | null) ?? null,
            },
          },
        });
        try {
          window.dispatchEvent(new CustomEvent("chatcoder:debug-paused", { detail: payload }));
        } catch { /* 非浏览器环境忽略 */ }
        break;
      }
      case "arthas.event": {
        /* plan-282-1441（Arthas 方案）：Java 现场诊断事件。
         *
         * 服务端 arthas_service 在 attach / 提交观测 / 拉到命中 / 断开时广播，
         * 右侧「调试」面板据此实时展示"AI 正在观测什么、看到了什么"（用户可见性）。
         * 会话归属校验同 debug.paused：别的会话的事件不得影响当前视图。
         */
        const arSid = Number(payload.session_id ?? 0);
        if (arSid && arSid !== get().currentSessionId) break;
        const phase = String(payload.phase || "");
        const prev = get().arthasState;
        const freshEntries = (payload.entries as ArthasEntryOut[]) || [];
        const attached = phase === "attached" ? true
          : phase === "detached" ? false
          : (prev?.attached ?? true);
        set({
          arthasState: {
            attached,
            pid: (payload.pid as number) ?? prev?.pid ?? null,
            http_port: (payload.http_port as number) ?? prev?.http_port ?? null,
            version: (payload.version as string) ?? prev?.version ?? null,
            main_class: prev?.main_class,
            summary: (payload.summary as string) || prev?.summary,
            // 只在有命中时追加（job_started/attached 等事件不产生条目）；上限 60 条
            entries: freshEntries.length
              ? [...freshEntries, ...(prev?.entries || [])].slice(0, 60)
              : (prev?.entries || []),
          },
        });
        try {
          window.dispatchEvent(new CustomEvent("chatcoder:arthas-event", { detail: payload }));
        } catch { /* 非浏览器环境忽略 */ }
        break;
      }
      case "context.folded":
        // 用户反馈："上下文已回收 x k tokens"提示太丑，直接不展示；
        // 事件仅落空消费（保留 case 以维持 ServerEventName 穷举检查），不再写入任何状态。
        break;
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
      case "approval.request": {
        // plan-230-1144: 跨会话串线双保险——服务端已按 detail.session_id 精确路由，
        // 前端再校验一次：detail 携带的 session_id 与当前会话不一致时忽略
        // （历史遗留事件、多窗口重放等场景下防止提问/审批弹进错误的会话）。
        const detail = (payload.detail || {}) as Record<string, unknown>;
        const evtSid = Number(detail.session_id ?? 0);
        const curSid = get().currentSessionId;
        if (evtSid > 0 && curSid != null && evtSid !== curSid) break;
        // 新提问到达：清掉上一轮作答草稿（approvalId 键控，避免残留串答）
        set({ pendingApproval: { approvalId: String(payload.approval_id ?? ""), detail }, questionDraft: null });
        break;
      }
      case "approval.response":
        // 审批结果广播回前端（含其他窗口），关闭本地审批横幅
        // plan-75-332: 一并清掉解释内容（本次审批已结束，保留只会串到下个审批卡）
        set({ pendingApproval: null, questionDraft: null, approvalExplain: null });
        break;
      case "approval.explain.delta": {
        // plan-75-332: 解释流式增量。按 approvalId 严格过滤——审批已被处理时
        // 后端仍可能推送尾部事件，不过滤会把内容写到已消失的卡片（下次审批复活）。
        const cur = get().approvalExplain;
        const aid = String(payload.approval_id ?? "");
        if (cur && aid && cur.approvalId === aid) {
          set({
            approvalExplain: {
              ...cur,
              status: "streaming",
              text: cur.text + String(payload.delta ?? ""),
            },
          });
        }
        break;
      }
      case "approval.explain.done": {
        const cur = get().approvalExplain;
        const aid = String(payload.approval_id ?? "");
        if (cur && aid && cur.approvalId === aid) {
          set({
            approvalExplain: {
              ...cur,
              status: "done",
              text: String(payload.text ?? cur.text),
              model: typeof payload.model === "string" ? payload.model : undefined,
              reasoningEffort:
                typeof payload.reasoning_effort === "string" ? payload.reasoning_effort : undefined,
            },
          });
        }
        break;
      }
      case "approval.explain.error": {
        const cur = get().approvalExplain;
        const aid = String(payload.approval_id ?? "");
        if (cur && aid && cur.approvalId === aid) {
          set({
            approvalExplain: {
              ...cur,
              status: "error",
              error: String(payload.message ?? "解释失败，请重试"),
            },
          });
        }
        break;
      }
      case "session.updated": {
        const sid = Number(payload.session_id ?? 0);
        const title = typeof payload.title === "string" ? payload.title : null;
        // v2.2 (plan-88): 执行结束后后端恢复 plan 模式 → 同步 permission_mode，
        // ComposerCore 据此把输入框切回「计划模式」。
        const pm = payload.permission_mode;
        // plan-75-332: 执行模式新三档（agent/plan/readonly）；旧值 default/accept_edits
        // 经 normalizeDraftMode 归一化，避免会话里留下菜单选不中的脏值。
        const permissionMode = typeof pm === "string" && pm ? normalizeDraftMode(pm) : null;
        // plan-75-332: 权限模式同步（多窗口/多端场景下其他端改动要跟随）
        const am = payload.approval_mode;
        const approvalMode = typeof am === "string" && am ? normalizeApprovalMode(am) : null;
        if (sid > 0 && (title || permissionMode || approvalMode)) {
          set((s) => ({
            sessions: s.sessions.map((x) => (x.id === sid
              ? { ...x,
                  ...(title ? { title } : {}),
                  ...(permissionMode ? { permission_mode: permissionMode } : {}),
                  ...(approvalMode ? { approval_mode: approvalMode } : {}) }
              : x)),
          }));
        }
        break;
      }
      case "session.completed": {
        const sid = Number((payload as { session_id?: unknown }).session_id ?? 0);
        // v39: 后台子代理仍在跑时保持“运行中”（转圈 + 等待子代理结束…）——
        // 此前无条件摘除运行标记，子代理续跑期间界面误显为空闲；
        // 待 subagent.pending=0 事件到达再摘除。
        const subPending = Math.max(0, Number((payload as { subagent_pending?: unknown }).subagent_pending ?? 0));
        // v1.1: 本地立即摘除转圈标记，避免等待 REST
        set((s) => {
          // v2.2: 仅当完成的就是当前会话才复位视图运行态——
          // 后台会话的 session.completed 只更新其列表标记，不串扰当前会话。
          const isCurrent = sid === s.currentSessionId;
          return {
            ...(isCurrent ? { isRunning: false, runningTurnId: null, pendingSubagents: subPending } : {}),
            sessions: s.sessions.map((x) => (x.id === sid
              ? setSessionRunning(x, subPending > 0)
              : x)),
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
      // plan-248-1258 M3.4: 符号索引进度（全局事件，无 session_id）——
      // 转成 window 事件供索引库页面刷新；不进入会话状态机。
      if (ev.event === "symbol_index.progress") {
        try {
          window.dispatchEvent(new CustomEvent("chatcoder:symbol-index-progress", { detail: p }));
        } catch { /* ignore */ }
        return;
      }
      const sid = Number(p.session_id ?? 0);
      if (!sid) return;
      const event = ev.event as ServerEventName;
      if (event === "session.completed") {
        const ts = typeof p.last_activity_at === "string" ? p.last_activity_at : null;
        // v44 修复（左侧转圈在子代理流程后消失）：与会话级通道同口径——后台子代理
        // 仍在跑（subagent_pending>0）时必须保持运行标记。此前这里无条件摘除，
        // 与会话级通道（按 pending 保持）对同一事件的处理互相打架：两条 WS 到达
        // 顺序不定，全局后到就会把转圈摘掉，且此后全局通道没有任何恢复事件
        // （turn.started / subagent.* 均不转发），侧栏长时间停在“空闲”假象。
        const subPending = Math.max(0, Number(p.subagent_pending ?? 0));
        set((s) => {
          if (!s.sessions.some((x) => x.id === sid)) return {};
          return {
            sessions: s.sessions.map((x) => (x.id === sid
              ? { ...setSessionRunning(x, subPending > 0), ...(ts ? { last_activity_at: ts } : {}) }
              : x)),
            ...(s.currentSessionId === sid ? { isRunning: false, runningTurnId: null } : {}),
          };
        });
        return;
      }
      // v44 修复：运行态**恢复**分支——后台会话的新轮启动（turn.started）/ 子代理唤醒
      // （subagent.wakeup）/ 子代理开始运行（subagent.pending>0）/ turn 回到 running
      // 时同步置位运行标记。只置位、不清除：摘除统一由 session.completed 权威事件
      // 负责（含服务端空闲兜底补发），避免多条通道对同一会话互相覆盖。
      if (event === "turn.started" || event === "subagent.wakeup"
          || (event === "turn.updated" && p.status === "running")
          || (event === "subagent.pending" && Number(p.pending ?? 0) > 0)) {
        set((s) => {
          if (!s.sessions.some((x) => x.id === sid)) return {};
          return {
            sessions: s.sessions.map((x) => (x.id === sid ? setSessionRunning(x, true) : x)),
          };
        });
        return;
      }
      if (event === "session.updated") {
        const title = typeof p.title === "string" ? p.title : null;
        const ts = typeof p.last_activity_at === "string" ? p.last_activity_at : null;
        const pm = p.permission_mode;
        // plan-75-332: 执行模式新三档（agent/plan/readonly）——旧值经归一化，
        // 避免侧栏会话留下模式菜单选不中的脏值。
        const permissionMode = typeof pm === "string" && pm ? normalizeDraftMode(pm) : null;
        // plan-75-332: 权限模式同步（后台会话改权限后侧栏/输入框要跟随）
        const am = p.approval_mode;
        const approvalMode = typeof am === "string" && am ? normalizeApprovalMode(am) : null;
        if (!title && !ts && !permissionMode && !approvalMode) return;
        set((s) => {
          if (!s.sessions.some((x) => x.id === sid)) return {};
          return {
            sessions: s.sessions.map((x) => (x.id === sid
              ? { ...x, ...(title ? { title } : {}), ...(ts ? { last_activity_at: ts } : {}),
                  ...(permissionMode ? { permission_mode: permissionMode } : {}),
                  ...(approvalMode ? { approval_mode: approvalMode } : {}) }
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
