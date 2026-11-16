// 微应用宿主桥 preload（plan-693-2868 · docs/app1/11-host-bridge-protocol.md §3-§5）
//
// 只干一件事：把「页面 ↔ 宿主」的四个能力暴露成 window.microapp，别的一概不给。
//   version   桥协议版本（2），页面据此降级；
//   invoke    Promise 化的宿主操作：上行 sendToHost("microapp:invoke")，回包走 "microapp:reply"；
//   on        订阅下行流式事件（主进程 "microapp:event"），返回取消订阅函数；
//   instance  当前实例身份（appId / version / uid），寻址用（协议 §6）。
//
// 为什么必须用 contextBridge（与 electron/preload.cjs 同款写法）：
//   微应用 webview 在 main.cjs 的 will-attach-webview 里被统一收敛为
//   sandbox: true + contextIsolation: true + nodeIntegration: false。
//   preload 因此跑在独立的隔离世界里，**直接写 window 页面看不到**；只有 contextBridge 暴露的
//   东西才会被拷贝到页面世界。反过来说：任何时候都不要为了「方便」关掉 contextIsolation 或
//   把 ipcRenderer 暴露出去——那等于把宿主 IPC 通道交给无审核的第三方页面（协议 §2 / §3）。
//
// 沙箱兼容：本文件只 require("electron")（sandboxed preload 允许的唯一运行时模块），
//   不碰 fs / path / child_process / process / os，也不使用 Node 的 require 能力。
"use strict";

const { contextBridge, ipcRenderer } = require("electron");

// ── 与主进程约定的频道与常量（改名字必须与 electron/browser-agent.cjs 同步）──
const BRIDGE_VERSION = 2;
const CH_INVOKE = "microapp:invoke"; // preload → 主进程（ipcRenderer.sendToHost）
const CH_REPLY = "microapp:reply";   // 主进程 → preload（上行结果）
const CH_EVENT = "microapp:event";   // 主进程 → preload（下行流式事件）

// 页面等回包的上限。与主进程的 MICROAPP_HOST_TIMEOUT_MS 对齐（那边同样 30 s 回收并回一条
// host-timeout），两边都兜底：页面先超时也只是不再等，主进程那边的 pending 同样会被清理。
const REPLY_TIMEOUT_MS = 30000;

// op 名的形状：exec.start / fs.read / workspace.info
// 只做格式校验（不校验「有没有这个 op」，那是主进程/后端的职责），挡掉拼接式乱传。
const OP_RE = /^[A-Za-z][A-Za-z0-9]*(?:\.[A-Za-z][A-Za-z0-9]*)*$/;

// ── 失败原因前缀（协议 §3：message 必须以 microapp-host-<reason> 开头）──
function hostError(reason, message) {
  const raw = String(reason || "failed");
  const prefix = raw.startsWith("microapp-host-") ? raw : "microapp-host-" + raw;
  const err = new Error(message ? prefix + ": " + message : prefix);
  err.reason = raw; // 页面可据此分支，不必解析字符串
  return err;
}

