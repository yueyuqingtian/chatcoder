/** v2 会话状态管理（zustand）：项目 / 会话 / turn 任务驱动。 */
import { create } from "zustand";
import { api } from "../api/client";
import type { MessageOut, ProjectOut, SessionOut, TaskOut, TurnOut } from "../api/client";
import { wsClient } from "../api/ws";

/** 上下文占用详情（输入框圆环）。 */
export interface UsageDetail {
  input: number;
  cached_input: number;
  output: number;
  reasoning_output: number;
  total: number;
  context_window: number;
  agent_name: string;
}

interface ChatState {
  projects: ProjectOut[];
  sessions: SessionOut[];
  currentProjectId: number | null;
  currentSessionId: number | null;
  /** 当前会话全部消息（含 turn_id，供 timeline 分组）。 */
  messages: MessageOut[];
  turns: TurnOut[];
  tasks: TaskOut[];
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
  loading: boolean;
  error: string | null;
  wsConnected: boolean;

  // 动作
  loadBootstrap: () => Promise<void>;
  createProject: (path: string, name?: string) => Promise<ProjectOut | null>;
  selectProject: (projectId: number) => Promise<void>;
  createSession: (projectId: number, title?: string) => Promise<void>;
  switchSession: (sessionId: number) => Promise<void>;
  deleteSession: (sessionId: number) => Promise<void>;
  renameSession: (sessionId: number, title: string) => Promise<void>;
  forkSession: (sessionId: number) => Promise<void>;
 sendTurn: (content: string, attachments?: Record<string, unknown>[], reasoningEffort?: string) => Promise<void>;
 cancelTurn: () => Promise<void>;
  forceStop: () => Promise<void>;
 resumeTurn: () => Promise<void>;
  rollbackTurn: (turnId: number, restoreToComposer?: boolean) => Promise<void>;
  refreshMessages: () => Promise<void>;
  refreshTurns: () => Promise<void>;
  refreshTasks: () => Promise<void>;
  setComposerDraft: (text: string) => void;
  clearError: () => void;
  addMessage: (msg: MessageOut) => void;
  handleWs: (event: string, payload: Record<string, unknown>) => void;
}

let _sendingGuard = false;
let _wsUnsub: (() => void) | null = null;
let _heartbeatTimer: ReturnType<typeof setTimeout> | null = null;
const HEARTBEAT_TIMEOUT = 60_000; // 60s 无事件超时兜底复位

/** 启动/重置心跳计时器：60s 内无任何 WS 事件则强制复位 isRunning */
function _startHeartbeat() {
  if (_heartbeatTimer) clearTimeout(_heartbeatTimer);
  _heartbeatTimer = setTimeout(() => {
    _heartbeatTimer = null;
    const st = useChatStore.getState();
    if (st.isRunning) {
      useChatStore.setState({ isRunning: false, runningTurnId: null });
    }
  }, HEARTBEAT_TIMEOUT);
}

/** 清除心跳计时器 */
function _clearHeartbeat() {
  if (_heartbeatTimer) {
    clearTimeout(_heartbeatTimer);
    _heartbeatTimer = null;
  }
}

/** 统一清理所有会话级字段（§9.1 #18：防止切换会话后残留） */
function _resetSessionState(): Partial<ChatState> {
  _clearHeartbeat();
  return {
    messages: [],
    turns: [],
    tasks: [],
    runningTurnId: null,
    isRunning: false,
    interruptedTurnId: null,
    streamingBuffers: {},
    thinkingBuffers: {},
    usage: null,
    isCompacting: false,
    pendingApproval: null,
  };
}

