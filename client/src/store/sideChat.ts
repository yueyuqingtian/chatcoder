/** 侧边会话状态仓库（plan-20261017-side-session P2）。
 *
 * 侧边会话是主会话的子会话：它是一个完整会话，turn 流水线与工具调用和主会话一致。
 * 本仓库只为「右侧面板里的一个子会话」保存视图状态（消息、turn、流式缓冲、审批），
 * 与主聊天仓库（chat.ts）完全隔离：主对话的任何代码路径都不会读到侧边的状态，反之亦然。
 *
 * 数据来源：
 *  - 打开时：REST 拉取消息与 turn（以数据库为准）；
 *  - 运行中：该子会话自己的 WebSocket 事件（流式增量写入缓冲，turn 相关事件触发 REST 对账）。
 * turn 的运行状态始终以 turn 列表为准（running 即运行中），事件只作为刷新信号，避免事件乱序导致状态漂移。 */
import { create } from "zustand";
import { api, type ComposerRefOut, type MessageOut, type TurnOut } from "../api/client";
import { WsClient } from "../api/ws";
import { pendingActions } from "./pendingInteractions";

export interface SideApproval { approvalId: string; detail: Record<string, unknown> }

export interface SideConvo {
  sessionId: number;
  loaded: boolean;
  messages: MessageOut[];
  turns: TurnOut[];
  streamingBuffers: Record<number, string>;
  thinkingBuffers: Record<number, string>;
  runningTurnId: number | null;
  isRunning: boolean;
  turnStatus: string | null;
  /** 待随下一条提问发送的主对话上下文（由面板「引用主对话」写入，发送后清空） */
  contextDraft: string;
  approvalQueue: SideApproval[];
  pendingApproval: SideApproval | null;
  error: string | null;
}

export interface SendSidePayload {
  content: string;
  attachments?: Record<string, unknown>[];
  reasoningEffort?: string;
  mode?: string | null;
  modelId?: number | null;
  refs?: ComposerRefOut[];
}

interface SideChatState {
  convos: Record<number, SideConvo>;
  openSide: (sessionId: number) => Promise<void>;
  closeSide: (sessionId: number) => void;
  refreshSide: (sessionId: number) => Promise<void>;
  sendSide: (sessionId: number, payload: SendSidePayload) => Promise<void>;
  stopSide: (sessionId: number) => Promise<void>;
  respondSide: (sessionId: number, approvalId: string, approved: boolean, remember?: boolean, answer?: Record<string, unknown>) => void;
  clearSideError: (sessionId: number) => void;
  setSideContext: (sessionId: number, text: string) => void;
}

const sockets = new Map<number, WsClient>();
/** 引用计数：同一子会话可能被多个面板实例同时挂载（右侧面板的隐藏标签会保活渲染），只有最后一个释放时才断开连接 */
const refs = new Map<number, number>();

function emptyConvo(sessionId: number): SideConvo {
  return {
    sessionId,
    loaded: false,
    messages: [],
    turns: [],
    streamingBuffers: {},
    thinkingBuffers: {},
    runningTurnId: null,
    isRunning: false,
    turnStatus: null,
    contextDraft: "",
    approvalQueue: [],
    pendingApproval: null,
    error: null,
  };
}

/** 上下文块：把所选的主对话轮次格式化为一段前置文本，随子会话的第一条消息发给模型。 */
export function buildContextBlock(turns: Array<{ user: string; assistant: string }>): string {
  if (turns.length === 0) return "";
  const body = turns
    .map((t, i) => `【第 ${i + 1} 轮】\n用户：${t.user.trim()}\n助手：${t.assistant.trim()}`)
    .join("\n\n");
  return `以下是主对话中与本次任务相关的上下文（仅供参考，不必逐条复述）：\n\n${body}\n\n---\n\n`;
}

function turnKey(payload: Record<string, unknown>, fallback: number | null): number {
  const t = payload.turn_id;
  if (typeof t === "number") return t;
  return fallback ?? 0;
}

