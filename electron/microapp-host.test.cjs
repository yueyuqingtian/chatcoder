"use strict";
/** 微应用宿主桥单测（node --test electron/microapp-host.test.cjs）。
 *
 *  plan-693-2868 · docs/app1/11-host-bridge-protocol.md：这里只覆盖三件「写错就出事」的事——
 *    ① main.cjs 的 will-attach-webview 只给 app://microapp/ 注入 preload，其余 webview 一律没有；
 *    ② microapp-preload.cjs 在沙箱里只暴露 window.microapp 四项，reqId 配对与超时可用；
 *    ③ browser-agent.cjs 的 uid → guest 映射、上行转发、下行聚批（100 ms / 200 行）。
 *  全程用假 electron（vm / 假模块），不需要桌面端，也不启动 Electron 本体。
 */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const vm = require("node:vm");
const Module = require("node:module");
const { EventEmitter } = require("events");

// agent 侧的长定时器（AI 动作窗口 20 s、上行等待 30 s、withTimeout 15 s）会把进程拖住，
// 这里只把 ≥1 s 的定时器 unref 掉；聚批用的 100 ms 与测试自己的等待保持正常引用。
const realSetTimeout = global.setTimeout;
global.setTimeout = (fn, ms, ...rest) => {
  const t = realSetTimeout(fn, ms, ...rest);
  if (t && typeof t.unref === "function" && Number(ms) >= 1000) t.unref();
  return t;
};

const sleep = (ms) => new Promise((resolve) => realSetTimeout(resolve, ms));
const ROOT = path.join(__dirname, "..");

// ─────────────────────────── ① preload（沙箱内） ───────────────────────────

/** 用假 electron 在独立 realm 里加载 microapp-preload.cjs，模拟 sandbox + contextIsolation。 */
function loadPreload({ location, fastTimeout = false }) {
  const sent = [];
  const listeners = new Map();
  const ipcRenderer = {
    sendToHost: (channel, payload) => { sent.push({ channel, payload }); },
    on: (channel, cb) => {
      if (!listeners.has(channel)) listeners.set(channel, []);
      listeners.get(channel).push(cb);
    },
    removeListener: () => {},
    invoke: () => Promise.reject(new Error("preload 不允许用 ipcRenderer.invoke")),
    send: () => { throw new Error("preload 不允许用 ipcRenderer.send"); },
  };
  let exposed = null;
  const contextBridge = {
    exposeInMainWorld: (name, api) => { exposed = { name, api }; },
  };
  const fakeRequire = (mod) => {
    if (mod === "electron") return { contextBridge, ipcRenderer };
    // 沙箱里的 preload 只能 require("electron")；拿别的模块必须直接失败
    throw new Error("microapp-preload 不允许 require(" + mod + ")");
  };
  const timers = fastTimeout
    ? { setTimeout: (fn) => { fn(); return 0; }, clearTimeout: () => {} }
    : { setTimeout: (fn, ms) => realSetTimeout(fn, ms), clearTimeout: (t) => clearTimeout(t) };

  const context = {
    require: fakeRequire,
    location,
    console,
    URLSearchParams,
    setTimeout: timers.setTimeout,
    clearTimeout: timers.clearTimeout,
  };
  const source = fs.readFileSync(path.join(__dirname, "microapp-preload.cjs"), "utf8");
  new vm.Script(source, { filename: "microapp-preload.cjs" }).runInNewContext(context);

  return {
    sent,
    exposed,
    /** 模拟主进程 → preload：投递 microapp:reply / microapp:event */
    fire: (channel, payload) => {
      for (const cb of (listeners.get(channel) || []).slice()) cb({}, payload);
    },
  };
}

