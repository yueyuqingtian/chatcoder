/** plan-522-2209：跨会话「待处理交互」索引（审批 / 提问）。
 *
 *  为什么独立成 store 而不是并进 chat.ts：
 *  chat.ts 的 approvalQueue 只服务**当前会话**（切会话即重建），侧栏却要显示「任意会话在等你」，
 *  跨会话状态必须与当前会话视图解耦，否则切会话就会把别的会话的等待依据一起清掉。
 *
 *  为什么保留 detail：
 *  用户在任意会话看到子会话的审批卡时，卡片要渲染工具名、动作短语与命令原文；只有
 *  「有个审批在等你」而不给内容，等于逼用户切过去看，跨会话可见性就失去意义。
 *  detail 在作答 / 取消时立即清除（见 clearRequest），不会长期滞留。
 */
import { create } from "zustand";

export type PendingKind = "approval" | "question";

/** 单条等待摘要 */
export interface PendingEntry {
  kind: PendingKind;
  /** 到达时间（毫秒），用于「最早到达」排序 */
  createdAt: number;
  /** 审批详情（用于跨会话渲染审批卡；作答后随条目一起清除） */
  detail: Record<string, unknown>;
}

/** 会话 id → (approvalId → 摘要)。
 *  用 map 而不是单值：同一会话可能同时有多个审批排队（并行子代理各请求一次），
 *  审批与提问也会并存，单值会互相覆盖。 */
export type PendingBySession = Record<number, Record<string, PendingEntry>>;

interface PendingInteractionsState {
  pendingBySession: PendingBySession;
}

/** kind 判定：detail.kind === "question" 视为提问，其余一律按审批处理
 *  （服务端 detail 的 kind 只有这两种语义，工具的普通审批不带 kind）。 */
export function kindOfDetail(detail: Record<string, unknown> | null | undefined): PendingKind {
  return detail?.kind === "question" ? "question" : "approval";
}

export const usePendingInteractions = create<PendingInteractionsState>(() => ({
  pendingBySession: {},
}));

export const pendingActions = {
  /** 记录一条到达的审批 / 提问。同一 approvalId 重放只记一次
   *  （全局通道重连会补发历史事件，重复记录会让计数虚高）。 */
  noteRequest: (
    sessionId: number,
    approvalId: string,
    kind: PendingKind,
    detail: Record<string, unknown> = {},
  ) => {
    if (!sessionId || !approvalId) return;
    usePendingInteractions.setState((s) => {
      const bucket = s.pendingBySession[sessionId];
      // 已存在即重放：保留首次到达时间（排序要按真实先后，不能让重放把它排到后面），
      // 但补全 detail（重放事件可能比首次到达时携带的信息更完整）
      if (bucket && bucket[approvalId]) {
        if (Object.keys(detail).length === 0) return {};
        return {
          pendingBySession: {
            ...s.pendingBySession,
            [sessionId]: {
              ...bucket,
              [approvalId]: { ...bucket[approvalId], detail },
            },
          },
        };
      }
      return {
        pendingBySession: {
          ...s.pendingBySession,
          [sessionId]: {
            ...(bucket || {}),
            [approvalId]: { kind, createdAt: Date.now(), detail },
          },
        },
      };
    });
  },

  /** 清除已作答 / 已取消的审批。整个会话桶空了就一并删掉，避免空对象堆积。 */
  clearRequest: (sessionId: number, approvalId: string) => {
    if (!sessionId || !approvalId) return;
    usePendingInteractions.setState((s) => {
      const bucket = s.pendingBySession[sessionId];
      if (!bucket || !bucket[approvalId]) return {};
      const nextBucket = { ...bucket };
      delete nextBucket[approvalId];
      const next = { ...s.pendingBySession };
      if (Object.keys(nextBucket).length === 0) delete next[sessionId];
      else next[sessionId] = nextBucket;
      return { pendingBySession: next };
    });
  },

  /** 按 approvalId 全局清除（作答 / 回执兜底）。
   *  会话隔离修复：跨会话作答时回执按提交方会话广播，只按会话清会留下幽灵条目，
   *  切回原会话后卡片复活、反复回到第 1 题。approvalId 全局唯一，可直接跨桶定位。 */
  clearByApprovalId: (approvalId: string) => {
    if (!approvalId) return;
    usePendingInteractions.setState((s) => {
      let changed = false;
      const next: PendingBySession = {};
      for (const sidKey of Object.keys(s.pendingBySession)) {
        const sessionId = Number(sidKey);
        const bucket = s.pendingBySession[sessionId];
        if (!bucket[approvalId]) {
          next[sessionId] = bucket;
          continue;
        }
        changed = true;
        const rest = { ...bucket };
        delete rest[approvalId];
        if (Object.keys(rest).length > 0) next[sessionId] = rest;
      }
      return changed ? { pendingBySession: next } : {};
    });
  },

  /** 清空（断线重连后由 REST 重建、或会话数据整体重载时使用）。 */
  reset: () => {
    usePendingInteractions.setState({ pendingBySession: {} });
  },
};

// ── 选择器：普通函数，供 utils/sessionWaiting.ts 与组件直接调用（非 hook，可在事件回调里用） ──

/** 该会话是否有等待中的审批 / 提问。 */
export function hasPending(sessionId: number): boolean {
  const bucket = usePendingInteractions.getState().pendingBySession[sessionId];
  return Boolean(bucket && Object.keys(bucket).length > 0);
}

/** 该会话等待中的条目数（并发审批时用于「另有 N 项」）。 */
export function pendingCountOf(sessionId: number): number {
  const bucket = usePendingInteractions.getState().pendingBySession[sessionId];
  return bucket ? Object.keys(bucket).length : 0;
}

/** 全部会话等待中的总条目数。 */
export function totalCount(): number {
  const all = usePendingInteractions.getState().pendingBySession;
  let n = 0;
  for (const key of Object.keys(all)) n += Object.keys(all[Number(key)]).length;
  return n;
}

/** 全局最早到达的一条等待（跨会话）。没有等待时返回 null。
 *  输入区审批卡在「当前会话无队首」时用它回退，保证任何会话的等待都能被看到。
 *  带上 detail 与 sessionId：卡片要渲染工具名与命令原文，来源标注也要靠 sessionId 反查标题。 */
export function earliestPending(): {
  sessionId: number;
  approvalId: string;
  kind: PendingKind;
  detail: Record<string, unknown>;
} | null {
  const all = usePendingInteractions.getState().pendingBySession;
  let best: {
    sessionId: number; approvalId: string; kind: PendingKind;
    createdAt: number; detail: Record<string, unknown>;
  } | null = null;
  for (const sidKey of Object.keys(all)) {
    const sessionId = Number(sidKey);
    const bucket = all[sessionId];
    for (const approvalId of Object.keys(bucket)) {
      const entry = bucket[approvalId];
      if (!best || entry.createdAt < best.createdAt) {
        best = {
          sessionId, approvalId, kind: entry.kind,
          createdAt: entry.createdAt, detail: entry.detail,
        };
      }
    }
  }
  if (!best) return null;
  return {
    sessionId: best.sessionId,
    approvalId: best.approvalId,
    kind: best.kind,
    detail: best.detail,
  };
}