export const useSideChatStore = create<SideChatState>((set, get) => {
  const patch = (sessionId: number, fn: (c: SideConvo) => Partial<SideConvo>) => {
    set((s) => {
      const cur = s.convos[sessionId] ?? emptyConvo(sessionId);
      return { convos: { ...s.convos, [sessionId]: { ...cur, ...fn(cur) } } };
    });
  };

  /** 以 turn 列表为准派生运行状态 */
  const applyTurns = (sessionId: number, turns: TurnOut[]) => {
    const running = turns.find((t) => t.status === "running") ?? null;
    patch(sessionId, () => ({
      turns,
      runningTurnId: running ? running.id : null,
      isRunning: Boolean(running),
    }));
  };

  const refreshTurns = async (sessionId: number) => {
    try {
      applyTurns(sessionId, await api.listTurns(sessionId));
    } catch (e) {
      patch(sessionId, () => ({ error: e instanceof Error ? e.message : String(e) }));
    }
  };

  const refreshMessages = async (sessionId: number) => {
    try {
      const messages = await api.listSessionMessages(sessionId);
      patch(sessionId, () => ({ messages }));
    } catch (e) {
      patch(sessionId, () => ({ error: e instanceof Error ? e.message : String(e) }));
    }
  };

  const applyEvent = (sessionId: number, ev: { event: string; payload: Record<string, unknown> }) => {
    const p = ev.payload || {};
    const cur = get().convos[sessionId] ?? emptyConvo(sessionId);
    switch (ev.event) {
      case "message.created": {
        const msg = p.msg as MessageOut | undefined;
        if (!msg || Number(msg.session_id) !== sessionId) return;
        patch(sessionId, (c) => {
          if (c.messages.some((m) => m.id === msg.id)) return {};
          // 同一 turn 的最终回复落库后，清掉对应的流式缓冲，避免文字重复显示
          const sb = { ...c.streamingBuffers };
          const tb = { ...c.thinkingBuffers };
          if (msg.turn_id != null && msg.sender_type !== "user") {
            delete sb[msg.turn_id];
            delete tb[msg.turn_id];
          }
          // 真实用户消息到达时，移除本地的乐观副本（负数临时 id）
          const isOwnUser = msg.sender_type === "user";
          const messages = [...c.messages.filter((m) => !(isOwnUser && m.id < 0)), msg];
          return { messages, streamingBuffers: sb, thinkingBuffers: tb };
        });
        return;
      }
      case "token.delta": {
        const key = turnKey(p, cur.runningTurnId);
        const delta = String(p.delta ?? "");
        patch(sessionId, (c) => ({ streamingBuffers: { ...c.streamingBuffers, [key]: (c.streamingBuffers[key] ?? "") + delta } }));
        return;
      }
      case "thinking.delta": {
        const key = turnKey(p, cur.runningTurnId);
        const delta = String(p.delta ?? "");
        patch(sessionId, (c) => ({ thinkingBuffers: { ...c.thinkingBuffers, [key]: (c.thinkingBuffers[key] ?? "") + delta } }));
        return;
      }
      case "token.done":
      case "thinking.done": {
        const key = turnKey(p, cur.runningTurnId);
        patch(sessionId, (c) => {
          const next = ev.event === "token.done" ? { ...c.streamingBuffers } : { ...c.thinkingBuffers };
          delete next[key];
          return ev.event === "token.done" ? { streamingBuffers: next } : { thinkingBuffers: next };
        });
        return;
      }
      case "turn.started":
      case "turn.updated":
        void refreshTurns(sessionId);
        return;
      case "turn.completed":
      case "turn.failed":
      case "turn.interrupted": {
        const turnId = typeof p.turn_id === "number" ? p.turn_id : null;
        patch(sessionId, (c) => {
          const sb = { ...c.streamingBuffers };
          const tb = { ...c.thinkingBuffers };
          if (turnId != null) { delete sb[turnId]; delete tb[turnId]; }
          return { streamingBuffers: sb, thinkingBuffers: tb, turnStatus: null };
        });
        void refreshTurns(sessionId);
        void refreshMessages(sessionId);
        return;
      }
      case "turn.status":
        patch(sessionId, () => ({ turnStatus: p.text ? String(p.text) : null }));
        return;
      case "tool.result":
        void refreshMessages(sessionId);
        return;
      case "approval.request": {
        const approvalId = String(p.approval_id ?? "");
        if (!approvalId) return;
        const item: SideApproval = { approvalId, detail: (p.detail as Record<string, unknown>) ?? {} };
        patch(sessionId, (c) => {
          if (c.approvalQueue.some((a) => a.approvalId === approvalId)) return {};
          const queue = [...c.approvalQueue, item];
          return { approvalQueue: queue, pendingApproval: queue[0] };
        });
        return;
      }
      case "approval.response": {
        const approvalId = String(p.approval_id ?? "");
        patch(sessionId, (c) => {
          const queue = c.approvalQueue.filter((a) => a.approvalId !== approvalId);
          return { approvalQueue: queue, pendingApproval: queue[0] ?? null };
        });
        return;
      }
      default:
        return;
    }
  };

  return {
    convos: {},

    openSide: async (sessionId) => {
      const first = (refs.get(sessionId) ?? 0) === 0;
      refs.set(sessionId, (refs.get(sessionId) ?? 0) + 1);
      // 侧边会话以服务端为准：首次打开（含切回）时先清空上一次的视图数据，
      // 否则切换标签会先显示旧内容，两个会话看起来就是「一模一样」。
      if (first) {
        patch(sessionId, () => ({
          messages: [], turns: [], streamingBuffers: {}, thinkingBuffers: {},
          runningTurnId: null, isRunning: false, turnStatus: null,
          pendingApproval: null, approvalQueue: [], error: null, loaded: false,
        }));
      } else if (!get().convos[sessionId]) {
        patch(sessionId, () => ({}));
      }
      if (!sockets.has(sessionId)) {
        const sock = new WsClient();
        sock.connect(sessionId);
        sock.on((ev) => applyEvent(sessionId, ev));
        sockets.set(sessionId, sock);
      }
      await Promise.all([refreshTurns(sessionId), refreshMessages(sessionId)]);
      patch(sessionId, () => ({ loaded: true }));
    },

    closeSide: (sessionId) => {
      const left = (refs.get(sessionId) ?? 1) - 1;
      if (left > 0) {
        refs.set(sessionId, left);
        return;
      }
      refs.delete(sessionId);
      const sock = sockets.get(sessionId);
      if (sock) {
        sock.disconnect();
        sockets.delete(sessionId);
      }
    },

    refreshSide: async (sessionId) => {
      await Promise.all([refreshTurns(sessionId), refreshMessages(sessionId)]);
    },

    sendSide: async (sessionId, payload) => {
      const typed = payload.content ?? "";
      const hasAtts = Array.isArray(payload.attachments) && payload.attachments.length > 0;
      if (!typed.trim() && !hasAtts) return;
      // 引用的主对话上下文只随本次提问发送一次：拼在发给模型的内容前面，本地显示仍只展示用户所写的内容
      const context = get().convos[sessionId]?.contextDraft ?? "";
      const content = context ? `${context}${typed}` : typed;
      if (context) patch(sessionId, () => ({ contextDraft: "" }));
      // 乐观显示：临时 id 取负数，与数据库 id 不冲突；对账时整体替换为数据库消息
      const tempId = -Date.now();
      const optimistic: MessageOut = {
        id: tempId,
        session_id: sessionId,
        turn_id: null,
        thread_id: null,
        sender_type: "user",
        sender_id: null,
        msg_type: "text",
        content: hasAtts ? { text: typed, attachments: payload.attachments } : { text: typed },
        token_usage: 0,
        created_at: new Date().toISOString(),
      };
      patch(sessionId, (c) => ({ messages: [...c.messages, optimistic], isRunning: true, error: null }));
      try {
        await api.createTurn({
          session_id: sessionId,
          content,
          attachments: payload.attachments,
          reasoning_effort: payload.reasoningEffort,
          mode: payload.mode ?? null,
          model_id: payload.modelId ?? undefined,
          refs: payload.refs,
        });
        await Promise.all([refreshTurns(sessionId), refreshMessages(sessionId)]);
      } catch (e) {
        patch(sessionId, (c) => ({
          messages: c.messages.filter((m) => m.id !== tempId),
          isRunning: false,
          // 发送失败：把已随本次提问清空的引用上下文放回去（若用户又加了新的，接在其后），避免选好的上下文被静默丢弃
          contextDraft: context ? context + c.contextDraft : c.contextDraft,
          error: e instanceof Error ? e.message : String(e),
        }));
      }
    },

    stopSide: async (sessionId) => {
      const turnId = get().convos[sessionId]?.runningTurnId;
      if (turnId == null) return;
      try {
        await api.cancelTurn(turnId);
      } catch (e) {
        patch(sessionId, () => ({ error: e instanceof Error ? e.message : String(e) }));
      }
    },

    respondSide: (sessionId, approvalId, approved, remember = false, answer) => {
      sockets.get(sessionId)?.send("approval.response", {
        approval_id: approvalId, approved, remember, remember_scope: "session",
        ...(answer ? { answer } : {}),
      });
      patch(sessionId, (c) => {
        const queue = c.approvalQueue.filter((a) => a.approvalId !== approvalId);
        return { approvalQueue: queue, pendingApproval: queue[0] ?? null };
      });
      // 会话隔离修复：同步摘掉侧栏等待索引里的这一条，避免回执丢失后侧边会话一直显示等待。
      pendingActions.clearByApprovalId(approvalId);
    },

    clearSideError: (sessionId) => patch(sessionId, () => ({ error: null })),

    setSideContext: (sessionId, text) => patch(sessionId, () => ({ contextDraft: text })),
  };
});