test("preload：只暴露 window.microapp 四项，且 instance 从 location.search 解析", () => {
  const p = loadPreload({
    location: {
      search: "?uid=" + encodeURIComponent("microapp#3") + "&appId=demo-app&version=1.0.0",
      hash: "",
      pathname: "/demo-app/1.0.0/app/index.html",
    },
  });
  assert.equal(p.exposed.name, "microapp", "必须挂在 window.microapp 上");
  assert.deepEqual(
    Object.keys(p.exposed.api).sort(),
    ["instance", "invoke", "on", "version"],
    "只允许契约里的四项，多一个都是越权面",
  );
  assert.equal(p.exposed.api.version, 2);
  assert.equal(typeof p.exposed.api.invoke, "function");
  assert.equal(typeof p.exposed.api.on, "function");
  assert.deepEqual(
    { ...p.exposed.api.instance },
    { appId: "demo-app", version: "1.0.0", uid: "microapp#3" },
  );
  assert.equal(p.exposed.api.ipcRenderer, undefined, "不得暴露 ipcRenderer");
  assert.equal(p.exposed.api.require, undefined, "不得暴露 require");
});

test("preload：query 缺失时从 app://microapp/<appId>/<version>/ 路径兜底，uid 保持空串", () => {
  const p = loadPreload({
    location: { search: "", hash: "", pathname: "/demo-app/1.0.0/app/index.html" },
  });
  assert.deepEqual({ ...p.exposed.api.instance }, { appId: "demo-app", version: "1.0.0", uid: "" });
});

test("preload：uid 未编码导致 '#' 变成 fragment 时能拼回去（避免多标签串台）", () => {
  const p = loadPreload({
    location: { search: "?uid=microapp", hash: "#3", pathname: "/demo-app/1.0.0/app/index.html" },
  });
  assert.equal(p.exposed.api.instance.uid, "microapp#3");
});

test("preload：invoke 走 sendToHost，按 reqId 配对 resolve / reject", async () => {
  const p = loadPreload({
    location: { search: "?uid=u1", hash: "", pathname: "/demo-app/1.0.0/app/index.html" },
  });

  const okPromise = p.exposed.api.invoke("exec.start", { command: "java -version" });
  assert.equal(p.sent.length, 1);
  const msg = p.sent[0];
  assert.equal(msg.channel, "microapp:invoke");
  assert.equal(msg.payload.kind, "invoke");
  assert.equal(msg.payload.op, "exec.start");
  assert.deepEqual({ ...msg.payload.args }, { command: "java -version" });
  assert.equal(msg.payload.uid, "u1", "身份要随请求带上，宿主据此做 uid 寻址");
  assert.match(msg.payload.reqId, /^req-\d+-[a-z0-9]+$/);

  p.fire("microapp:reply", { kind: "reply", reqId: msg.payload.reqId, ok: true, result: { streamId: "s-1" } });
  assert.deepEqual(await okPromise, { streamId: "s-1" });

  const badPromise = p.exposed.api.invoke("fs.read", { path: "a.txt" });
  const badMsg = p.sent[1];
  p.fire("microapp:reply", { kind: "reply", reqId: badMsg.payload.reqId, ok: false, reason: "exec-failed", message: "命令不存在" });
  await assert.rejects(badPromise, (err) => {
    // preload 在 vm 的独立 realm 里跑，它的 Error 不是宿主 realm 的 Error——只校验形状与文案
    assert.equal(err && err.name, "Error");
    assert.match(String(err && err.message), /^microapp-host-exec-failed: 命令不存在$/, "失败原因必须以 microapp-host-<reason> 开头");
    return true;
  });
});

test("preload：op 形状非法直接 reject，不产生上行报文；30 s 超时给出 microapp-host-timeout", async () => {
  const p = loadPreload({
    location: { search: "", hash: "", pathname: "/demo-app/1.0.0/app/index.html" },
  });
  await assert.rejects(p.exposed.api.invoke("bad op; rm -rf", {}), /^Error: microapp-host-op-invalid/);
  assert.equal(p.sent.length, 0);

  const fast = loadPreload({
    fastTimeout: true,
    location: { search: "", hash: "", pathname: "/demo-app/1.0.0/app/index.html" },
  });
  await assert.rejects(fast.exposed.api.invoke("exec.status", {}), /^Error: microapp-host-timeout/);
});

