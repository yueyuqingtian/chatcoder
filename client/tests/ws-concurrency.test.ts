/**
 * 会话并发隔离测试（Node 原生 TS 运行：`node tests/ws-concurrency.test.ts`）。
 *
 * 复现缺陷：WsClient 为单例，onmessage 经共享 handlers Set 派发。
 * 切换会话时旧 socket 在 close() 前已收到、但尚未派发的消息（事件循环中
 * 排队的 macrotask）会在新 handler 注册后继续派发——旧会话的结束事件
 * （turn.completed / session.completed 等）被当成当前会话处理，
 * 导致"新会话刚发消息、旧会话完成事件串过来把运行态清掉"。
 *
 * 修复后：仅当消息来自当前 socket（this.ws === sock）才派发。
 */
import { WsClient } from "../src/api/ws.ts";

class FakeWebSocket {
  static OPEN = 1;
  static instances: FakeWebSocket[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  readyState = 1;
  sent: string[] = [];
  closed = false;
  url: string;
  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }
  close() {
    this.closed = true;
    this.readyState = 3;
  }
  send(d: string) {
    this.sent.push(d);
  }
}

/** 向指定 socket 模拟注入一条服务端消息（绕过浏览器事件循环，直接驱动 onmessage）。 */
function inject(sock: FakeWebSocket, event: string, payload: Record<string, unknown>, seq?: number) {
  sock.onmessage?.({ data: JSON.stringify({ event, payload, ...(seq != null ? { seq } : {}) }) });
}

async function tick(ms = 20) {
  await new Promise((r) => setTimeout(r, ms));
}

let failures = 0;
function assert(cond: boolean, msg: string) {
  if (!cond) {
    failures++;
    console.error(`FAIL: ${msg}`);
  } else {
    console.log(`ok: ${msg}`);
  }
}

async function main() {
  // —— 环境准备：Node 里没有 window/location，注入 chatcoderAPI 让客户端走桌面直连分支 ——
  (globalThis as Record<string, unknown>).window = {
    chatcoderAPI: { getBackendPort: async () => 8123 },
  };
  (globalThis as Record<string, unknown>).WebSocket = FakeWebSocket;
  FakeWebSocket.instances = [];

  const client = new WsClient();
  const received: Array<{ event: string; payload: Record<string, unknown> }> = [];
  // 与 chat.ts switchSession 相同：先 connect，再注册 handler（disconnect 会清空 handlers）
  const register = () => client.on((ev) => received.push(ev));

  // —— 场景：会话 1 运行中（turn 1 流式），切到新会话 2 ——
  client.connect(1);
  register();
  await tick();
  const ws1 = FakeWebSocket.instances[0];
  assert(!!ws1, "connect(1) 后创建了会话 1 的 socket");
  ws1.onopen?.();

  inject(ws1, "turn.started", { turn_id: 1 });
  assert(received.length === 1 && received[0].event === "turn.started", "会话 1 的事件正常派发");

  // —— 切换到会话 2（新开一个会话发消息）——
  client.connect(2);
  register();
  await tick();
  const ws2 = FakeWebSocket.instances[1];
  assert(!!ws2, "connect(2) 后创建了会话 2 的 socket");
  assert(ws1.closed, "旧 socket 已关闭");
  ws2.onopen?.();

  inject(ws2, "turn.started", { turn_id: 2 });
  assert(received.length === 2 && received[1].event === "turn.started", "会话 2 的事件正常派发");

  // —— 核心断言：旧 socket（会话 1）已入队但未派发的结束事件不得串到会话 2 ——
  // 浏览器在 close() 后仍会派发这条已入队的消息（onmessage 回调已被调度）。
  inject(ws1, "session.completed", { session_id: 1 });
  inject(ws1, "turn.completed", { turn_id: 1, summary: "old session done" });
  assert(received.length === 2, "旧 socket 的迟到结束事件被丢弃（不串到新会话）");

  inject(ws2, "token.delta", { agent_id: 9, delta: "hi" });
  assert(received.length === 3 && received[2].event === "token.delta", "会话 2 的流式事件继续正常派发");

  // —— 同会话重连：旧连接的消息同样不得派发 ——
  const receivedAfter = received.length;
  client.connect(2);
  register();
  await tick();
  const ws2b = FakeWebSocket.instances[2];
  assert(!!ws2b && ws2b !== ws2, "重连创建了新 socket");
  ws2b.onopen?.();
  inject(ws2, "turn.completed", { turn_id: 2 }); // 旧的 ws2 已关闭，迟到消息
  assert(received.length === receivedAfter, "重连后旧连接迟到事件被丢弃");

  client.disconnect();
  console.log(failures === 0 ? "\nALL PASS" : `\n${failures} FAILURE(S)`);
  process.exit(failures === 0 ? 0 : 1);
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
