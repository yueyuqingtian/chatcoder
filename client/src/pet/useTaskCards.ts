/** 多任务状态机（plan-73-323 阶段2；设计见方案三章 3.1/3.2/3.6/3.7/3.8/3.10）。
 *
 * 信息模型：**一个运行中的会话 = 一张任务卡**。子代理、工具调用、清单更新都归并到父卡，
 * 不单独占卡（同类产品共识：过滤内部任务，否则列表会被内部任务淹没）。
 *
 * 数据来源：全局通道事件为增量主源；REST 快照只做「启动基线 + 60s 校正 + 重连对齐」。
 * 步骤状态归一化口径与主窗 `components/chat/taskProgress.ts` 保持一致
 * （in_progress→running、cancelled/failed→interrupted、done/completed→done），
 * 避免同一个任务在主窗与宠物处出现"两种说法"。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { CONNECTED_EVENT, RECONNECTED_EVENT, fetchSessions, fetchTasks, petWs, type PetServerEvent } from "./petEvents";
import type { SessionSnapshot, TaskRow } from "./petApi";

export type TaskStatus = "running" | "waiting" | "failed";
export type StepStatus = "pending" | "running" | "done" | "interrupted";

export interface TaskStep {
  content: string;
  activeForm: string;
  status: StepStatus;
}

export interface TaskCard {
  sessionId: number;
  title: string;
  status: TaskStatus;
  /** 当前 turn（用于 todo.updated 的归属校验：只采纳当前 turn 的清单） */
  turnId: number | null;
  /** 开始执行时间（ms）——优先取快照 running_started_at，与侧栏排序口径一致 */
  startedAt: number | null;
  steps: TaskStep[];
  stepsSource: "todo" | "engine" | null;
  currentAction: string;
  lastResult: { tool: string; ok: boolean; at: number } | null;
  subagentCount: number;
  waitingReason: string;
  /** 等待确认时的待处理动作（工具名 + 入参预览）——浮窗用它说明「在等什么」，
   *  比只显示裸工具名可读（plan-73-340） */
  pendingAction: { tool: string; argsPreview: string } | null;
  errorText: string;
  goalText: string;
  goalTurnsUsed: number;
  /** 最近一次收到该会话事件的时间（静默超时与快照校正的保护依据） */
  lastEventAt: number;
}

export interface RecentDone {
  sessionId: number;
  title: string;
  at: number;
}

/** 聚合态：宠物本体与面板头部用它表达"全局最要紧的那件事" */
export type PetAggregate = "failed" | "waiting" | "running" | "idle";

/** 任务状态优先级（越小越要紧）：失败 > 等待 > 运行 —— 与方案 3.6 一致 */
const STATUS_PRIORITY: Record<TaskStatus, number> = { failed: 0, waiting: 1, running: 2 };

/** 单卡静默超时（方案 3.6：防幽灵卡） */
const CARD_IDLE_TIMEOUT_MS = 15 * 60 * 1000;
/** 快照校正：移除卡片前要求"至少这么久没收到该会话事件"，避免快照迟到误杀刚起的任务 */
const SNAPSHOT_GUARD_MS = 5000;
const SNAPSHOT_INTERVAL_MS = 60 * 1000;
const SWEEP_INTERVAL_MS = 30 * 1000;
const RECENT_KEEP = 3;