test("preload：on(event, cb) 按事件名分发，返回的 off 能取消订阅", () => {
  const p = loadPreload({
    location: { search: "", hash: "", pathname: "/demo-app/1.0.0/app/index.html" },
  });
  const seen = [];
  const off = p.exposed.api.on("stream.log", (payload) => seen.push(payload));
  p.fire("microapp:event", { kind: "stream", event: "stream.log", seq: 1, lines: ["a"] });
  p.fire("microapp:event", { kind: "stream", event: "stream.state", state: "running" });
  assert.equal(seen.length, 1, "只把匹配事件名的报文交给回调");
  assert.deepEqual(seen[0].lines, ["a"]);

  off();
  p.fire("microapp:event", { kind: "stream", event: "stream.log", seq: 2, lines: ["b"] });
  assert.equal(seen.length, 1, "off 之后不再收到事件");
});

// ───────────────── ② main.cjs 的 will-attach-webview 条件注入 ─────────────────

/** 从 main.cjs 里把真实的 will-attach-webview 回调切出来求值——测的是线上那段代码本身，
 *  不是它的副本（协议 §9-1 的越权判定太关键，复制一份逻辑来测等于什么都没测）。 */
function loadWillAttachHandler(expectedPreloadPath) {
  const source = fs.readFileSync(path.join(__dirname, "main.cjs"), "utf8");
  const marker = 'mainWindow.webContents.on("will-attach-webview"';
  const at = source.indexOf(marker);
  assert.ok(at > 0, "main.cjs 里找不到 will-attach-webview 钩子");

  assert.match(source.slice(at, at + 100), /hardenWebviewAttach/, "主窗口应绑定共用加固函数");
  // plan-20261116-review-fixes：线上钩子已抽为具名函数，仍提取真实实现而不复制规则。
  const functionAt = source.indexOf("function hardenWebviewAttach(");
  const paramsStart = source.indexOf("(_event, webPreferences, params)", functionAt);
  assert.ok(functionAt >= 0 && paramsStart > functionAt, "找不到共用加固函数");
  const braceStart = source.indexOf("{", paramsStart);
  let depth = 0;
  let end = -1;
  for (let i = braceStart; i < source.length; i++) {
    if (source[i] === "{") depth++;
    else if (source[i] === "}") { depth--; if (depth === 0) { end = i; break; } }
  }
  assert.ok(end > braceStart, "回调花括号不配对");
  const fnSource = source.slice(functionAt, end + 1);
  assert.ok(fnSource.includes("app://microapp/"), "提取到的回调用例里没有来源判定，切错了");

  // 顺序断言：先无条件 delete，再按来源注入（否则页面自带的 preload 属性会顶掉我们的）
  const delAt = fnSource.indexOf("delete webPreferences.preload;");
  const injectAt = fnSource.indexOf("webPreferences.preload = MICROAPP_PRELOAD_PATH;");
  assert.ok(delAt >= 0 && injectAt > delAt, "必须先 delete 再条件注入");

  const handler = new Function("MICROAPP_PRELOAD_PATH", "logErr", "return (" + fnSource + ");")(expectedPreloadPath, () => {});
  return { handler, fnSource };
}

const PRELOAD_PATH = path.join(__dirname, "microapp-preload.cjs");

function runAttach(handler, params, preset) {
  const webPreferences = preset ? { ...preset } : {};
  handler({}, webPreferences, params);
  return webPreferences;
}

