/**
 * 微应用宿主桥 · 渲染层中枢回归测试（plan-693-2868 · docs/app1/11-host-bridge-protocol.md §4-§7）。
 *
 * 复现的三个真实风险（都是「错了但看起来没事」的那种）：
 *   ① 每个 MicroAppPanel 各自订阅 IPC → 一条上行请求被转发 N 次，页面命令被执行 N 遍；
 *   ② sessionId 取「当前活动会话」→ 后台会话里的微应用把命令跑进用户正在看的项目；
 *   ③ 全局 WS 断开时静默丢弃 → 页面干等 30 秒宿主超时，排查时看不出是宿主没回。
 *
 * 纯逻辑测试：用假 ws / 假 IPC 驱动 `createMicroappHostBridge`，不碰 DOM、不连真后端。
 * 运行方式（client 目录）：`npm test -- microapp-host`（脚本按文件名过滤）。
 */
import assert from "node:assert/strict";
import {
  createMicroappHostBridge,
  ERR_OFFLINE,
  ERR_UNKNOWN_INSTANCE,
  EV_HOST_PUSH,
  EV_HOST_REQUEST,
  EV_HOST_RESPONSE,
  type MicroappHostIpc,
  type MicroappHostWs,
} from "../src/browserAgent/microappHost.ts";

type WsEvent = { event: string; payload: Record<string, unknown> };

/** 假全局 WS：记录订阅者与发出的每一条事件；connected=false 时 send 返回 false（模拟断线）。 */
function fakeWs() {
  const handlers = new Set<(ev: WsEvent) => void>();
  const sent: Array<{ event: string; payload: Record<string, unknown> }> = [];
  let subscribed = 0;
  const ws: MicroappHostWs = {
    on(cb) {
      subscribed += 1;
      handlers.add(cb);
      return () => {
        subscribed -= 1;
        handlers.delete(cb);
      };
    },
    send(event, payload) {
      if (!state.connected) return false;
      sent.push({ event, payload });
      return true;
    },
  };
  const state = { connected: true };
  return {
    ws,
    sent,
    state,
    /** 当前仍然挂着的订阅者数（验证「重挂载不叠加监听」） */
    get subscribers() { return subscribed; },
    emit(event: string, payload: Record<string, unknown>) {
      for (const h of Array.from(handlers)) h({ event, payload });
    },
  };
}

/** 假主进程：记录回执、下行推送与注册的上行监听。 */
function fakeIpc() {
  const replies: Array<Record<string, unknown>> = [];
  const pushes: Array<Record<string, unknown>> = [];
  let listener: ((payload: Record<string, unknown>) => void) | null = null;
  const handlers: MicroappHostIpc = {
    onMicroappHostRequest(cb) {
      listener = cb;
      return () => { if (listener === cb) listener = null; };
    },
    microappHostReply(payload) {
      replies.push(payload);
    },
    microappHostPush(payload) {
      pushes.push(payload);
      return Promise.resolve({ ok: true });
    },
  };
  return {
    ipc: () => handlers,
    replies,
    pushes,
    get listening() { return listener !== null; },
    /** 模拟主进程经 `microapp:hostRequest` 抛来一条上行请求 */
    fromMain(payload: Record<string, unknown>) {
      assert.ok(listener, "主进程上行监听未注册");
      listener!(payload);
    },
  };
}

function setup() {
  const ws = fakeWs();
  const ipc = fakeIpc();
  const bridge = createMicroappHostBridge({ ws: ws.ws, ipc: ipc.ipc });
  return { ws, ipc, bridge };
}

const tests: Array<[string, () => void | Promise<void>]> = [];
const test = (name: string, fn: () => void | Promise<void>) => tests.push([name, fn]);

// ① 可重入：面板反复挂载也只挂一份监听，计数归零才摘
test("start 幂等：重复启动不叠加监听，计数归零才摘", () => {
  const { ws, ipc, bridge } = setup();
  const stop1 = bridge.start();
  const stop2 = bridge.start();
  assert.equal(ws.subscribers, 1, "WS 只应有一个订阅");
  assert.equal(ipc.listening, true, "主进程上行监听应已注册");

  stop1();
  assert.equal(ws.subscribers, 1, "还有人持有时不摘监听");
  assert.equal(ipc.listening, true);

  stop2();
  stop2(); // 重复调用只生效一次
  assert.equal(ws.subscribers, 0, "计数归零后摘掉 WS 订阅");
  assert.equal(ipc.listening, false, "计数归零后摘掉主进程监听");
});

