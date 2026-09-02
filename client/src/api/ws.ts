/** WebSocket 客户端：连接会话通道，接收实时事件。
 *
 * v1.0: 指数退避重连 + 断线补偿 + handler 清理。
 * v2.1: 会话级事件序号跟踪（lastSeq），重连后发 sync.request 补发断线期间事件。
 * v2.2: 只派发当前 socket 的事件——旧连接 close() 前已入队的迟到消息不得串到新会话。
 */

export interface ServerEvent {
  event: string;
  seq?: number;
  payload: Record<string, unknown>;
}

type Handler = (event: ServerEvent) => void;

const _BASE_DELAY = 2000;
const _MAX_DELAY = 30000;
const _MAX_JITTER = 1000;

export class WsClient {
  private ws: WebSocket | null = null;
  private handlers: Set<Handler> = new Set();
  private reconnectTimer: number | null = null;
  private reconnectAttempt = 0;
  private currentSessionId: number | null = null;
  private intentionalClose = false;
  /** 会话级事件序号（断线补偿：重连后请求补发 seq > lastSeq 的事件） */
  private lastSeq = 0;

  connect(sessionId: number) {
    this.disconnect();
    this.currentSessionId = sessionId;
    this.intentionalClose = false;
    this.reconnectAttempt = 0;
    this.lastSeq = 0;
    this._doConnect(sessionId);
  }

  private _doConnect(sessionId: number) {
    // 桌面版直连后端;网页版走同源代理
    // v6.4: 开发模式直连后端，绕过 vite ws 代理
    // v2.1: 打包版端口由主进程透传（getBackendPort），端口冲突自动换空闲端口
    const isElectron = typeof window !== "undefined" && Boolean((window as Window).chatcoderAPI);
    // v2.2: 防御性访问 import.meta.env（非 Vite 环境如 Node 测试/打包变体下 env 不存在）
    const isDev = Boolean((import.meta as { env?: { DEV?: boolean } }).env?.DEV);
    const portPromise = isElectron
      ? (window as Window).chatcoderAPI?.getBackendPort?.() ?? Promise.resolve(8000)
      : Promise.resolve(8000);
    void portPromise
      .then((port) => {
        // v2.2: 端口解析期间若已切换会话，丢弃旧会话的连接（避免打开旧通道）
        if (this.currentSessionId !== sessionId) return;
        if (!Number.isFinite(port) || port <= 0) port = 8000;
        const wsUrl = (isElectron || isDev)
          ? `ws://127.0.0.1:${port}/ws/sessions/${sessionId}`
          : `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/ws/sessions/${sessionId}`;
        this._open(sessionId, wsUrl);
      })
      .catch(() => {
        if (this.currentSessionId !== sessionId) return;
        const wsUrl = `ws://127.0.0.1:8000/ws/sessions/${sessionId}`;
        this._open(sessionId, wsUrl);
      });
  }

  private _open(sessionId: number, wsUrl: string) {
    const sock = new WebSocket(wsUrl);
    this.ws = sock;

    sock.onopen = () => {
      // 只处理当前连接的会话（切换/重连后旧 socket 的 onopen 忽略）
      if (this.ws !== sock || this.currentSessionId !== sessionId) return;
      // v1.0: 重连成功，重置计数
      this.reconnectAttempt = 0;
      // v2.1: 断线补偿——请求补发 lastSeq 之后的事件
      this.send("sync.request", { last_seq: this.lastSeq });
    };

    sock.onmessage = (e) => {
      // v2.2: 只派发当前 socket 的事件——切会话/重连后，旧连接在 close() 前
      // 已收到但尚未派发的消息（事件循环中排队的 macrotask）会被浏览器继续回调，
      // 若放行会把旧会话的 turn.completed/session.completed 等事件串到新会话，
      // 导致"新会话刚发消息，运行态却被旧会话结束事件清掉"。
      if (this.ws !== sock || this.currentSessionId !== sessionId) return;
      try {
        const data = JSON.parse(e.data) as ServerEvent;
        // v2.1: 推进事件序号（sync.response 的 seq=0 不参与推进）
        if (typeof data.seq === "number" && data.seq > this.lastSeq) {
          this.lastSeq = data.seq;
        }
        this.handlers.forEach((h) => h(data));
      } catch {
        // ignore invalid
      }
    };

    sock.onclose = () => {
      // v1.0: 指数退避重连 + 随机抖动
      if (this.intentionalClose) return;
      // v2.2: 旧连接（已被切换/替换）的 close 事件不触发重连
      if (this.ws !== sock || this.currentSessionId !== sessionId) return;
      this.reconnectAttempt++;
      const delay = Math.min(
        _BASE_DELAY * Math.pow(2, this.reconnectAttempt - 1),
        _MAX_DELAY
      ) + Math.random() * _MAX_JITTER;
      this.reconnectTimer = window.setTimeout(() => {
        if (this.currentSessionId === sessionId) {
          this._doConnect(sessionId);
        }
      }, delay);
    };
  }