test("will-attach-webview：app://microapp/ 注入宿主桥 preload，安全项全部保持收紧", () => {
  const { handler } = loadWillAttachHandler(PRELOAD_PATH);
  for (const src of [
    "app://microapp/demo-app/1.0.0/app/index.html",
    "app://microapp/demo-app/1.0.0/app/index.html?uid=" + encodeURIComponent("microapp#3"),
  ]) {
    const wp = runAttach(handler, { src });
    assert.equal(wp.preload, PRELOAD_PATH, "微应用 webview 必须注入宿主桥 preload：" + src);
    assert.equal(wp.sandbox, true);
    assert.equal(wp.contextIsolation, true);
    assert.equal(wp.nodeIntegration, false);
    assert.equal(wp.nodeIntegrationInSubFrames, false);
    assert.equal(wp.backgroundThrottling, false);
  }
});

test("will-attach-webview：非 app://microapp/ 一律不注入（含 includes/大小写陷阱）", () => {
  const { handler } = loadWillAttachHandler(PRELOAD_PATH);
  const cases = [
    ["http://evil.example/app://microapp/x", "includes 陷阱"],
    ["APP://MICROAPP/demo-app/1.0.0/app/index.html", "大小写"],
    ["https://example.com/", "普通网页（浏览器面板）"],
    ["http://localhost:5173/", "本地站点"],
    ["app://vendor/lib/1.0.0/index.html", "vendor 命名空间"],
    ["app://microapp", "缺少尾斜杠"],
    ["app://microappevil/demo-app/1.0.0/a.html", "前缀不完整"],
    ["file:///C:/tmp/a.html", "本地文件"],
    ["about:blank", "空白页"],
    ["", "空串"],
    [undefined, "undefined"],
  ];
  for (const [src, why] of cases) {
    const preset = { preload: "/attacker/preload.js" };
    const wp = runAttach(handler, src === undefined ? {} : { src }, preset);
    assert.equal(wp.preload, undefined, "不该注入 preload（" + why + "：" + src + "）");
    assert.equal(wp.sandbox, true, "sandbox 必须对所有 webview 保持");
    assert.equal(wp.contextIsolation, true);
    assert.equal(wp.nodeIntegration, false);
  }
});

test("will-attach-webview：params 缺失不抛错，且不注入 preload", () => {
  const { handler } = loadWillAttachHandler(PRELOAD_PATH);
  const wp = runAttach(handler, undefined);
  assert.equal(wp.preload, undefined);
  assert.equal(wp.sandbox, true);
});

test("will-attach-webview：微应用 src 也要顶掉页面自带的 preload 属性", () => {
  const { handler } = loadWillAttachHandler(PRELOAD_PATH);
  const wp = runAttach(handler, { src: "app://microapp/demo-app/1.0.0/app/index.html" }, { preload: "/attacker/preload.js" });
  assert.equal(wp.preload, PRELOAD_PATH, "页面/渲染层自带的 preload 一律不生效");
});

// ───────────────── ③ browser-agent：uid 映射 / 上行转发 / 下行聚批 ─────────────────

/** 假 electron：只提供 browser-agent.cjs 用到的部分，其余一律让真实 require 处理。 */
function buildFakeElectron() {
  const allWcs = [];
  const ipcHandlers = new Map();
  const ipcListeners = new Map();
  const microSession = { name: "persist:microapp" };
  const fake = {
    app: {
      getPath: () => os.tmpdir(),
      isPackaged: false,
      on: () => {},
      commandLine: { appendSwitch: () => {} },
    },
    ipcMain: {
      handle: (channel, fn) => ipcHandlers.set(channel, fn),
      on: (channel, fn) => {
        if (!ipcListeners.has(channel)) ipcListeners.set(channel, []);
        ipcListeners.get(channel).push(fn);
      },
      removeHandler: () => {},
      removeAllListeners: () => {},
    },
    webContents: {
      getAllWebContents: () => allWcs.slice(),
      fromId: (id) => allWcs.find((wc) => wc.id === Number(id)) || null,
    },
    session: { fromPartition: () => microSession },
    clipboard: { readText: () => "", writeText: () => {} },
    nativeImage: { createFromBuffer: () => ({ isEmpty: () => true }) },
    shell: { openPath: async () => "" },
  };
  return { fake, allWcs, ipcHandlers, ipcListeners, microSession };
}