// ② 上行：uid → 标签所属会话桶（不是当前活动会话）
test("上行转发用标签所属会话桶的 sessionId", () => {
  const { ws, ipc, bridge } = setup();
  bridge.start();
  const unbind = bridge.bind({ uid: "microapp#3", sessionId: 42, appId: "java-devtools", version: "1.0.0" });

  ipc.fromMain({ uid: "microapp#3", reqId: "req-1-a", op: "exec.start", args: { command: "java -version" }, wcId: 12 });

  assert.equal(ws.sent.length, 1, "应转发恰好一条上行请求");
  const { event, payload } = ws.sent[0];
  assert.equal(event, EV_HOST_REQUEST, "事件名必须是 microapp.host.request");
  assert.equal(payload.sessionId, 42, "sessionId 必须取该标签所属会话桶");
  assert.equal(payload.uid, "microapp#3");
  assert.equal(payload.op, "exec.start");
  assert.equal(payload.reqId, "req-1-a");
  assert.deepEqual(payload.args, { command: "java -version" });
  assert.equal(payload.appId, "java-devtools", "appId 缺省时回落到绑定身份");
  assert.equal(payload.version, "1.0.0");
  assert.equal(ipc.replies.length, 0, "正常情况下不回执失败");
  unbind();
});

// ③ 未登记的 uid：立刻回绝，不静默丢弃
test("未知 uid 立即回执 unknown-instance", () => {
  const { ws, ipc, bridge } = setup();
  bridge.start();
  ipc.fromMain({ uid: "microapp#99", reqId: "req-2", op: "fs.list", args: {} });

  assert.equal(ws.sent.length, 0, "不得转发到后端");
  assert.equal(ipc.replies.length, 1);
  const r = ipc.replies[0];
  assert.equal(r.reqId, "req-2");
  assert.equal(r.ok, false);
  assert.equal(r.reason, ERR_UNKNOWN_INSTANCE);
  assert.ok(String(r.message).startsWith(ERR_UNKNOWN_INSTANCE), "message 也要带 reason 前缀");
});

// ④ 首页面板（无会话桶）：不发出去，给出可读原因
test("无会话桶的界面拒绝上行", () => {
  const { ws, ipc, bridge } = setup();
  bridge.start();
  bridge.bind({ uid: "microapp#1", sessionId: null });
  ipc.fromMain({ uid: "microapp#1", reqId: "req-3", op: "workspace.info", args: {} });

  assert.equal(ws.sent.length, 0);
  assert.equal(ipc.replies.length, 1);
  assert.equal(ipc.replies[0].reason, ERR_UNKNOWN_INSTANCE);
  assert.match(String(ipc.replies[0].message), /首页/);
});

// ⑤ WS 断开：明确 microapp-host-offline，而不是让页面等 30 秒宿主超时
test("WS 未连接时回执 microapp-host-offline", () => {
  const { ws, ipc, bridge } = setup();
  bridge.start();
  bridge.bind({ uid: "microapp#4", sessionId: 7 });
  ws.state.connected = false;

  ipc.fromMain({ uid: "microapp#4", reqId: "req-4", op: "exec.start", args: { command: "ls" } });

  assert.equal(ws.sent.length, 0, "断线时不应假装发出去了");
  assert.equal(ipc.replies.length, 1);
  assert.equal(ipc.replies[0].reqId, "req-4");
  assert.equal(ipc.replies[0].ok, false);
  assert.equal(ipc.replies[0].reason, ERR_OFFLINE);
});

