/** 宠物窗口数据层（plan-73-323 / plan-73-326 整改）。
 *
 * 三条通路：
 *  ① `/ws/global`（渲染层直连）：多任务增量事件 —— 概览主通道，事件驱动、低频；
 *  ② `/ws/sessions/{id}`（渲染层直连，**仅主任务一条**）：实时消息流 ——
 *     思考块 / 普通消息 / 工具调用都来自流式增量（`thinking.delta` / `token.delta`），
 *     而全局通道按设计不转发高频增量，因此这里为「当前最要紧的任务」单独开一条会话连接；
 *     始终只有 1 条，切换主任务时切换目标，不为每个会话各开一条；
 *  ③ `/api/sessions` 与 `/turns/{id}/tasks`（主进程代取）：启动基线、60s 校正、
 *     选中无清单任务时补齐引擎步骤。
 *
 * 为何 REST 不直连：宠物页以 `file://` 加载，向 `http://127.0.0.1` 发 fetch 属跨源
 * （Origin: null），会被 CORS 拦截；WebSocket 无该限制，故 WS 直连、REST 经主进程代理。
 */
import { petApi } from "./petApi";
import type { SessionSnapshot, TaskRow } from "./petApi";

export interface PetServerEvent {
  event: string;
  seq?: number;
  payload: Record<string, unknown>;
}

/** 连接建立事件（非服务端事件）：上层据此触发一次快照校正 */
export const CONNECTED_EVENT = "__pet_connected";

const BASE_DELAY = 1500;
const MAX_DELAY = 20000;

/** 全局通道客户端：仅重连与派发，无补偿缓冲（快照校正兜底） */
export class PetGlobalWs {
  private ws: WebSocket | null = null;
  private handlers = new Set<(e: PetServerEvent) => void>();
  private timer: number | null = null;
  private attempt = 0;
  private closed = true;
  private port = 12973;

  connect(port: number) {
    this.port = Number.isFinite(port) && port > 0 ? port : 12973;
    this.closed = false;
    this.attempt = 0;
    this.open();
  }

  private open() {
    if (this.closed) return;
    try {
      this.ws = new WebSocket(`ws://127.0.0.1:${this.port}/ws/global`);
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.ws.onopen = () => {
      this.attempt = 0;
      this.emit({ event: CONNECTED_EVENT, payload: {} });
    };
    this.ws.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data as string) as PetServerEvent;
        if (data && typeof data.event === "string") this.emit(data);
      } catch {
        // 非法帧忽略即可，不影响后续事件
      }
    };
    this.ws.onclose = () => {
      this.ws = null;
      this.scheduleReconnect();
    };
    // onerror 后必然跟 onclose，重连逻辑统一放 onclose，避免重复调度
    this.ws.onerror = () => {};
  }

  private scheduleReconnect() {
    if (this.closed) return;
    this.attempt++;
    const delay = Math.min(BASE_DELAY * 2 ** (this.attempt - 1), MAX_DELAY) + Math.random() * 500;
    this.timer = window.setTimeout(() => this.open(), delay);
  }

  private emit(ev: PetServerEvent) {
    this.handlers.forEach((h) => h(ev));
  }

  on(handler: (e: PetServerEvent) => void) {
    this.handlers.add(handler);
    return () => this.handlers.delete(handler);
  }

  close() {
    this.closed = true;
    if (this.timer != null) {
      window.clearTimeout(this.timer);
      this.timer = null;
    }
    this.ws?.close();
    this.ws = null;
  }

  get connected() {
    return this.ws?.readyState === WebSocket.OPEN;
  }
}

export const petWs = new PetGlobalWs();

/** 会话级连接重连成功的事件名（非服务端事件）：上层据此拉一次快照补齐状态。
 *  为什么需要：`token.delta` / `thinking.delta` 是**不入服务端缓冲、不占序号**的高频增量，
 *  断连期间丢失的内容无法重放；但"当前状态"（哪些会话在跑、步骤到哪一步）可以经 REST 补齐。 */
export const RECONNECTED_EVENT = "__pet_session_reconnected";

/**
 * 会话级连接（实时消息流）：只服务「主任务」的浮窗实时行。
 *
 * **连接常驻**（plan-73-341）：是否连接只取决于「宠物窗口可见 + 存在主任务」，
 * **不再依赖 hover/expanded** —— 此前挂在 hover 上，鼠标一动就断连，
 * 而流式增量断连即永久丢失，表现为"浮窗消息卡住不动"。
 *
 * 竞态修复：`close()` 是异步派发的，旧连接的 `onclose` 会在新连接建立后触发，
 * 从而额外排一个重连定时器（双连接 / 僵尸连接）。现用两道保险：
 *  ① 断开时先摘掉全部回调再 close；
 *  ② generation 令牌：所有回调先比对令牌，不匹配即忽略。
 */