const env = buildFakeElectron();
const originalLoad = Module._load;
Module._load = function (request, parent, isMain) {
  if (request === "electron") return env.fake;
  return originalLoad.call(this, request, parent, isMain);
};
const setupBrowserAgent = require("./browser-agent.cjs");

function makeWc({ id, type, url, host, session }) {
  const wc = new EventEmitter();
  wc.id = id;
  wc.type = type;
  wc.url = url;
  wc.hostWebContents = host || null;
  wc.session = session || env.microSession;
  wc.sent = [];
  // 销毁状态可切换：真实 webContents 销毁后 isDestroyed() 为 true，而 host 侧的兜底扫描依赖它
  wc.destroyed = false;
  wc.getType = () => wc.type;
  wc.isDestroyed = () => wc.destroyed;
  wc.getURL = () => wc.url;
  wc.send = (channel, payload) => { wc.sent.push({ channel, payload }); return true; };
  wc.executeJavaScript = async (expr) => (
    String(expr).includes("__MICROAPP_METHODS__ && typeof") ? true : { ok: true, value: { pong: 1 } }
  );
  wc.getBackgroundThrottling = () => true;
  wc.setBackgroundThrottling = () => {};
  wc.once = wc.once.bind(wc);
  env.allWcs.push(wc);
  return wc;
}

const logs = [];
const errors = [];
const mainWc = makeWc({ id: 1, type: "window", url: "file:///app/index.html" });
const mainWindow = { isDestroyed: () => false, webContents: mainWc };
const agent = setupBrowserAgent({
  getMainWindow: () => mainWindow,
  log: (...args) => logs.push(args.join(" ")),
  logErr: (...args) => errors.push(args.join(" ")),
});

const guestUrl = (appId, version, uid) =>
  "app://microapp/" + appId + "/" + version + "/app/index.html?uid=" + encodeURIComponent(uid);

const guestA = makeWc({ id: 11, type: "webview", url: guestUrl("demo-app", "1.0.0", "microapp#3"), host: mainWc });
const guestB = makeWc({ id: 12, type: "webview", url: guestUrl("demo-app", "1.0.0", "microapp#4"), host: mainWc });
const panelGuestWc = makeWc({ id: 13, type: "webview", url: "https://example.com/", host: mainWc, session: { name: "default" } });

test("hookMainWindow：did-attach-webview 记录 uid → guest，非微应用 guest 不登记", () => {
  assert.equal(agent.hookMainWindow(mainWc), true);
  mainWc.emit("did-attach-webview", {}, guestA, { src: guestA.url });
  mainWc.emit("did-attach-webview", {}, guestB, { src: guestB.url });
  mainWc.emit("did-attach-webview", {}, panelGuestWc, { src: panelGuestWc.url });

  const list = agent.microappInstances();
  assert.equal(list.length, 2, "浏览器面板的 http guest 不能被登记为微应用实例");
  assert.deepEqual(list.map((r) => r.uid).sort(), ["microapp#3", "microapp#4"]);
  assert.equal(list[0].appId, "demo-app");
  assert.equal(list[0].version, "1.0.0");
});

test("上行：guest 的 microapp:invoke 转成 microapp:hostRequest（带 uid / wcId）", () => {
  mainWc.sent.length = 0;
  guestA.emit("ipc-message", {}, "microapp:invoke", {
    kind: "invoke", reqId: "req-1-abc", op: "exec.start", args: { command: "java -version" },
    uid: "microapp#3",
  });
  assert.equal(mainWc.sent.length, 1);
  const { channel, payload } = mainWc.sent[0];
  assert.equal(channel, "microapp:hostRequest");
  assert.equal(payload.uid, "microapp#3");
  assert.equal(payload.appId, "demo-app");
  assert.equal(payload.version, "1.0.0");
  assert.equal(payload.wcId, 11);
  assert.equal(payload.reqId, "req-1-abc");
  assert.equal(payload.op, "exec.start");
  assert.deepEqual(payload.args, { command: "java -version" });
});

