/** WebSocket 客户端：连接会话通道，接收实时事件。
 *
 * v1.0: 指数退避重连 + 断线补偿 + handler 清理。
 */

export interface ServerEvent {
  event: string;
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

  connect(sessionId: number) {
    this.disconnect();
    this.currentSessionId = sessionId;
    this.intentionalClose = false;
    this.reconnectAttempt = 0;
    this._doConnect(sessionId);
  }

  private _doConnect(sessionId: number) {
    // 桌面版直连后端;网页版走同源代理
    const isElectron = typeof window !== "undefined" && Boolean((window as Window).chatcoderAPI);
    const wsUrl = isElectron
      ? `ws://127.0.0.1:8000/ws/sessions/${sessionId}`
      : `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/ws/sessions/${sessionId}`;
    this.ws = new WebSocket(wsUrl);

    this.ws.onopen = () => {
      // v1.0: 重连成功，重置计数
      this.reconnectAttempt = 0;
    };

    this.ws.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data) as ServerEvent;
        this.handlers.forEach((h) => h(data));
      } catch {
        // ignore invalid
      }
    };

    this.ws.onclose = () => {
      // v1.0: 指数退避重连 + 随机抖动
      if (this.intentionalClose) return;
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