// ⑥ 下行回执：成功原样回、失败去掉重复的 code 前缀
test("下行回执回给主进程（成功 / 失败两种形态）", () => {
  const { ws, ipc, bridge } = setup();
  bridge.start();

  ws.emit(EV_HOST_RESPONSE, { reqId: "req-5", uid: "microapp#3", ok: true, result: { streamId: "bg_123" } });
  assert.deepEqual(ipc.replies[0], { reqId: "req-5", uid: "microapp#3", ok: true, result: { streamId: "bg_123" } });

  ws.emit(EV_HOST_RESPONSE, {
    reqId: "req-6",
    uid: "microapp#3",
    ok: false,
    reason: "microapp-host-fs-outside-workspace",
    message: "microapp-host-fs-outside-workspace: 路径不在会话工作区内",
  });
  const fail = ipc.replies[1];
  assert.equal(fail.ok, false);
  assert.equal(fail.reason, "microapp-host-fs-outside-workspace");
  assert.equal(fail.message, "路径不在会话工作区内", "重复的 code 前缀应被去掉（preload 会再拼一次）");
});

// ⑦ 下行推送：内层 event 提到外层，其余字段原样交给主进程聚批
test("下行 push 转成主进程认识的形状", () => {
  const { ws, ipc, bridge } = setup();
  bridge.start();

  ws.emit(EV_HOST_PUSH, {
    uid: "microapp#3", streamId: "bg_123", seq: 12, event: "stream.log", lines: ["a", "b"], stream: "stdout",
  });
  assert.equal(ipc.pushes.length, 1);
  const push = ipc.pushes[0];
  assert.equal(push.uid, "microapp#3");
  assert.equal(push.event, "stream.log");
  assert.deepEqual(push.payload, { streamId: "bg_123", seq: 12, lines: ["a", "b"], stream: "stdout" });

  ws.emit(EV_HOST_PUSH, { uid: "microapp#3", streamId: "bg_123", seq: 13, event: "stream.state", state: "exited", code: 0 });
  assert.equal(ipc.pushes[1].event, "stream.state");
  assert.deepEqual(ipc.pushes[1].payload, { streamId: "bg_123", seq: 13, state: "exited", code: 0 });
});

// ⑧ 绑定同一性：先绑定的解绑不摘掉后来的绑定（React 严格模式 挂载→清理→挂载）
test("解绑只摘自己那一次", () => {
  const { ws, ipc, bridge } = setup();
  bridge.start();
  const unbind1 = bridge.bind({ uid: "microapp#8", sessionId: 1 });
  const unbind2 = bridge.bind({ uid: "microapp#8", sessionId: 2 });
  unbind1(); // 第一次的清理（严格模式下的多余调用）

  ipc.fromMain({ uid: "microapp#8", reqId: "req-8", op: "workspace.info", args: {} });
  assert.equal(ws.sent.length, 1, "后一次绑定应仍然有效");
  assert.equal(ws.sent[0].payload.sessionId, 2);

  unbind2();
  ipc.fromMain({ uid: "microapp#8", reqId: "req-9", op: "workspace.info", args: {} });
  assert.equal(ipc.replies.length, 1, "全部解绑后应拒绝");
  assert.equal(ipc.replies[0].reason, ERR_UNKNOWN_INSTANCE);
});

test("窗口实例按会话定位，返回值不能修改桥内身份", () => {
  const ws = fakeWs();
  const ipc = fakeIpc();
  const bridge = createMicroappHostBridge({ ws: ws.ws, ipc: ipc.ipc });
  const offA = bridge.bind({ uid: "window-a", sessionId: 1, appId: "demo-app", version: "1.0.0" });
  bridge.bind({ uid: "window-b", sessionId: 2, appId: "demo-app", version: "1.0.0" });
  assert.equal(bridge.instanceFor(1, "demo-app", "1.0.0")?.uid, "window-a");
  assert.equal(bridge.instanceFor(3, "demo-app", "1.0.0"), undefined);
  const copy = bridge.instanceFor(1, "demo-app", "1.0.0")!;
  copy.sessionId = 99;
  assert.equal(bridge.instanceFor(1, "demo-app", "1.0.0")?.sessionId, 1);
  offA();
  assert.equal(bridge.instanceFor(1, "demo-app", "1.0.0"), undefined);
});

let failed = 0;
for (const [name, fn] of tests) {
  try {
    await fn();
    console.log(`  ✓ ${name}`);
  } catch (err) {
    failed += 1;
    console.error(`  ✗ ${name}`);
    console.error(err instanceof Error ? err.stack || err.message : String(err));
  }
}
console.log(failed ? `\n失败 ${failed}/${tests.length}` : `\n全部通过：${tests.length} 项`);
process.exit(failed ? 1 : 0);