test("上行：渲染层 hostReply 原样回给发起方 guest；非主窗口 sender 被拒绝", () => {
  const replyHandler = (env.ipcListeners.get("microapp:hostReply") || [])[0];
  assert.equal(typeof replyHandler, "function", "必须注册 microapp:hostReply 频道");

  guestA.sent.length = 0;
  replyHandler({ sender: mainWc }, { uid: "microapp#3", reqId: "req-1-abc", ok: true, result: { streamId: "s-1" } });
  assert.equal(guestA.sent.length, 1);
  assert.equal(guestA.sent[0].channel, "microapp:reply");
  assert.deepEqual(guestA.sent[0].payload, { kind: "reply", reqId: "req-1-abc", ok: true, result: { streamId: "s-1" } });

  // 非主窗口（比如 guest 自己）回包：必须不被采纳
  guestB.sent.length = 0;
  replyHandler({ sender: guestB }, { uid: "microapp#4", reqId: "req-2-abc", ok: true, result: {} });
  assert.equal(guestB.sent.length, 0);
  assert.ok(errors.some((line) => line.includes("只有主窗口渲染层可以回包")));
});

test("下行：microappHostPush 的 stream.state 立即投递到目标 uid", () => {
  const pushHandler = env.ipcHandlers.get("microapp:hostPush");
  assert.equal(typeof pushHandler, "function", "必须注册 microapp:hostPush 频道");

  guestA.sent.length = 0;
  guestB.sent.length = 0;
  const res = pushHandler({}, { uid: "microapp#4", event: "stream.state", payload: { streamId: "s-9", state: "running" } });
  assert.equal(res.ok, true);
  assert.equal(res.result.delivered, true);
  assert.equal(res.result.uid, "microapp#4");
  assert.equal(guestB.sent.length, 1, "非日志事件立即下发");
  assert.equal(guestB.sent[0].channel, "microapp:event");
  assert.equal(guestB.sent[0].payload.event, "stream.state");
  assert.equal(guestB.sent[0].payload.state, "running");
  assert.equal(guestB.sent[0].payload.uid, "microapp#4");
  assert.equal(guestA.sent.length, 0, "不能串到另一个标签");
});

test("下行：stream.log 按 100 ms 聚批，seq 在同 streamId 内递增", async () => {
  const pushHandler = env.ipcHandlers.get("microapp:hostPush");
  guestA.sent.length = 0;

  for (let i = 0; i < 5; i++) {
    pushHandler({}, { uid: "microapp#3", event: "stream.log", payload: { streamId: "s-100", stream: "stdout", lines: ["line " + i] } });
  }
  assert.equal(guestA.sent.length, 0, "100 ms 未到且未满 200 行时不应下发");

  await sleep(160);
  assert.equal(guestA.sent.length, 1, "聚批窗口到点后一次发完");
  const batch = guestA.sent[0].payload;
  assert.equal(batch.event, "stream.log");
  assert.equal(batch.streamId, "s-100");
  assert.equal(batch.seq, 1);
  assert.deepEqual(batch.lines, ["line 0", "line 1", "line 2", "line 3", "line 4"]);

  pushHandler({}, { uid: "microapp#3", event: "stream.log", payload: { streamId: "s-100", stream: "stdout", line: "line 5" } });
  await sleep(160);
  assert.equal(guestA.sent.length, 2);
  assert.equal(guestA.sent[1].payload.seq, 2, "同 streamId 的 seq 必须单调递增");
  assert.deepEqual(guestA.sent[1].payload.lines, ["line 5"]);
});