// ── 实例身份：优先读 query，其次从 app://microapp/<appId>/<version>/… 的路径兜底 ──
// 协议 §6：页面身份由渲染层在创建 webview 时通过 query 传入（preload 拿不到 document.currentScript），
// 因此这里读 location.search；读不到就给空串（不猜）。
function readInstance() {
  const out = { appId: "", version: "", uid: "" };
  let search = "";
  let hash = "";
  let pathname = "";
  try {
    search = String(location.search || "");
    hash = String(location.hash || "");
    pathname = String(location.pathname || "");
  } catch {
    return out; // 非浏览器上下文（理论上不会发生）：保持空身份
  }

  let params = null;
  try { params = new URLSearchParams(search); } catch { params = null; }
  const take = (key) => {
    if (!params) return "";
    try {
      const v = params.get(key);
      return v === null || v === undefined ? "" : String(v);
    } catch { return ""; }
  };

  out.uid = take("uid");
  out.appId = take("appId") || take("appid");
  out.version = take("version") || take("v");

  // 客户端 uid 形如 "microapp#3"（client/src/store/panel.ts 的 nextUid）：若渲染层忘了
  // encodeURIComponent，"#" 会被当成 fragment 起点，uid 被截断成 "microapp"，
  // 于是同一个 appId 的多个标签在宿主侧全部撞成同一个 uid——协议 §6 要修的正是这个串台。
  // 这里把「裸的 #数字」补回去（与 browser-agent.cjs 的 parseMicroappSrc 同一条规则）。
  if (out.uid && out.uid.indexOf("#") === -1 && /^#\d{1,6}$/.test(hash)) out.uid += hash;

  // 路径兜底：query 没带身份时（老客户端 / 直接打开 app:// 地址）从 URL 路径取，
  // 路径形状由 app:// 协议保证（main.cjs:265：app://microapp/<appId>/<version>/<path...>）。
  if (!out.appId || !out.version) {
    const seg = pathname.split("/").filter(Boolean);
    if (!out.appId && seg.length >= 1 && /^[a-z0-9][a-z0-9._-]{1,62}$/i.test(seg[0])) out.appId = seg[0];
    if (!out.version && seg.length >= 2 && /^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$/.test(seg[1])) out.version = seg[1];
  }
  return out;
}

const instance = Object.freeze(readInstance());

// ── 上行：reqId 配对 + 30 s 超时清理 ──
const pending = new Map(); // reqId → { resolve, reject, timer }
let reqSeq = 0;

function nextReqId() {
  reqSeq += 1;
  return "req-" + reqSeq + "-" + Math.random().toString(36).slice(2, 10);
}

// 主进程回包（协议 §4）：{ kind:"reply", reqId, ok:true, result } | { kind:"reply", reqId, ok:false, reason, message }
ipcRenderer.on(CH_REPLY, (_event, msg) => {
  if (!msg || typeof msg !== "object") return;
  if (msg.kind && msg.kind !== "reply") return; // 容错：只认 reply（将来若扩展别的报文，这里不会误消费）
  const reqId = String(msg.reqId || "");
  const entry = reqId ? pending.get(reqId) : null;
  if (!entry) return; // 已超时 / 不是本页发的：直接丢
  pending.delete(reqId);
  clearTimeout(entry.timer);
  if (msg.ok) entry.resolve(msg.result === undefined ? null : msg.result);
  else entry.reject(hostError(msg.reason, msg.message));
});

/** 发起一次宿主操作，等结果（协议 §3 / §4）。 */
function invoke(op, args) {
  if (typeof op !== "string" || !OP_RE.test(op)) {
    return Promise.reject(hostError("op-invalid", "op 必须形如 exec.start 的操作名"));
  }
  const reqId = nextReqId();
  const callArgs = args === undefined || args === null ? {} : args;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      pending.delete(reqId);
      reject(hostError("timeout", "宿主 " + Math.round(REPLY_TIMEOUT_MS / 1000) + " 秒内没有返回结果"));
    }, REPLY_TIMEOUT_MS);
    pending.set(reqId, { resolve, reject, timer });
    try {
      ipcRenderer.sendToHost(CH_INVOKE, {
        kind: "invoke",
        reqId,
        op,
        args: callArgs,
        // 身份随请求带上：主进程据此建立「uid → guest」绑定（协议 §6），
        // 也是「同 appId 多标签，请求只打到发起方」的实际依据。
        uid: instance.uid,
        appId: instance.appId,
        version: instance.version,
      });
    } catch (err) {
      // args 里带函数 / 循环引用时结构化克隆会失败：收成 reject，不让异常穿到页面的调用点外面
      clearTimeout(timer);
      pending.delete(reqId);
      reject(hostError("send-failed", String((err && err.message) || err)));
    }
  });
}

// ── 下行：事件分发（协议 §5）──
const listeners = new Map(); // event → Set<cb>

ipcRenderer.on(CH_EVENT, (_event, payload) => {
  if (!payload || typeof payload !== "object") return;
  const event = String(payload.event || "");
  const set = event ? listeners.get(event) : null;
  if (!set || !set.size) return;
  // 复制一份再遍历：回调里 off() 自己不会打断本轮分发
  for (const cb of Array.from(set)) {
    try { cb(payload); } catch (err) {
      // 页面回调抛错不能污染桥（也不会影响其它订阅者）
      try { console.error("[microapp] on(" + event + ") 回调抛错:", err); } catch { /* 忽略 */ }
    }
  }
});

/** 订阅下行流式事件，返回取消订阅函数（协议 §3：on(event, cb) => off）。 */
function on(event, cb) {
  const name = String(event || "");
  if (!name || typeof cb !== "function") return () => {};
  let set = listeners.get(name);
  if (!set) { set = new Set(); listeners.set(name, set); }
  set.add(cb);
  return () => {
    const cur = listeners.get(name);
    if (!cur) return;
    cur.delete(cb);
    if (!cur.size) listeners.delete(name);
  };
}

// ── 暴露（严格四项，多一个都不要加）──
// 特别提醒：不要在这里暴露 ipcRenderer / require / process / __dirname 或任何 Node 能力。
contextBridge.exposeInMainWorld("microapp", {
  version: BRIDGE_VERSION,
  invoke,
  on,
  instance,
});