  disconnect() {
    this.intentionalClose = true;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.ws?.close();
    this.ws = null;
    this.currentSessionId = null;
    this.lastSeq = 0;
    // v1.0: 清理所有 handler，避免累积泄漏
    this.handlers.clear();
  }

  send(event: string, payload: Record<string, unknown>) {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ event, payload }));
    }
  }

  on(handler: Handler) {
    this.handlers.delete(handler);  // v4.5: 先删除确保不重复注册
    this.handlers.add(handler);
    return () => this.handlers.delete(handler);
  }

  get connected() {
    return this.ws?.readyState === WebSocket.OPEN;
  }
}

export const wsClient = new WsClient();

/**
 * 全局状态客户端（v37）：连接 /ws/global，接收跨会话状态事件。
 *
 * 会话级 WsClient 只为「当前聚焦会话」派发事件（切会话即 disconnect 重建），
 * 因此后台会话的 session.completed / turn.completed 前端收不到——
 * 侧栏 has_running 一直停在 sendTurn 乐观置的 true，只能靠整表刷新修正。
 *
 * 本类与会话级客户端完全解耦：不共享 lastSeq、不共享 handlers、不影响
 * ws-concurrency 测试断言的会话级隔离语义。只做指数退避重连与 handler 派发。
 */
export class GlobalWsClient {
  private ws: WebSocket | null = null;
  private handlers: Set<Handler> = new Set();
  private reconnectTimer: number | null = null;
  private reconnectAttempt = 0;
  private intentionalClose = false;

  connect() {
    if (this.ws) return;
    this.intentionalClose = false;
    this.reconnectAttempt = 0;
    this._doConnect();
  }

  private _doConnect() {
    const isElectron = typeof window !== "undefined" && Boolean((window as Window).chatcoderAPI);
    const isDev = Boolean((import.meta as { env?: { DEV?: boolean } }).env?.DEV);
    const portPromise = isElectron
      ? (window as Window).chatcoderAPI?.getBackendPort?.() ?? Promise.resolve(8000)
      : Promise.resolve(8000);
    void portPromise
      .then((port) => {
        if (this.ws) return;
        if (!Number.isFinite(port) || port <= 0) port = 8000;
        const wsUrl = (isElectron || isDev)
          ? `ws://127.0.0.1:${port}/ws/global`
          : `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/ws/global`;
        this._open(wsUrl);
      })
      .catch(() => {
        if (this.ws) return;
        this._open(`ws://127.0.0.1:8000/ws/global`);
      });
  }

  private _open(wsUrl: string) {
    const sock = new WebSocket(wsUrl);
    this.ws = sock;

    sock.onopen = () => {
      if (this.ws !== sock) return;
      this.reconnectAttempt = 0;
    };

    sock.onmessage = (e) => {
      if (this.ws !== sock) return;
      try {
        const data = JSON.parse(e.data) as ServerEvent;
        this.handlers.forEach((h) => h(data));
      } catch {
        // ignore invalid
      }
    };

    sock.onclose = () => {
      if (this.intentionalClose) return;
      if (this.ws !== sock) return;
      this.ws = null;
      this.reconnectAttempt++;
      const delay = Math.min(
        _BASE_DELAY * Math.pow(2, this.reconnectAttempt - 1),
        _MAX_DELAY,
      ) + Math.random() * _MAX_JITTER;
      this.reconnectTimer = window.setTimeout(() => {
        if (!this.intentionalClose) this._doConnect();
      }, delay);
    };
  }

  disconnect() {
    this.intentionalClose = true;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.ws?.close();
    this.ws = null;
    this.handlers.clear();
  }

  on(handler: Handler) {
    this.handlers.add(handler);
    return () => this.handlers.delete(handler);
  }
}

export const globalWsClient = new GlobalWsClient();