test("下行：一次推 250 行时满 200 行立即发一批，余下按 100 ms 发", async () => {
  const pushHandler = env.ipcHandlers.get("microapp:hostPush");
  guestA.sent.length = 0;

  const lines = Array.from({ length: 250 }, (_, i) => "L" + i);
  const res = pushHandler({}, { uid: "microapp#3", event: "stream.log", payload: { streamId: "s-200", stream: "stderr", lines } });
  assert.equal(res.result.flushed, 1);
  assert.equal(res.result.buffered, 50);
  assert.equal(guestA.sent.length, 1, "满 200 行必须立刻发，不等 100 ms");
  assert.equal(guestA.sent[0].payload.lines.length, 200);
  assert.equal(guestA.sent[0].payload.seq, 1);

  await sleep(160);
  assert.equal(guestA.sent.length, 2);
  assert.equal(guestA.sent[1].payload.lines.length, 50);
  assert.equal(guestA.sent[1].payload.seq, 2);
});

test("寻址：未知 uid 的推送报 guest-missing；浏览器面板 guest 永远不是目标", () => {
  const pushHandler = env.ipcHandlers.get("microapp:hostPush");
  const res = pushHandler({}, { uid: "microapp#999", event: "stream.state", payload: {} });
  assert.equal(res.ok, false);
  assert.match(res.error, /^microapp-guest-missing/);

  const noTarget = pushHandler({}, { event: "stream.state", payload: {} });
  assert.equal(noTarget.ok, false);
  assert.match(noTarget.error, /^microapp-host-push-invalid/);

  // 按 wcId 找浏览器面板的 guest：它不在微应用映射里，也不能被当成微应用实例
  const byPanel = pushHandler({}, { wcId: panelGuestWc.id, event: "stream.state", payload: {} });
  assert.equal(byPanel.ok, false);
  assert.match(byPanel.error, /^microapp-guest-missing/);
});

test("安全：页面导航到 http 后上行请求被 origin-rejected；will-navigate 被掐断", () => {
  const originalUrl = guestA.url;
  guestA.url = "https://evil.example/";

  guestA.sent.length = 0;
  guestA.emit("ipc-message", {}, "microapp:invoke", { kind: "invoke", reqId: "req-3-x", op: "exec.start", args: {} });
  assert.equal(mainWc.sent.filter((item) => item.payload && item.payload.reqId === "req-3-x").length, 0, "不能转发给渲染层");
  assert.equal(guestA.sent.length, 1);
  assert.equal(guestA.sent[0].payload.ok, false);
  assert.equal(guestA.sent[0].payload.reason, "origin-rejected");

  guestA.url = originalUrl;
  let prevented = false;
  guestA.emit("will-navigate", { preventDefault: () => { prevented = true; } }, "https://evil.example/");
  assert.equal(prevented, true, "微应用不允许导航到非 app://microapp/");
  let prevented2 = false;
  guestA.emit("will-navigate", { preventDefault: () => { prevented2 = true; } }, guestUrl("demo-app", "1.0.0", "microapp#3"));
  assert.equal(prevented2, false, "app://microapp/ 内部导航放行");
});

test("v1 未回归：microappCall / microappProbe 结果里带上命中的 uid", async () => {
  const execHandler = env.ipcHandlers.get("browserAgent:exec");
  const call = await execHandler({}, 0, "microappCall", { appId: "demo-app", version: "1.0.0", method: "ping", uid: "microapp#4" });
  assert.equal(call.ok, true);
  assert.deepEqual(call.result.result, { pong: 1 });
  assert.equal(call.result.uid, "microapp#4", "命中的实例 uid 要回报，便于排查串台");

  const probe = await execHandler({}, 0, "microappProbe", { appId: "demo-app", version: "1.0.0", uid: "microapp#4" });
  assert.equal(probe.ok, true);
  assert.equal(probe.result.ready, true);
  assert.equal(probe.result.uid, "microapp#4");
});