export const useChatStore = create<ChatState>((set, get) => ({
  projects: [],
  sessions: [],
  currentProjectId: null,
  currentSessionId: null,
  messages: [],
  turns: [],
  tasks: [],
  runningTurnId: null,
  isRunning: false,
  interruptedTurnId: null,
  streamingBuffers: {},
  thinkingBuffers: {},
  usage: null,
  isCompacting: false,
  pendingApproval: null,
  composerDraft: "",
  loading: false,
  error: null,
  wsConnected: false,

  loadBootstrap: async () => {
    set({ loading: true, error: null });
    try {
      const [projects, sessions] = await Promise.all([api.listProjects(), api.listSessions()]);
      set({ projects, sessions, loading: false });
      // 自动选中第一个活跃项目/会话
      const activeProjects = projects.filter((p) => !p.archived);
      const activeSessions = sessions.filter((s) => s.status !== "archived");
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
    set({ currentProjectId: projectId, currentSessionId: null, messages: [], turns: [], tasks: [] });
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
    } catch (e) {
      set({ error: String(e) });
    }
  },

  switchSession: async (sessionId) => {
    // 清理旧 WS 连接和 handler（§9.1 #1②：防止 handler 累积）
    wsClient.disconnect();
    if (_wsUnsub) { _wsUnsub(); _wsUnsub = null; }

    set({
      currentSessionId: sessionId,
      ..._resetSessionState(),
    });
    const session = get().sessions.find((s) => s.id === sessionId);
    if (session?.project_id) set({ currentProjectId: session.project_id });

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
        }});
      } catch { /* usage 加载失败不阻塞 */ }

      // WS 连接 + 事件监听（保存 cleanup 函数）
      wsClient.connect(sessionId);
      _wsUnsub = wsClient.on((ev) => {
        const payload = ev.payload as Record<string, unknown>;
        get().handleWs(ev.event, payload);
      });

      // 如果有运行中的 turn，启动心跳超时兜底（§9.1 #3）
      if (running) _startHeartbeat();
    } catch (e) {
      set({ loading: false, error: String(e) });
    }
  },

  deleteSession: async (sessionId) => {
    try {
      await api.deleteSession(sessionId);
      const sessions = await api.listSessions();
      set({ sessions });
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

  sendTurn: async (content, attachments, reasoningEffort) => {
    const { currentSessionId, isRunning } = get();
    if (!currentSessionId || !content.trim()) return;
    if (_sendingGuard || isRunning) return;
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
      content: { text: content },
      token_usage: 0,
      created_at: new Date().toISOString(),
    };
    get().addMessage(optimisticUserMsg);
    try {
      const turn = await api.createTurn({ session_id: currentSessionId, content, attachments, reasoning_effort: reasoningEffort });
      set((s) => ({
        turns: [...s.turns, turn],
        runningTurnId: turn.id,
        isRunning: true,
        interruptedTurnId: null,
      }));
      _startHeartbeat();
    } catch (e) {
      set({ error: String(e), isRunning: false });
    } finally {
      _sendingGuard = false;
    }
  },

 cancelTurn: async () => {
   const { runningTurnId } = get();
   if (!runningTurnId) return;
   try {
     await api.cancelTurn(runningTurnId);
      // 后端确认取消成功后复位
      _clearHeartbeat();
      set({ isRunning: false, runningTurnId: null });
   } catch (e) {
      // 取消失败：turn 可能已结束，强制复位并刷新真实状态
      _clearHeartbeat();
      set({ isRunning: false, runningTurnId: null, streamingBuffers: {}, thinkingBuffers: {} });
      const { currentSessionId } = get();
      if (currentSessionId) { try { await get().refreshTurns(); } catch { /* ignore */ } }
   }
 },
  forceStop: async () => {
    // 兜底强制复位：turn 可能已结束但 WS 未通知，或后端取消接口报错
    const { runningTurnId, currentSessionId } = get();
    if (runningTurnId) {
      try { await api.cancelTurn(runningTurnId); } catch { /* 已结束则忽略 */ }
    }
    _clearHeartbeat();
    set({ isRunning: false, runningTurnId: null, streamingBuffers: {}, thinkingBuffers: {} });
    // 重新拉取 turn 状态以同步真实状态
    if (currentSessionId) { try { await get().refreshTurns(); } catch { /* ignore */ } }
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

  refreshMessages: async () => {
    const { currentSessionId } = get();
    if (!currentSessionId) return;
    try {
      const messages = await api.listSessionMessages(currentSessionId);
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
      set({ tasks });
    } catch (e) {
      set({ error: String(e) });
    }
  },

  setComposerDraft: (text) => set({ composerDraft: text }),
  clearError: () => set({ error: null }),

  addMessage: (msg) => {
    set((state) => {
      // 去重：相同 ID 直接跳过
      if (state.messages.some((m) => m.id === msg.id)) return {};
      // 处理乐观消息替换：后端真实用户消息到达时，移除同内容同 sender 的临时乐观消息
      // 乐观消息 ID 是 Date.now()（很大），真实消息 ID 较小
      const isUserMsg = msg.sender_type === "user";
      const msgText = (msg.content as Record<string, unknown>).text as string | undefined;
      if (isUserMsg && msgText) {
        const optimisticIdx = state.messages.findIndex((m) =>
          m.sender_type === "user" &&
          m.id > 1_000_000_000_000 && // 乐观消息特征：超大临时 ID
          (m.content as Record<string, unknown>).text === msgText
        );
        if (optimisticIdx >= 0) {
          const newMessages = [...state.messages];
          newMessages.splice(optimisticIdx, 1, msg); // 原位替换，保持顺序
          return { messages: newMessages };
        }
      }
      // 按 ID 升序插入，保证顺序正确（后端按 id asc 返回）
      const newMessages = [...state.messages, msg].sort((a, b) => {
        const ia = Number(a.id) || 0;
        const ib = Number(b.id) || 0;
        return ia - ib;
      });
      return { messages: newMessages };
    });
  },

  handleWs: (event, payload) => {
    const st = get();
    // 运行中收到任何 WS 事件时重置心跳（§9.1 #3：60s 无事件超时兜底）
    if (st.isRunning) _startHeartbeat();
    switch (event) {
      case "message.created": {
        // 后端 payload 结构为 { msg: MessageOut }，需解包
        const rawMsg = (payload.msg ?? payload) as MessageOut;
        if (rawMsg.id != null) rawMsg.id = Number(rawMsg.id);
        if (rawMsg.content == null) rawMsg.content = {};
        // v1.3: 合并 addMessage 和清空 buffer 为一次 set()，避免中间态闪烁
        const sid = Number(rawMsg.sender_id);
        const isThinking = Boolean((rawMsg.content as Record<string, unknown>).thinking);
        set((state) => {
          // 去重
          if (state.messages.some((m) => m.id === rawMsg.id)) return {};
          // 处理乐观消息替换
          const isUserMsg = rawMsg.sender_type === "user";
          const msgText = (rawMsg.content as Record<string, unknown>).text as string | undefined;
          let newMessages: MessageOut[];
          if (isUserMsg && msgText) {
            const optimisticIdx = state.messages.findIndex((m) =>
              m.sender_type === "user" &&
              m.id > 1_000_000_000_000 &&
              (m.content as Record<string, unknown>).text === msgText
            );
            if (optimisticIdx >= 0) {
              newMessages = [...state.messages];
              newMessages.splice(optimisticIdx, 1, rawMsg);
            } else {
              newMessages = [...state.messages, rawMsg].sort((a, b) => (Number(a.id) || 0) - (Number(b.id) || 0));
            }
          } else {
            newMessages = [...state.messages, rawMsg].sort((a, b) => (Number(a.id) || 0) - (Number(b.id) || 0));
          }
          // 清空对应 agent 的流式 buffer，与消息落库同一次渲染完成
          const newStreaming = sid ? { ...state.streamingBuffers } : state.streamingBuffers;
          const newThinking = sid ? { ...state.thinkingBuffers } : state.thinkingBuffers;
          if (sid) {
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
        set({ runningTurnId: Number(payload.turn_id), isRunning: true });
        break;
      case "turn.updated": {
        const turnId = Number(payload.turn_id);
        const status = String(payload.status ?? "");
        set((s) => ({
          turns: s.turns.map((t) => (t.id === turnId ? { ...t, status } : t)),
          ...(status === "running" ? { runningTurnId: turnId, isRunning: true } : {}),
          ...(status === "completed" || status === "failed" || status === "cancelled" || status === "blocked"
            ? { runningTurnId: null, isRunning: false }
            : {}),
          ...(status === "interrupted" ? { interruptedTurnId: turnId, runningTurnId: null, isRunning: false } : {}),
        }));
        break;
      }
      case "turn.completed": {
        const turnId = Number(payload.turn_id);
        set((s) => ({
          turns: s.turns.map((t) => (t.id === turnId ? { ...t, status: "completed", summary: (payload.summary as string) ?? t.summary } : t)),
          runningTurnId: null, isRunning: false,
          streamingBuffers: {}, thinkingBuffers: {},  // v1.3: turn 结束兜底清空
        }));
        get().refreshMessages();
        get().refreshTasks();
        break;
      }
      case "turn.interrupted": {
        const turnId = Number(payload.turn_id);
        set({ interruptedTurnId: turnId, runningTurnId: null, isRunning: false, streamingBuffers: {}, thinkingBuffers: {} });
        get().refreshMessages();
        break;
      }
      case "turn.rolled_back":
        get().refreshMessages();
        get().refreshTurns();
        get().refreshTasks();
        break;
      case "agent.completed":
        get().refreshTasks();
        break;
      case "thinking.delta": {
        const aid = Number(payload.agent_id);
        const delta = String(payload.delta ?? "");
        if (aid && delta) {
          set((s) => ({ thinkingBuffers: { ...s.thinkingBuffers, [aid]: (s.thinkingBuffers[aid] || "") + delta } }));
        }
        break;
      }
      case "thinking.done": {
        // v1.3: 不立即清空 buffer，等 message.created 落库后再清
        break;
      }
      case "token.delta": {
        const aid = Number(payload.agent_id);
        const delta = String(payload.delta ?? "");
        if (aid && delta) {
          set((s) => ({ streamingBuffers: { ...s.streamingBuffers, [aid]: (s.streamingBuffers[aid] || "") + delta } }));
        }
        break;
      }
      case "token.done": {
        // v1.3: 不立即清空 buffer，等 message.created 落库后再清
        // 避免流式正文"刷完就消失"（落库消息到达前 buffer 保留显示）
        break;
      }
      case "tool.call":
      case "tool.result":
        // 工具调用由 message.created 落库驱动展示；此处仅触发消息刷新兜底
        break;
      case "task.updated": {
        const taskId = Number(payload.task_id);
        const status = String(payload.status ?? "");
        const note = payload.note != null ? String(payload.note) : null;
        set((s) => ({
          tasks: s.tasks.map((t) => (t.id === taskId ? { ...t, status: status || t.status, ...(note ? { note } : {}) } : t)),
        }));
        break;
      }
      case "usage.update": {
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
      case "session.completed":
        set({ isRunning: false, runningTurnId: null });
        get().refreshTurns();
        get().refreshTasks();
        break;
      case "error":
        set({ error: String((payload as { message?: string }).message ?? "未知错误") });
        break;
      default:
        break;
    }
    void st;
  },
}));