function num(v: unknown): number | null {
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function str(v: unknown): string {
  return typeof v === "string" ? v : "";
}

function parseIso(v: unknown): number | null {
  const s = str(v);
  if (!s) return null;
  const t = Date.parse(s);
  return Number.isFinite(t) ? t : null;
}

/** 步骤状态归一化（与主窗 taskProgress.ts 同口径） */
export function normalizeStep(status: unknown): StepStatus {
  const s = str(status);
  if (s === "in_progress" || s === "running") return "running";
  if (s === "cancelled" || s === "failed") return "interrupted";
  if (s === "done" || s === "completed") return "done";
  return "pending";
}

/** 解析 todo.updated 的清单（服务端已把 content/activeForm 截断到 200 字） */
function parseTodos(raw: unknown): TaskStep[] {
  if (!Array.isArray(raw)) return [];
  const out: TaskStep[] = [];
  for (const item of raw) {
    if (!item || typeof item !== "object") continue;
    const o = item as Record<string, unknown>;
    const content = str(o.content).trim();
    if (!content) continue;
    out.push({ content, activeForm: str(o.activeForm).trim(), status: normalizeStep(o.status) });
    if (out.length >= 12) break; // 服务端清单上限 12 项，超出的视为异常数据
  }
  return out;
}

/** 展示态兜底：任务确实在跑且清单里没有"进行中"项时，把首个待办标为进行中 */
function withRunningFallback(steps: TaskStep[], running: boolean): TaskStep[] {
  if (!running || steps.length === 0) return steps;
  if (steps.some((s) => s.status === "running")) return steps;
  const idx = steps.findIndex((s) => s.status === "pending");
  if (idx < 0) return steps;
  const out = steps.slice();
  out[idx] = { ...out[idx], status: "running" };
  return out;
}

function newCard(sessionId: number, now: number, patch?: Partial<TaskCard>): TaskCard {
  return {
    sessionId,
    title: `会话 ${sessionId}`,
    status: "running",
    turnId: null,
    startedAt: now,
    steps: [],
    stepsSource: null,
    currentAction: "",
    lastResult: null,
    subagentCount: 0,
    waitingReason: "",
    pendingAction: null,
    errorText: "",
    goalText: "",
    goalTurnsUsed: 0,
    lastEventAt: now,
    ...patch,
  };
}

/** 自动排序（方案 3.7）：状态优先级 → 开始时间倒序 → 会话 id 兜底（顺序确定、不抖动） */
function autoSort(list: TaskCard[]): TaskCard[] {
  return list.slice().sort((a, b) => {
    const pa = STATUS_PRIORITY[a.status];
    const pb = STATUS_PRIORITY[b.status];
    if (pa !== pb) return pa - pb;
    const ta = a.startedAt ?? 0;
    const tb = b.startedAt ?? 0;
    if (tb !== ta) return tb - ta;
    return b.sessionId - a.sessionId;
  });
}

/** 排序：**用户拖拽调序优先**（plan-73-340 追加需求）。
 *
 *  `order` 是显式的会话 id 序列（索引 0 = 最外层/最靠近宠物）；列表里出现过的会话
 *  按它在 order 中的位置排前，未出现过的（新任务）回落到自动排序规则并排在后面——
 *  这样「新起的任务」不会打乱用户已经排好的顺序，也不会永远被压在最后。
 */
function sortCards(list: TaskCard[], order: number[]): TaskCard[] {
  if (!order || order.length === 0) return autoSort(list);
  const rank = new Map<number, number>();
  order.forEach((id, i) => rank.set(id, i));
  const pinned = list.filter((c) => rank.has(c.sessionId));
  const rest = list.filter((c) => !rank.has(c.sessionId));
  pinned.sort((a, b) => (rank.get(a.sessionId) ?? 0) - (rank.get(b.sessionId) ?? 0));
  return [...pinned, ...autoSort(rest)];
}

/** 聚合（方案 3.6）：取最要紧的任务状态 */
function computeAggregate(cards: TaskCard[]): {
  status: PetAggregate;
  total: number;
  waiting: number;
  failed: number;
} {
  let status: PetAggregate = "idle";
  let waiting = 0;
  let failed = 0;
  for (const c of cards) {
    if (c.status === "failed") failed++;
    if (c.status === "waiting") waiting++;
  }
  if (failed > 0) status = "failed";
  else if (waiting > 0) status = "waiting";
  else if (cards.length > 0) status = "running";
  return { status, total: cards.length, waiting, failed };
}

/**
 * 多任务状态机。
 *
 * @param port       后端端口（来自 getBoot）
 * @param focused    浮窗/面板是否被聚焦 —— 决定合帧节奏（100ms / 250ms）
 * @param order      用户拖拽调序的显式会话顺序（pref.blockOrder）；为空则走自动排序
 * @param bootTitles 主进程持久化的会话标题缓存（id → title）：冷启动即可显示真实标题
 */
export function useTaskCards(
  port: number | null,
  focused: boolean,
  order: number[] = [],
  bootTitles: Record<string, string> = {}
) {
  const mapRef = useRef(new Map<number, TaskCard>());
  const dirtyRef = useRef(false);
  const recentRef = useRef<RecentDone[]>([]);
  const focusedRef = useRef(focused);
  const versionRef = useRef(0);
  /** 显式顺序用 ref 跟随：flush 是稳定回调，读取最新值即可，无需重建 */
  const orderRef = useRef<number[]>(order);
  /** 会话标题缓存：初始化自主进程持久缓存（plan-73-341），运行时由快照与
   *  session.updated 持续补齐。建卡时优先取它，避免浮窗显示「会话 N」。
   *  注：旧版本主进程不下发 `titles`，故此处必须兜底空表，否则 `Object.entries` 会抛错。 */
  const titlesRef = useRef(new Map<number, string>(
    Object.entries(bootTitles || {}).map(([k, v]) => [Number(k), v])
  ));
  /** 待补标题的会话集合 + 重试轮次（指数退避：0.6s → 1s → 2s → 4s → 8s，最多 5 轮）。
   *  为什么不是"一次性补拉"：那是历史缺陷 —— 只试一次，失败即静默，
   *  标题就长期停在兜底值（用户反馈"改了几次还是不对"）。 */
  const pendingTitlesRef = useRef(new Set<number>());
  const titleRetryRef = useRef<{ timer: number; round: number }>({ timer: 0, round: 0 });
  const [view, setView] = useState<{ cards: TaskCard[]; recent: RecentDone[] }>({ cards: [], recent: [] });

  focusedRef.current = focused;
  orderRef.current = order;

  const flush = useCallback(() => {
    if (!dirtyRef.current) return;
    dirtyRef.current = false;
    versionRef.current++;
    setView({
      cards: sortCards([...mapRef.current.values()], orderRef.current),
      recent: recentRef.current.slice(),
    });
  }, []);

  /** 标记数据已变更，等待下一个合帧窗口提交 */
  const markDirty = useCallback(() => {
    dirtyRef.current = true;
  }, []);

  /** 用户拖拽调序后（order 变化）立即重排一次——否则要等下一个事件才生效 */
  useEffect(() => {
    markDirty();
  }, [order, markDirty]);

  /** 快照校正：补建漏掉的运行任务、清掉已收尾的幽灵卡、补齐标题与目标。
   *  定义在 handleEvent 之前——handleEvent 与 requestTitles 都依赖它。 */
  const correctFromSnapshot = useCallback(async () => {
    const list = await fetchSessions();
    if (!list.length) return;
    const now = Date.now();
    const map = mapRef.current;
    const byId = new Map<number, SessionSnapshot>();
    for (const s of list) {
      byId.set(s.id, s);
      // 标题缓存：后续建卡直接用真实标题，不再回落到「会话 N」
      if (s.title) titlesRef.current.set(s.id, s.title);
    }

    for (const [sid, card] of [...map.entries()]) {
      const snap = byId.get(sid);
      if (!snap) continue;
      if (snap.title) card.title = snap.title;
      if (snap.goal_text) card.goalText = snap.goal_text;
      if (snap.goal_turns_used != null) card.goalTurnsUsed = Number(snap.goal_turns_used) || 0;
      if (!snap.has_running && now - card.lastEventAt > SNAPSHOT_GUARD_MS) {
        // 快照说它没在跑，且近期也没有事件 —— 判定为幽灵卡（丢失 session.completed 的兜底）
        map.delete(sid);
        recentRef.current = [
          { sessionId: sid, title: card.title, at: now },
          ...recentRef.current.filter((r) => r.sessionId !== sid),
        ].slice(0, RECENT_KEEP);
      }
    }

    for (const s of list) {
      if (!s.has_running || map.has(s.id)) continue;
      const started = parseIso(s.running_started_at) ?? now;
      map.set(s.id, newCard(s.id, now, {
        title: s.title || `会话 ${s.id}`,
        startedAt: started,
        goalText: s.goal_text || "",
        goalTurnsUsed: Number(s.goal_turns_used) || 0,
      }));
    }
    markDirty();
  }, [markDirty]);

  /** 标记某会话的标题待补，并按**指数退避**重试直到拿到（plan-73-341）。
   *
   *  为什么不用"一次性去抖补拉"：那是历史缺陷 —— 只尝试一次，失败被静默吞掉，
   *  标题就长期停在兜底值「会话 N」（用户反馈"改了几次还是不对"）。
   *  现在：0.6s → 1s → 2s → 4s → 8s，最多 5 轮；拿到即移出待补集合，跑完仍无果则告警放弃。 */
  const requestTitles = useCallback((sessionId: number) => {
    if (sessionId) pendingTitlesRef.current.add(sessionId);
    const st = titleRetryRef.current;
    if (st.timer) return; // 已有在途重试，共用同一轮

    const settled = () => {
      // 已拿到标题的会话移出待补集合
      for (const id of [...pendingTitlesRef.current]) {
        if (titlesRef.current.get(id)) pendingTitlesRef.current.delete(id);
      }
    };

    const run = () => {
      st.timer = 0;
      settled();
      if (pendingTitlesRef.current.size === 0) { st.round = 0; return; }
      if (st.round >= 5) {
        console.warn("[pet] 会话标题补拉失败（已重试 5 轮）:", [...pendingTitlesRef.current]);
        pendingTitlesRef.current.clear();
        st.round = 0;
        return;
      }
      void correctFromSnapshot().finally(() => {
        settled();
        if (pendingTitlesRef.current.size === 0) { st.round = 0; return; }
        st.round++;
        const delay = Math.min(600 * 2 ** st.round, 8000);
        st.timer = window.setTimeout(run, delay);
      });
    };
    st.timer = window.setTimeout(run, 600);
  }, [correctFromSnapshot]);

  // 卸载时清掉在途重试，避免无主定时器
  useEffect(() => () => {
    if (titleRetryRef.current.timer) window.clearTimeout(titleRetryRef.current.timer);
  }, []);

  const handleEvent = useCallback(
    (ev: PetServerEvent) => {
      const now = Date.now();
      const map = mapRef.current;
      const p = (ev.payload || {}) as Record<string, unknown>;
      const sid = num(p.session_id);
      const event = ev.event;

      // 连接相关事件（都是"拉一次快照校正状态"的时机）：
      //  · 全局通道首次建连 → 建立基线；
      //  · 会话级通道重连成功 → 增量无法重放，但"当前状态"可以经 REST 补齐（plan-73-341）。
      if (event === CONNECTED_EVENT || event === RECONNECTED_EVENT) {
        void correctFromSnapshot();
        return;
      }
      // 无 session_id 的事件（如符号索引进度）与宠物卡片无关，直接忽略
      if (!sid) return;

      switch (event) {
        case "turn.started": {
          const turnId = num(p.turn_id);
          const card = map.get(sid);
          if (card) {
            // 新一轮：清空上一轮的清单与瞬时态（旧清单不能当作本轮进度）
            card.status = "running";
            card.turnId = turnId;
            card.steps = [];
            card.stepsSource = null;
            card.currentAction = "";
            card.waitingReason = "";
            card.pendingAction = null;
            card.errorText = "";
            card.subagentCount = 0;
            card.startedAt = card.startedAt ?? now;
          } else {
            // 建卡：优先用缓存的真实标题；没缓存则先用编号兜底，
            // 并启动「指数退避补拉」直到拿到真标题（不再只试一次）
            const cached = titlesRef.current.get(sid);
            map.set(sid, newCard(sid, now, {
              turnId,
              title: cached || `会话 ${sid}`,
            }));
            if (!cached) requestTitles(sid);
          }
          break;
        }
        case "turn.updated": {
          const card = map.get(sid);
          if (!card) break;
          if (str(p.status) === "running") card.status = "running";
          break;
        }
        case "todo.updated": {
          const card = map.get(sid);
          if (!card) break;
          const tid = num(p.turn_id);
          if (card.turnId != null && tid != null && tid !== card.turnId) break; // 旧轮/子代理的清单不采纳
          card.steps = parseTodos(p.todos);
          card.stepsSource = "todo";
          if (card.status !== "waiting" && card.status !== "failed") card.status = "running";
          break;
        }
        case "tool.call": {
          const card = map.get(sid);
          if (!card) break;
          const tool = str(p.tool);
          const argsPreview = str(p.args_preview);
          // 记录待处理动作：等待审批时浮窗用它说明「在等哪一步」（plan-73-340）
          card.pendingAction = { tool, argsPreview };
          const args = argsPreview.replace(/\s+/g, " ").trim();
          card.currentAction = args ? `${tool} · ${args}` : tool;
          if (card.status !== "waiting" && card.status !== "failed") card.status = "running";
          break;
        }
        case "tool.result": {
          const card = map.get(sid);
          if (!card) break;
          card.lastResult = { tool: str(p.tool) || "工具", ok: p.ok !== false, at: now };
          break;
        }
        case "approval.request": {
          const card = map.get(sid);
          if (!card) break;
          const detail = (p.detail || {}) as Record<string, unknown>;
          card.status = "waiting";
          card.waitingReason = str(detail.tool) || str(detail.title) || "等待确认";
          // 优先用审批详情里的工具/参数；缺失则沿用最近一次 tool.call 的上下文，
          // 这样浮窗能显示「等待确认 · 执行命令 · npm run build」而不是裸工具名
          const tool = str(detail.tool) || card.pendingAction?.tool || "";
          const argsPreview = str(detail.args_preview) || card.pendingAction?.argsPreview || "";
          if (tool) card.pendingAction = { tool, argsPreview };
          break;
        }
        case "turn.failed": {
          const card = map.get(sid);
          if (!card) break;
          card.status = "failed";
          card.errorText = (str(p.error) || str(p.summary) || "执行失败").slice(0, 120);
          break;
        }
        case "subagent.pending": {
          const card = map.get(sid);
          if (!card) break;
          card.subagentCount = Math.max(0, num(p.pending) ?? 0);
          if (card.subagentCount > 0 && card.status === "running") card.currentAction = card.currentAction || "子代理执行中";
          break;
        }
        case "subagent.wakeup": {
          const card = map.get(sid);
          if (!card) break;
          if (card.status !== "failed") card.status = "running";
          break;
        }
        case "session.updated": {
          const title = str(p.title);
          // 标题先写入缓存（该事件可能早于建卡到达），再更新已有卡片
          if (title) titlesRef.current.set(sid, title);
          const card = map.get(sid);
          if (!card) break;
          if (title) card.title = title;
          break;
        }
        case "session.completed": {
          const subPending = Math.max(0, num(p.subagent_pending) ?? 0);
          const card = map.get(sid);
          if (!card) break;
          // 后台子代理仍在跑 → 会话并未真正收尾，保持卡片（与主窗侧栏标记同口径）
          if (subPending > 0) {
            card.subagentCount = subPending;
            card.lastEventAt = now;
            break;
          }
          map.delete(sid);
          const title = card.title;
          recentRef.current = [
            { sessionId: sid, title, at: now },
            ...recentRef.current.filter((r) => r.sessionId !== sid),
          ].slice(0, RECENT_KEEP);
          break;
        }
        default:
          break;
      }

      const touched = map.get(sid);
      if (touched) touched.lastEventAt = now;
      markDirty();
    },
    [markDirty, requestTitles]
  );

  // 订阅全局通道（连接由 port 变化驱动；事件只写入 ref，交由合帧提交）
  useEffect(() => {
    if (!port) return;
    const off = petWs.on(handleEvent);
    petWs.connect(port);
    void correctFromSnapshot();
    return () => {
      off();
      petWs.close();
    };
  }, [port, handleEvent, correctFromSnapshot]);

  // 合帧提交：焦点态 100ms、非焦点 250ms（多会话并发时不逐事件重渲染）
  useEffect(() => {
    const timer = window.setInterval(flush, focused ? 100 : 250);
    return () => window.clearInterval(timer);
  }, [flush, focused]);

  // 快照心跳（60s）
  useEffect(() => {
    const timer = window.setInterval(() => void correctFromSnapshot(), SNAPSHOT_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [correctFromSnapshot]);

  // 静默清扫（30s 检查一次，单卡 15 分钟无事件即摘除）
  useEffect(() => {
    const timer = window.setInterval(() => {
      const now = Date.now();
      let changed = false;
      for (const [sid, card] of [...mapRef.current.entries()]) {
        if (now - card.lastEventAt > CARD_IDLE_TIMEOUT_MS) {
          mapRef.current.delete(sid);
          changed = true;
        }
      }
      if (changed) markDirty();
    }, SWEEP_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [markDirty]);

  /** 按需补齐引擎步骤（仅当该卡没有 AI 清单时；一次性，不轮询） */
  const loadStepsFor = useCallback(async (sessionId: number) => {
    const card = mapRef.current.get(sessionId);
    if (!card || card.stepsSource === "todo") return;
    const rows: TaskRow[] = await fetchTasks(sessionId);
    const steps = rows
      .filter((t) => !t.is_hidden && (t.kind === "step" || (t.parent_task_id != null && t.kind !== "group")))
      .map<TaskStep>((t) => ({ content: t.title, activeForm: t.note || "", status: normalizeStep(t.status) }));
    const latest = mapRef.current.get(sessionId);
    if (!latest) return;
    // 拉取期间若已有 AI 清单到达，则以清单为准，不覆盖
    if (latest.stepsSource === "todo") return;
    latest.steps = steps;
    latest.stepsSource = "engine";
    markDirty();
  }, [markDirty]);

  // 展示态：为运行中的卡片补"当前进行项"（不改 map 原始数据）
  const cards = view.cards.map((c) => ({
    ...c,
    steps: withRunningFallback(c.steps, c.status === "running"),
  }));
  const aggregate = computeAggregate(cards);

  return { cards, aggregate, recent: view.recent, loadStepsFor, connected: petWs.connected };
}