test("v1 未回归：不带 uid 时按 appId+version 仍能命中（取最近活跃，并回报 uid）", async () => {
  const execHandler = env.ipcHandlers.get("browserAgent:exec");
  const call = await execHandler({}, 0, "microappCall", { appId: "demo-app", version: "1.0.0", method: "ping" });
  assert.equal(call.ok, true);
  assert.ok(["microapp#3", "microapp#4"].includes(call.result.uid), "命中任一实例但必须标注 uid");
  assert.equal(call.result.matches, undefined, "microappCall 的返回形状不变（uid 之外不加字段）");
});

test("guest 销毁后清理映射：再推同一个 uid 报 guest-missing", () => {
  const pushHandler = env.ipcHandlers.get("microapp:hostPush");
  assert.equal(pushHandler({}, { uid: "microapp#4", event: "stream.state", payload: {} }).ok, true);

  guestB.destroyed = true;
  guestB.emit("destroyed");
  const after = pushHandler({}, { uid: "microapp#4", event: "stream.state", payload: {} });
  assert.equal(after.ok, false);
  assert.match(after.error, /^microapp-guest-missing/);
  assert.ok(!agent.microappInstances().some((r) => r.uid === "microapp#4"));
});

// ───────── ④ 主窗口 preload 暴露的三条渲染层通道（客户端只从这里取宿主桥） ─────────

test("preload.cjs：暴露 onMicroappHostRequest / microappHostReply / microappHostPush 三条通道", () => {
  const listeners = new Map();
  const calls = [];
  const ipcRenderer = {
    on: (channel, cb) => { if (!listeners.has(channel)) listeners.set(channel, []); listeners.get(channel).push(cb); },
    removeListener: (channel, cb) => {
      const arr = listeners.get(channel) || [];
      const at = arr.indexOf(cb);
      if (at >= 0) arr.splice(at, 1);
    },
    send: (channel, payload) => calls.push({ kind: "send", channel, payload }),
    invoke: (channel, payload) => { calls.push({ kind: "invoke", channel, payload }); return Promise.resolve({ ok: true }); },
    sendSync: () => ({}),
  };
  let exposed = null;
  const contextBridge = { exposeInMainWorld: (name, api) => { exposed = { name, api }; } };
  const context = {
    require: (mod) => {
      if (mod === "electron") return { contextBridge, ipcRenderer, webUtils: { getPathForFile: () => "" } };
      if (mod === "os") return { userInfo: () => ({ username: "tester" }) };
      throw new Error("preload.cjs 意外 require(" + mod + ")");
    },
    process: { platform: "win32" },
    console,
  };
  const source = fs.readFileSync(path.join(__dirname, "preload.cjs"), "utf8");
  new vm.Script(source, { filename: "preload.cjs" }).runInNewContext(context);

  assert.equal(exposed.name, "chatcoderAPI");
  const api = exposed.api;
  assert.equal(typeof api.onMicroappHostRequest, "function");
  assert.equal(typeof api.microappHostReply, "function");
  assert.equal(typeof api.microappHostPush, "function");

  const seen = [];
  const off = api.onMicroappHostRequest((payload) => seen.push(payload));
  assert.equal(listeners.get("microapp:hostRequest").length, 1);
  listeners.get("microapp:hostRequest")[0]({}, { uid: "microapp#3", reqId: "req-1", op: "exec.start" });
  assert.equal(seen.length, 1);
  off();
  assert.equal(listeners.get("microapp:hostRequest").length, 0, "off 必须真的摘掉监听");

  api.microappHostReply({ uid: "microapp#3", reqId: "req-1", ok: true, result: {} });
  api.microappHostPush({ uid: "microapp#3", event: "stream.log", payload: {} });
  assert.deepEqual(calls.map((c) => [c.kind, c.channel]), [
    ["send", "microapp:hostReply"],
    ["invoke", "microapp:hostPush"],
  ]);
});