export class PetSessionWs {
  private ws: WebSocket | null = null;
  private handlers = new Set<(e: PetServerEvent) => void>();
  private timer: number | null = null;
  private attempt = 0;
  private closed = true;
  private port = 12973;
  private sessionId = 0;
  /** 代次令牌：每次 watch/断开递增，用于失效在途回调 */
  private gen = 0;
  /** 是否曾经成功连接过（用于区分首次连接与重连，重连才通知上层补状态） */
  private everOpen = false;

  /** 切换监听的会话；同一会话 + 同一端口 + 连接健在时是空操作（避免频繁重建） */
  watch(sessionId: number, port: number) {
    const nextPort = Number.isFinite(port) && port > 0 ? port : 12973;
    if (this.sessionId === sessionId && this.port === nextPort && this.ws) return;
    this.teardown();
    this.sessionId = sessionId;
    this.port = nextPort;
    this.closed = false;
    this.attempt = 0;
    this.everOpen = false;
    this.gen++;
    this.open(this.gen);
  }

  /** 摘掉当前 socket（先清回调再 close，避免旧连接回调污染新连接） */
  private teardown() {
    if (this.timer != null) {
      window.clearTimeout(this.timer);
      this.timer = null;
    }
    const sock = this.ws;
    this.ws = null;
    if (!sock) return;
    sock.onopen = null;
    sock.onmessage = null;
    sock.onclose = null;
    sock.onerror = null;
    try { sock.close(); } catch { /* 忽略 */ }
  }

  private open(gen: number) {
    if (this.closed || !this.sessionId || gen !== this.gen) return;
    let sock: WebSocket;
    try {
      sock = new WebSocket(`ws://127.0.0.1:${this.port}/ws/sessions/${this.sessionId}`);
    } catch {
      this.scheduleReconnect(gen);
      return;
    }
    this.ws = sock;

    sock.onopen = () => {
      if (gen !== this.gen || this.ws !== sock) return;
      this.attempt = 0;
      if (this.everOpen) {
        // 重连成功：通知上层补状态（增量无法重放，状态可以）
        this.emit({ event: RECONNECTED_EVENT, payload: {} });
      }
      this.everOpen = true;
    };
    sock.onmessage = (e) => {
      if (gen !== this.gen || this.ws !== sock) return;
      try {
        const data = JSON.parse(e.data as string) as PetServerEvent;
        if (data && typeof data.event === "string") this.emit(data);
      } catch {
        // 忽略非法帧
      }
    };
    sock.onclose = () => {
      if (gen !== this.gen || this.ws !== sock) return;
      this.ws = null;
      this.scheduleReconnect(gen);
    };
    sock.onerror = () => {};
  }

  private scheduleReconnect(gen: number) {
    if (this.closed || gen !== this.gen) return;
    this.attempt++;
    const delay = Math.min(BASE_DELAY * 2 ** (this.attempt - 1), MAX_DELAY) + Math.random() * 500;
    this.timer = window.setTimeout(() => this.open(gen), delay);
  }

  private emit(ev: PetServerEvent) {
    this.handlers.forEach((h) => h(ev));
  }

  on(handler: (e: PetServerEvent) => void) {
    this.handlers.add(handler);
    return () => this.handlers.delete(handler);
  }

  disconnect() {
    this.closed = true;
    this.gen++; // 让所有在途回调失效
    this.teardown();
    this.sessionId = 0;
    this.everOpen = false;
  }
}

export const petSessionWs = new PetSessionWs();

/** 会话快照（主进程代取）；失败返回空数组，由上层保留现有卡片 */
export async function fetchSessions(): Promise<SessionSnapshot[]> {
  const api = petApi();
  if (!api) return [];
  try {
    const list = await api.snapshot();
    return Array.isArray(list) ? list : [];
  } catch {
    return [];
  }
}

/** 引擎步骤（仅选中无清单的任务时按需拉取一次，不轮询） */
export async function fetchTasks(sessionId: number): Promise<TaskRow[]> {
  const api = petApi();
  if (!api) return [];
  try {
    const list = await api.tasks(sessionId);
    return Array.isArray(list) ? list : [];
  } catch {
    return [];
  }
}
