/**
 * 内置浏览器 · AI 宿主（plan-20261015-browser-agent）。
 *
 * 桌面端经全局 WS 登记为浏览器宿主，执行服务端下发的原语（清单见 server/app/orchestration/browser/driver.py），
 * 操作对象就是右侧面板里用户看得见的 webview：
 *
 * - 面板没打开 / 在看别的标签 / 切到别的会话 / 窗口最小化：webview 停泊在视口内照常运行（见 BrowserPanel），
 *   AI 照常操作；会话面板里还没有浏览器标签时自动加上，但**不展开面板**；
 * - AI 默认在自己的标签工作，不覆盖用户正在看的页面；用户最近在操作面板时也不强行切换显示的标签；
 * - 不打扰：键盘与文字输入走不抢焦点的通道（sendInputEvent / insertText，实测宿主焦点不动）；
 *   鼠标点击必然把焦点交给网页，点击前记录宿主焦点、点击后立即还原；用户正在输入法组字或刚按过键时，
 *   AI 的鼠标动作礼让顺延（最多 1.5 秒）；
 * - 可视化：动作文案、步数、AI 光标、目标高亮、点击波纹、截图闪白写进 browser store，由面板渲染；
 *   面板不可见时跳过一切动画等待，AI 全速执行。
 */
import { globalWsClient } from "../api/ws";
import { notify } from "../store/toast";
import { healParkedFrame } from "./parkGuard";
import { IDLE_AGENT, normalizeUrl, sessionKey, useBrowserStore, type BrowserTabState } from "../store/browser";
import { bucketKeyOf, usePanelStore } from "../store/panel";
import {
  findFrameByWcId, getFrame, hostSleep, waitFrameReady, waitNavigationSettled, type FrameRecord,
} from "./frames";

const PROTOCOL = 1;
const NOKIT = "__CC_NOKIT__";
const HOST_ID = Math.random().toString(36).slice(2, 10);

let kitSource = "";
let kitVersion = 0;
/** 会话 → AI 当前操作的标签 */
const agentTab = new Map<string, string>();
/** 会话 → 待随下一次结果回报的事件（新窗口转标签等） */
const pendingEvents = new Map<string, Array<Record<string, unknown>>>();
const idleTimers = new Map<string, number>();
const COUNTED_KIT = new Set(["snapshot", "text", "fill", "wait", "scrollTo", "selectAll", "paste"]);

// ── 用户输入礼让 ─────────────────────────────────────────
let lastKeyAt = 0;
let composing = false;

// 宿主等待走主进程计时：应用窗口被遮挡时页面定时器被节流到秒级（plan-20261015-browser-perf R1）
const sleep = hostSleep;

function withTimeout<T>(p: Promise<T>, ms: number, msg: string): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const t = window.setTimeout(() => reject(new Error(msg)), ms);
    p.then((v) => { window.clearTimeout(t); resolve(v); }, (e) => { window.clearTimeout(t); reject(e); });
  });
}

function errText(e: unknown): string {
  const s = e instanceof Error ? e.message : String(e);
  // Electron 把页面脚本异常包装成固定英文提示，换成可读说明。
  // plan-20261020-pts：服务端已把运行时错误包装成带回的值，到这里的多为脚本语法错误，其次才是页面跳转
  if (/Script failed to execute/i.test(s)) return "页面脚本执行失败（多为脚本语法错误，也可能是页面正在跳转或被弹窗阻塞）";
  return s;
}

async function politeness() {
  const t0 = Date.now();
  while ((composing || Date.now() - lastKeyAt < 350) && Date.now() - t0 < 1500) await sleep(60);
}

function hostFocus(): HTMLElement | null {
  const a = document.activeElement as HTMLElement | null;
  if (!a || a === document.body || a.tagName === "WEBVIEW" || a.tagName === "IFRAME") return null;
  return a;
}

/** 点击期间守住宿主焦点：网页一抢到焦点就还原（窗口压到毫秒级），结束时再兜底检查一次。 */
function guardFocus(el: any, prev: HTMLElement | null): () => void {
  if (!prev) return () => {};
  const restore = () => {
    const act = document.activeElement as HTMLElement | null;
    if (act !== prev && (!act || act === document.body || act.tagName === "WEBVIEW")) {
      try { prev.focus({ preventScroll: true }); } catch { /* 元素已卸载 */ }
    }
  };
  try { el.addEventListener("focus", restore); } catch { /* ignore */ }
  return () => {
    try { el.removeEventListener("focus", restore); } catch { /* ignore */ }
    restore();
  };
}

// ── 面板 / 标签 ─────────────────────────────────────────
function numericSid(sid: string): number | null {
  const n = Number(sid);
  return Number.isFinite(n) && n > 0 ? n : null;
}

/** 该会话的浏览器标签此刻是否正被用户看着（决定是否播放可视化动画） */
function tabOnScreen(sid: string, tabId: string): boolean {
  if (typeof document !== "undefined" && document.visibilityState !== "visible") return false;
  const panel = usePanelStore.getState();
  if (!panel.onScreen) return false;
  if (panel.activeBucket !== bucketKeyOf(numericSid(sid))) return false;
  if (!panel.activeKey || !panel.activeKey.startsWith("browser-")) return false;
  const st = useBrowserStore.getState().getSessionState(sid);
  return st.activeTabId === tabId && st.tabView === "preview";
}

/** 会话面板里确保有浏览器标签（不展开面板；面板折叠时设为当前标签，用户一展开就看到） */
function ensurePanelTab(sid: string) {
  usePanelStore.getState().ensureTabIn(numericSid(sid), "browser");
}

function tabsOf(sid: string): BrowserTabState[] {
  return useBrowserStore.getState().ensureSession(sid).tabs;
}

function openAgentTab(sid: string, url: string): BrowserTabState {
  const store = useBrowserStore.getState();
  const st = store.ensureSession(sid);
  const visible = tabOnScreen(sid, st.activeTabId);
  const userBusy = Date.now() - st.userActiveAt < 6000;
  // 用户正在看并操作面板时不切走他的标签，只给出「AI 在另一标签工作」提示
  const activate = !visible || !userBusy;
  const id = store.newTab(sid, url, { owner: "ai", activate });
  if (!activate) store.updateSessionState(sid, { agentElsewhere: id });
  agentTab.set(sid, id);
  return tabsOf(sid).find((t) => t.id === id)!;
}

/** 选出本次动作的标签：显式 > 偏好（服务端缓存）> AI 当前标签 > 空白页认领 > 用户当前页面 / 新开 AI 标签 */
function resolveTab(sid: string, explicit?: unknown, prefer?: unknown, forNav = false): BrowserTabState {
  const store = useBrowserStore.getState();
  // 先确保会话状态已创建：缺失会话拿到的是共享默认对象，用它的标签 id 写入会落空（E2E 实测首次导航失败）
  const st = store.ensureSession(sid);
  if (typeof explicit === "string" && explicit) {
    const t = st.tabs.find((x) => x.id === explicit);
    if (!t) throw new Error(`标签 ${explicit} 不存在，请用 browser_tabs 查看现有标签`);
    agentTab.set(sid, t.id);
    return t;
  }
  for (const id of [prefer, agentTab.get(sid)]) {
    if (typeof id === "string" && id) {
      const t = st.tabs.find((x) => x.id === id);
      if (t) { agentTab.set(sid, t.id); return t; }
    }
  }
  const active = st.tabs.find((t) => t.id === st.activeTabId);
  if (active && !active.url) {
    store.updateTab(sid, active.id, { owner: "ai" });
    agentTab.set(sid, active.id);
    return { ...active, owner: "ai" };
  }
  if (!forNav && active && active.url) {
    // 还没有 AI 标签时，读 / 操作用户正在看的页面（如「看看我当前打开的页面」）
    agentTab.set(sid, active.id);
    return active;
  }
  return openAgentTab(sid, "");
}

async function ensureFrame(sid: string, tab: BrowserTabState): Promise<FrameRecord> {
  ensurePanelTab(sid);
  const cur = tabsOf(sid).find((t) => t.id === tab.id) ?? tab;
  if (!cur.src && !cur.url) throw new Error("当前标签是空白页：请先用 browser_navigate 打开网页");
  const rec = await waitFrameReady(sid, tab.id, 12000);
  if (!rec) throw new Error("浏览器标签未能就绪（页面仍在加载或面板尚未挂载），请稍后重试");
  if (typeof rec.el?.executeJavaScript !== "function") throw new Error("当前环境不支持操作页面");
  // plan-20261018-operator-boost：停泊网页若被祖先包含块带离视口会完全不出帧，动作前先确保它在视口原点
  try { healParkedFrame(rec.el as unknown as Element); } catch { /* 守卫失败不影响动作 */ }
  return rec;
}

// ── 可视化与活动态 ───────────────────────────────────────
function begin(sid: string, tabId: string, label: string, count: boolean) {
  const t = idleTimers.get(sid);
  if (t) { window.clearTimeout(t); idleTimers.delete(sid); }
  const store = useBrowserStore.getState();
  const prev = store.getSessionState(sid).agent;
  const now = Date.now();
  const fresh = prev.phase === "idle" || now - prev.updatedAt > 20000;
  store.patchAgent(sid, {
    phase: "working",
    tabId,
    label: label || prev.label || "正在操作浏览器",
    steps: (fresh ? 0 : prev.steps) + (count ? 1 : 0),
    startedAt: fresh ? now : prev.startedAt,
    updatedAt: now,
  });
}

function scheduleIdle(sid: string) {
  const old = idleTimers.get(sid);
  if (old) window.clearTimeout(old);
  idleTimers.set(sid, window.setTimeout(() => {
    const store = useBrowserStore.getState();
    if (store.getSessionState(sid).agent.phase !== "working") return;
    store.patchAgent(sid, { phase: "done", highlight: null, updatedAt: Date.now() });
    idleTimers.set(sid, window.setTimeout(() => {
      idleTimers.delete(sid);
      const s2 = useBrowserStore.getState();
      if (s2.getSessionState(sid).agent.phase === "done") s2.patchAgent(sid, { ...IDLE_AGENT });
    }, 4000));
  }, 1800));
}

let vizSeq = 0;
/** zoom：网页缩放系数——页面坐标是 CSS 像素，面板叠加层按宿主像素绘制 */
function showTarget(sid: string, rect: unknown, x?: number, y?: number, zoom = 1) {
  const store = useBrowserStore.getState();
  const r = rect && typeof rect === "object" ? (rect as Record<string, number>) : null;
  const patch: Record<string, unknown> = {};
  if (r && Number.isFinite(r.width) && Number.isFinite(r.height)) {
    patch.highlight = { x: r.x * zoom, y: r.y * zoom, width: r.width * zoom, height: r.height * zoom, seq: ++vizSeq };
  }
  if (Number.isFinite(x) && Number.isFinite(y)) patch.cursor = { x: (x as number) * zoom, y: (y as number) * zoom };
  if (Object.keys(patch).length) store.patchAgent(sid, patch);
}

// ── 页面工具包 ───────────────────────────────────────────
async function evalKit(rec: FrameRecord, fn: string, args: unknown, timeoutMs = 15000): Promise<any> {
  const call = `(window.__ccKit && window.__ccKit.v === ${kitVersion}) ? window.__ccKit.call(${JSON.stringify(fn)}, ${JSON.stringify(args ?? {})}) : ${JSON.stringify(NOKIT)}`;
  let r = await withTimeout(Promise.resolve(rec.el.executeJavaScript(call)), timeoutMs, "页面没有响应（可能卡在弹窗或正在加载）");
  if (r === NOKIT) {
    if (!kitSource) throw new Error("页面工具包未就绪（与服务端的握手尚未完成），请稍后重试");
    await withTimeout(Promise.resolve(rec.el.executeJavaScript(kitSource)), 8000, "注入页面工具包超时");
    r = await withTimeout(Promise.resolve(rec.el.executeJavaScript(call)), timeoutMs, "页面没有响应（可能卡在弹窗或正在加载）");
  }
  if (!r || typeof r !== "object") throw new Error("页面工具执行失败");
  if (!r.ok) throw new Error(String(r.error || "页面工具执行失败"));
  return r.value;
}

async function mainExec(rec: FrameRecord | null, op: string, args: Record<string, unknown>): Promise<any> {
  const api = window.chatcoderAPI;
  if (!api?.browserAgentExec) throw new Error("当前环境不支持该操作");
  const res = await api.browserAgentExec(rec?.wcId ?? 0, op, args);
  if (!res?.ok) throw new Error(res?.error || "浏览器操作失败");
  return res.result;
}

async function drainEvents(sid: string, rec: FrameRecord | null): Promise<Array<Record<string, unknown>>> {
  const out = pendingEvents.get(sid) ?? [];
  pendingEvents.delete(sid);
  if (rec?.wcId) {
    try {
      const r = await mainExec(rec, "events", {});
      if (Array.isArray(r?.events)) out.push(...r.events);
    } catch { /* 主进程不可用时忽略 */ }
  }
  return out;
}

async function baseOf(sid: string, tabId: string, rec: FrameRecord | null) {
  const t = tabsOf(sid).find((x) => x.id === tabId);
  return { tab: tabId, url: t?.url ?? "", title: t?.title ?? "", events: await drainEvents(sid, rec) };
}

interface SettleOpts {
  /** 动作起点的网络序号（主进程 aiWindow 返回）；null 表示网络记录不可用 */
  netSeq: number | null;
  /** 动作起点的控制台序号 */
  consoleSeq: number;
  /** 动作起点的 DOM 就绪序号（判断跳转后的新页面是否已就绪） */
  domSince: number;
  /** 模型要求动作完成即截图 */
  shot: boolean;
  visible: boolean;
}

/** 动作后的「稳定」判定（plan-20261015-browser-perf）。桌面端优先走 settleViaMain（一次 IPC，事件驱动）；
 *  下面的轮询只在主进程不可用（非 webview 宿主 / 主进程尚未更新）时兜底。单循环约 30 ms 一轮——
 *  导航检测与 DOM 静默并行；只统计动作起点之后页面脚本发起的请求，在途数为 0 才算稳定（点击后等接口返回，
 *  报错提示随之出现）；DOM 静默由页面工具包过滤掉高频噪声节点。最短观察 60 ms、静默 100 ms，预算由动作决定。
 *  结束时一次取回：提示文字（含已消失的）、新出现的可操作元素、接口错误、控制台报错；出错时同一次调用里截图。 */
async function settleAfter(sid: string, tabId: string, rec: FrameRecord, since: number, settleMs: number, o: SettleOpts) {
  if (!settleMs) return { navigated: rec.navSeq !== since };
  if (rec.wcId) {
    const fast = await settleViaMain(sid, tabId, rec, since, settleMs, o);
    if (fast) return fast;
  }
  const t0 = performance.now();
  let budget = settleMs;
  let navigated = false;
  let st: Record<string, any> = {};
  let pending = 0;
  for (;;) {
    if (!navigated && rec.navSeq !== since) {
      navigated = true;
      await waitNavigationSettled(sid, tabId, since, 12000, { domSince: o.domSince, graceMs: 0 });
      budget = performance.now() - t0 + 800;
    }
    const [s1, n1] = await Promise.all([
      evalKit(rec, "state", {}, 4000).catch(() => null),
      o.netSeq !== null ? mainExec(rec, "netSince", { since: o.netSeq }).catch(() => null) : Promise.resolve(null),
    ]);
    if (s1) st = s1;
    pending = Number(n1?.pending) || 0;
    const el = performance.now() - t0;
    if (s1 && el >= 60 && Number(st.idle_ms ?? 1e9) >= 100 && pending === 0) break;
    if (!s1 && !navigated && rec.navSeq === since && el >= 60) break; // 页面忙且没在跳转：不空等
    if (el >= budget) break;
    await sleep(30);
  }
  const out: Record<string, any> = { ...st, navigated, waited: Math.round(performance.now() - t0) };
  if (pending > 0) out.pending = pending;
  const [chg, net] = await Promise.all([
    evalKit(rec, "changes", {}, 4000).catch(() => null),
    o.netSeq !== null ? mainExec(rec, "netSince", { since: o.netSeq, bodies: true }).catch(() => null) : Promise.resolve(null),
  ]);
  return finishSettle(sid, rec, out, chg, net, o);
}

/** 稳定判定走主进程一次完成（plan-20261017-ux-overhaul 阶段 F）：导航 / 接口返回 / DOM 静默先到先得，
 *  收尾的页面变化与接口错误也在同一次 IPC 里带回。跳转时等新页面 DOM 就绪，再给新页面一次短预算的静默判定。
 *  主进程不可用时返回 null，由调用方退回宿主轮询。 */
async function settleViaMain(sid: string, tabId: string, rec: FrameRecord, since: number, settleMs: number, o: SettleOpts) {
  const t0 = performance.now();
  const first: Record<string, any> | null = await mainExec(rec, "settle", { since: o.netSeq, budget_ms: settleMs, quiet_ms: 100, collect: true }).catch(() => null);
  if (!first) return null;
  let r: Record<string, any> = first;
  let navigated = !!r.navigated || rec.navSeq !== since;
  if (navigated) {
    // 主进程先于宿主收到跳转：宿主的 navSeq 可能还差一拍，给 300 ms 宽限，确实没跳就按未跳转收尾
    const moved = await waitNavigationSettled(sid, tabId, since, 12000, { domSince: o.domSince, graceMs: 300 });
    if (moved) {
      // 新文档没有工具包：watch 注入并开始记录变化，再判一次静默
      await evalKit(rec, "watch", {}, 3000).catch(() => null);
      r = await mainExec(rec, "settle", { since: o.netSeq, budget_ms: 800, quiet_ms: 100, collect: true }).catch(() => null) ?? {};
    } else {
      navigated = false;
      r = await mainExec(rec, "settle", { since: o.netSeq, budget_ms: Math.max(0, settleMs - (performance.now() - t0)), quiet_ms: 100, collect: true }).catch(() => null) ?? {};
    }
  }
  const out: Record<string, any> = { ...(r.state ?? {}), navigated, waited: Math.round(performance.now() - t0) };
  if (Number(r.pending) > 0) out.pending = Number(r.pending);
  return finishSettle(sid, rec, out, r.changes ?? null, r.net ?? null, o);
}

/** 稳定判定收尾：合并页面变化、接口错误、控制台报错；出错或模型要求时同一次调用里截图 */
async function finishSettle(sid: string, rec: FrameRecord, out: Record<string, any>, chg: Record<string, any> | null, net: Record<string, any> | null, o: SettleOpts) {
  if (chg) {
    if (chg.notices?.length) out.notices = chg.notices;
    if (chg.added?.length) out.added = chg.added;
    if (chg.more) out.added_more = true;
    if (chg.dialog) out.new_dialog = chg.dialog;
  }
  if (net?.errors?.length) out.net_errors = net.errors;
  const cerr = rec.console.filter((e) => e.seq > o.consoleSeq && e.level === "error").slice(-3);
  if (cerr.length) out.console_errors = cerr.map((e) => e.text.slice(0, 300));
  const hasError = !!(out.net_errors || out.new_dialog || (out.notices || []).some((n: any) => n.level === "error"));
  if (o.shot || hasError) {
    try {
      out.shot = await mainExec(rec, "capture", { max_side: 1280, quality: 72 });
      out.shot_reason = o.shot ? "requested" : "error";
      if (o.visible) {
        const store = useBrowserStore.getState();
        store.patchAgent(sid, { flash: store.getSessionState(sid).agent.flash + 1 });
      }
    } catch (e) {
      out.shot_error = errText(e);
    }
  }
  return out;
}

/** marks 截图：在截图上画编号框（只改图片，不改页面 DOM、不改面板视觉），模型按编号对应的 ref 操作 */
async function drawMarks(res: Record<string, any>, items: Array<Record<string, any>>): Promise<Record<string, unknown>> {
  const raw = atob(String(res.base64 || ""));
  const bytes = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
  const bmp = await createImageBitmap(new Blob([bytes], { type: String(res.mime || "image/jpeg") }));
  const canvas = new OffscreenCanvas(bmp.width, bmp.height);
  const g = canvas.getContext("2d");
  if (!g) throw new Error("no 2d");
  g.drawImage(bmp, 0, 0);
  const k = bmp.width / Math.max(1, Number(res.css_width) || bmp.width);
  g.lineWidth = 2;
  g.font = "bold 12px sans-serif";
  g.textBaseline = "top";
  for (const m of items) {
    const r = m.rect || {};
    const x = r.x * k, y = r.y * k, w = r.width * k, h = r.height * k;
    g.strokeStyle = "#e5484d";
    g.strokeRect(x, y, w, h);
    const label = String(m.n);
    const tw = g.measureText(label).width + 6;
    const ly = y >= 15 ? y - 15 : y;
    g.fillStyle = "#e5484d";
    g.fillRect(x, ly, tw, 15);
    g.fillStyle = "#ffffff";
    g.fillText(label, x + 3, ly + 1);
  }
  const blob = await canvas.convertToBlob({ type: "image/jpeg", quality: 0.8 });
  const buf = new Uint8Array(await blob.arrayBuffer());
  let bin = "";
  for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode(...buf.subarray(i, i + 0x8000));
  return { base64: btoa(bin), mime: "image/jpeg", width: bmp.width, height: bmp.height };
}

/** 等待条件（文字 / 元素出现或消失、加载完成）：宿主侧轮询页面的同步检查，理由同上 */
async function waitCondition(rec: FrameRecord, args: Record<string, any>): Promise<Record<string, unknown>> {
  const timeout = Math.max(100, Math.min(Number(args.timeout_ms) || 10000, 60000));
  const t0 = performance.now();
  for (;;) {
    let r: Record<string, any> = {};
    try {
      r = await evalKit(rec, "check", args, 4000);
    } catch (e) {
      if (/选择器无效/.test(errText(e))) throw e;
    }
    const waited = Math.round(performance.now() - t0);
    if (r.met) return { ...r, waited };
    if (waited >= timeout) return { ...r, met: false, waited };
    await sleep(100);
  }
}

// ── 输入 ─────────────────────────────────────────────────
const KEY_MAP: Record<string, string> = {
  Enter: "Enter", Escape: "Escape", Tab: "Tab", Backspace: "Backspace", Delete: "Delete", Insert: "Insert",
  Home: "Home", End: "End", PageUp: "PageUp", PageDown: "PageDown",
  ArrowUp: "Up", ArrowDown: "Down", ArrowLeft: "Left", ArrowRight: "Right", " ": "Space",
};

function parseCombo(combo: string): { key: string; mods: string[] } {
  if (combo === "+" || combo.endsWith("++")) {
    const mods = combo === "+" ? [] : combo.slice(0, -2).split("+");
    return { key: "+", mods };
  }
  const parts = combo.split("+");
  const key = parts.pop() || "";
  return { key, mods: parts };
}

function sendKeyCombo(el: any, combo: string) {
  const { key, mods } = parseCombo(combo);
  const modifiers = mods.map((m) => m.toLowerCase()).filter((m) => ["control", "shift", "alt", "meta"].includes(m));
  const code = KEY_MAP[key] ?? (key.length === 1 ? key : key);
  el.sendInputEvent({ type: "keyDown", keyCode: code, modifiers });
  const hasCmd = modifiers.some((m) => m === "control" || m === "alt" || m === "meta");
  if (!hasCmd) {
    if (key === "Enter") el.sendInputEvent({ type: "char", keyCode: "\r", modifiers });
    else if (key.length === 1) el.sendInputEvent({ type: "char", keyCode: key, modifiers });
  }
  el.sendInputEvent({ type: "keyUp", keyCode: code, modifiers });
}

function mouseButtons(mods: string[]): string[] {
  return mods.map((m) => m.toLowerCase()).filter((m) => ["control", "shift", "alt", "meta"].includes(m));
}

/** 网页缩放系数：sendInputEvent 的坐标是 DIP，页面坐标是 CSS 像素，缩放不是 100% 时必须换算（R10） */
function zoomOf(rec: FrameRecord): number {
  try {
    const z = typeof rec.el?.getZoomFactor === "function" ? Number(rec.el.getZoomFactor()) : 1;
    return Number.isFinite(z) && z > 0 ? z : 1;
  } catch { return 1; }
}

async function dispatchMouse(el: any, a: Record<string, any>, zoom = 1) {
  const x = Math.round((Number(a.x) || 0) * zoom);
  const y = Math.round((Number(a.y) || 0) * zoom);
  const button = a.button === "right" ? "right" : a.button === "middle" ? "middle" : "left";
  const modifiers = mouseButtons(Array.isArray(a.modifiers) ? a.modifiers : []);
  const action = String(a.action || "click");
  if (action === "move") {
    el.sendInputEvent({ type: "mouseMove", x: Math.max(0, x - 6), y: Math.max(0, y - 6), modifiers });
    el.sendInputEvent({ type: "mouseMove", x, y, modifiers });
    return;
  }
  if (action === "wheel") {
    el.sendInputEvent({ type: "mouseMove", x, y, modifiers });
    // Electron 的滚轮方向与页面滚动方向相反：deltaY 为负表示内容向下滚
    el.sendInputEvent({ type: "mouseWheel", x, y, deltaX: -Number(a.dx || 0), deltaY: -Number(a.dy || 0), canScroll: true, modifiers });
    return;
  }
  if (action === "drag") {
    const tx = a.to_x != null ? Math.round(Number(a.to_x) * zoom) : x;
    const ty = a.to_y != null ? Math.round(Number(a.to_y) * zoom) : y;
    const steps = Math.max(2, Math.min(Number(a.steps) || 12, 40));
    el.sendInputEvent({ type: "mouseMove", x, y, modifiers });
    el.sendInputEvent({ type: "mouseDown", x, y, button: "left", clickCount: 1, modifiers });
    for (let i = 1; i <= steps; i++) {
      const px = Math.round(x + ((tx - x) * i) / steps);
      const py = Math.round(y + ((ty - y) * i) / steps);
      el.sendInputEvent({ type: "mouseMove", x: px, y: py, button: "left", modifiers: [...modifiers, "leftButtonDown"] });
      await sleep(16);
    }
    el.sendInputEvent({ type: "mouseUp", x: tx, y: ty, button: "left", clickCount: 1, modifiers });
    return;
  }
  const count = Math.max(1, Math.min(Number(a.count) || 1, 3));
  el.sendInputEvent({ type: "mouseMove", x, y, modifiers });
  for (let i = 1; i <= count; i++) {
    el.sendInputEvent({ type: "mouseDown", x, y, button, clickCount: i, modifiers });
    el.sendInputEvent({ type: "mouseUp", x, y, button, clickCount: i, modifiers });
  }
}

// ── 点击送达校验（plan-20261017-hidden-input）────────────
// 应用窗口被遮挡 / 最小化时，sendInputEvent 的鼠标事件曾被观察到完全到不了网页，而结果照报「已点击」。
// 派发前在页面挂一个只认可信事件的探针，派发后确认 pointerdown / mousedown 真的到达；没到就改走 CDP 再派发一次，
// 仍然没到就如实报错。目标落在 iframe 里时顶层页面收不到事件，无从判断，此时不校验（也就不会重复点击）。
const PROBE_INSTALL = (x: number, y: number) => `(() => {
  const p = window.__ccInputProbe || (window.__ccInputProbe = { n: 0, on: false });
  p.n = 0;
  if (!p.on) {
    p.on = true;
    const f = (e) => { if (e.isTrusted) p.n++; };
    // 多认几种事件：页面在 window 捕获阶段拦掉其中一种时不至于误判为未送达
    for (const t of ["pointerdown", "mousedown", "pointerup", "mouseup", "click", "contextmenu"]) window.addEventListener(t, f, true);
  }
  const el = document.elementFromPoint(${x}, ${y});
  return el ? el.tagName : "";
})()`;
// 页面内等待：guest 已关后台节流，定时器不受宿主隐藏影响；事件一到立即返回
const PROBE_CHECK = `new Promise((resolve) => {
  const p = window.__ccInputProbe; const t0 = performance.now();
  const tick = () => {
    if (!p) return resolve(-1);
    if (p.n > 0) return resolve(p.n);
    if (performance.now() - t0 > 300) return resolve(0);
    setTimeout(tick, 8);
  };
  tick();
})`;
const FRAME_TAGS = new Set(["IFRAME", "FRAME", "EMBED", "OBJECT"]);

/** 装探针；返回 false 表示这次点击无法校验（目标在框架内或页面脚本不可用） */
async function probeInstall(rec: FrameRecord, x: number, y: number): Promise<boolean> {
  try {
    const tag = await withTimeout(Promise.resolve(rec.el.executeJavaScript(PROBE_INSTALL(x, y), false)), 3000, "probe");
    return typeof tag === "string" && !FRAME_TAGS.has(tag);
  } catch { return false; }
}

/** true = 已送达；false = 确认未送达；null = 无法判断（页面已跳转、脚本被弹窗阻塞等，按已送达处理，避免重复点击） */
async function probeCheck(rec: FrameRecord, navSince: number): Promise<boolean | null> {
  if (rec.navSeq !== navSince) return null;
  try {
    const n = await withTimeout(Promise.resolve(rec.el.executeJavaScript(PROBE_CHECK, false)), 3000, "probe");
    if (rec.navSeq !== navSince) return null;
    return typeof n === "number" && n >= 0 ? n > 0 : null;
  } catch { return null; }
}

/** 宿主窗口当前是否处于后台（被遮挡 / 最小化时宿主页面会被判为 hidden），用于报错说明 */
function hostBackground(): boolean {
  return document.visibilityState === "hidden";
}

// ── 原语分派 ─────────────────────────────────────────────
async function listTabs(sid: string) {
  const st = useBrowserStore.getState().getSessionState(sid);
  const agent = agentTab.get(sid) ?? null;
  return {
    tabs: st.tabs.filter((t) => t.url || t.src).map((t) => ({
      id: t.id, url: t.url, title: t.title, loading: t.loading, active: t.id === st.activeTabId, owner: t.owner,
    })),
    active: st.activeTabId,
    agent_tab: agent,
    visible: tabOnScreen(sid, st.activeTabId),
  };
}

/** 导航默认等到主框架 DOM 就绪、页面脚本的请求与 DOM 稳定即返回（R4）；wait=load 才等图片、字体等全部加载完。 */
async function navigateTab(sid: string, tab: BrowserTabState, a: Record<string, any>) {
  const action = a.action as string | undefined;
  const timeout = Math.max(3000, Math.min(Number(a.timeout_ms) || 30000, 60000));
  const untilLoad = a.wait === "load";
  if (action === "back" || action === "forward" || action === "reload") {
    const rec = await ensureFrame(sid, tab);
    const since = rec.navSeq;
    const domSince = rec.domSeq;
    const netSeq = await netMark(rec);
    if (action === "back") { if (!rec.el.canGoBack()) throw new Error("没有可后退的页面"); rec.el.goBack(); }
    else if (action === "forward") { if (!rec.el.canGoForward()) throw new Error("没有可前进的页面"); rec.el.goForward(); }
    else rec.el.reload();
    await waitNavigationSettled(sid, tab.id, since, timeout, { untilLoad, domSince, graceMs: 1500 });
    if (!untilLoad) await quietAfterNav(rec, netSeq, 1500);
    return { rec, status: undefined };
  }
  const url = normalizeUrl(String(a.url || ""));
  if (!url) throw new Error("url 不能为空");
  ensurePanelTab(sid);
  const store = useBrowserStore.getState();
  const rec0 = getFrame(sid, tab.id);
  if (rec0?.ready && typeof rec0.el?.loadURL === "function") {
    store.updateTab(sid, tab.id, { url, loading: true, error: undefined });
    const since = rec0.navSeq;
    const domSince = rec0.domSeq;
    const netSeq = await netMark(rec0);
    let failed: string | null = null;
    let finished = false;
    Promise.resolve(rec0.el.loadURL(url)).then(
      () => { finished = true; },
      (e: unknown) => { failed = errText(e); finished = true; },
    );
    const t0 = performance.now();
    while (performance.now() - t0 < timeout) {
      if (failed) throw new Error(`页面加载失败：${loadErrorText(failed, url)}`);
      if (finished) break;
      if (!untilLoad && rec0.navSeq !== since && rec0.domSeq > domSince) break;
      await sleep(30);
    }
    if (!untilLoad) await quietAfterNav(rec0, netSeq, 1500);
    return { rec: rec0, status: undefined };
  }
  store.navigate(sid, url, tab.id);
  const rec = await waitFrameReady(sid, tab.id, 15000);
  if (!rec) throw new Error("浏览器标签未能就绪，请稍后重试");
  // 首次挂载：webview 就绪即 DOM 就绪；只有显式要求完整加载时继续等
  const t0 = performance.now();
  while (performance.now() - t0 < timeout) {
    const t = tabsOf(sid).find((x) => x.id === tab.id);
    if (t?.error) throw new Error(`页面加载失败：${loadErrorText(t.error, url)}`);
    if (!untilLoad || (!rec.loading && !t?.loading)) break;
    await sleep(30);
  }
  if (!untilLoad) await quietAfterNav(rec, await netMark(rec), 1500);
  return { rec, status: undefined };
}

/** 当前网络序号（之后发起的请求才算本次动作的） */
async function netMark(rec: FrameRecord): Promise<number | null> {
  if (!rec.wcId) return null;
  try {
    const r = await mainExec(rec, "netSince", { since: Number.MAX_SAFE_INTEGER });
    return r?.available ? Number(r.seq) || 0 : null;
  } catch { return null; }
}

/** 新页面 DOM 就绪后：页面脚本的首批请求返回、DOM 静默 100 ms 即可（上限 budget） */
async function quietAfterNav(rec: FrameRecord, netSeq: number | null, budget: number) {
  const t0 = performance.now();
  try { await evalKit(rec, "watch", {}, 3000); } catch { return; }
  if (rec.wcId) {
    // 主进程一次判定（接口返回 + DOM 静默，事件驱动）；主进程不支持时退回下面的轮询
    const left = Math.max(0, budget - (performance.now() - t0));
    const r = await mainExec(rec, "settle", { since: netSeq, budget_ms: left, quiet_ms: 100 }).catch(() => null);
    if (r) return;
  }
  for (;;) {
    const [st, n] = await Promise.all([
      evalKit(rec, "state", {}, 3000).catch(() => null),
      netSeq !== null ? mainExec(rec, "netSince", { since: netSeq }).catch(() => null) : Promise.resolve(null),
    ]);
    const el = performance.now() - t0;
    if (!st || el >= budget) return;
    if (el >= 60 && Number(st.idle_ms ?? 1e9) >= 100 && !(Number(n?.pending) > 0)) return;
    await sleep(30);
  }
}

function loadErrorText(raw: string, url: string): string {
  raw = raw.replace(/^Error invoking remote method '[^']*':\s*/i, "").replace(/^Error:\s*/i, "");
  if (/ERR_CONNECTION_REFUSED|-102/.test(raw)) return `连接被拒绝（${url}）——如果是本地开发地址，请确认开发服务器已启动`;
  if (/ERR_UNSAFE_PORT|-312/.test(raw)) return `该端口被浏览器视为不安全端口，无法访问（${url}）`;
  if (/ERR_CONNECTION_TIMED_OUT|-118/.test(raw)) return `连接超时（${url}）`;
  if (/ERR_NAME_NOT_RESOLVED|-105/.test(raw)) return `域名无法解析（${url}）`;
  if (/ERR_INTERNET_DISCONNECTED|-106/.test(raw)) return "网络未连接";
  if (/ERR_CERT/.test(raw)) return `证书错误（${url}）`;
  if (/ERR_ABORTED|-3\b/.test(raw)) return "加载被中止（可能被新的跳转替代）";
  return raw.slice(0, 200);
}

async function dispatch(sid: string, op: string, a: Record<string, any>): Promise<Record<string, unknown>> {
  const store = useBrowserStore.getState();
  if (op === "tabs.list") return listTabs(sid);
  if (op === "clipboard") return mainExec(null, "clipboard", a);
  // plan-20261018-operator-boost：下载记录由主进程维护（保存到设置的下载目录），与标签无关
  if (op === "downloads") return mainExec(null, "downloads", a);
  if (op === "tabs.open") {
    begin(sid, "", a.label || "新建标签", true);
    const tab = openAgentTab(sid, a.url ? normalizeUrl(String(a.url)) : "");
    let rec: FrameRecord | null = null;
    if (a.url) {
      ensurePanelTab(sid);
      rec = await waitFrameReady(sid, tab.id, 15000);
    }
    return baseOf(sid, tab.id, rec);
  }
  if (op === "tabs.select") {
    const t = resolveTab(sid, a.tab);
    if (!tabOnScreen(sid, store.getSessionState(sid).activeTabId) || Date.now() - store.getSessionState(sid).userActiveAt > 6000) {
      store.setActiveTab(sid, t.id);
    }
    return baseOf(sid, t.id, getFrame(sid, t.id) ?? null);
  }
  if (op === "tabs.close") {
    const t = resolveTab(sid, a.tab);
    store.closeTab(sid, t.id);
    if (agentTab.get(sid) === t.id) agentTab.delete(sid);
    return { closed: t.id };
  }

  let tab = resolveTab(sid, a.tab, a.prefer_tab, op === "nav");
  if (op === "nav" && a.new_tab) tab = openAgentTab(sid, "");
  const label = typeof a.label === "string" ? a.label : "";

  if (op === "nav") {
    begin(sid, tab.id, label || "打开网页", true);
    const { rec } = await navigateTab(sid, tab, a);
    return { ...(await baseOf(sid, tab.id, rec)) };
  }
  if (op === "viz") {
    begin(sid, tab.id, label, false);
    showTarget(sid, a.rect);
    return baseOf(sid, tab.id, getFrame(sid, tab.id) ?? null);
  }

  const rec = await ensureFrame(sid, tab);
  const visible = tabOnScreen(sid, tab.id);

  if (op === "kit") {
    const fn = String(a.fn || "");
    begin(sid, tab.id, label, COUNTED_KIT.has(fn));
    if (visible && a.rect) showTarget(sid, a.rect);
    const value = fn === "wait" ? await waitCondition(rec, a.args ?? {}) : await evalKit(rec, fn, a.args ?? {}, 20000);
    if (visible && value && typeof value === "object" && value.rect && (fn === "focus" || fn === "scrollTo" || fn === "fill")) {
      showTarget(sid, value.rect);
    }
    return { ...(await baseOf(sid, tab.id, rec)), value };
  }

  if (op === "eval") {
    begin(sid, tab.id, label || "执行页面脚本", true);
    const script = String(a.script || "");
    let value: unknown;
    try {
      value = await withTimeout(Promise.resolve(rec.el.executeJavaScript(script, false)), 20000, "脚本执行超时（20 秒）");
    } catch (e) {
      const m = errText(e);
      if (!/could not be cloned/i.test(m)) throw new Error(`脚本执行出错：${m}`);
      // 返回值不可序列化（DOM 节点等）：在页面内转成 JSON / 文本再取回
      const wrapped = `(async () => { const __v = await (0, eval)(${JSON.stringify(script)}); try { return JSON.parse(JSON.stringify(__v === undefined ? null : __v)); } catch (e) { return String(__v); } })()`;
      value = await withTimeout(Promise.resolve(rec.el.executeJavaScript(wrapped, false)), 20000, "脚本执行超时（20 秒）");
    }
    return { ...(await baseOf(sid, tab.id, rec)), value };
  }

  if (op === "mouse" || op === "keys" || op === "text") {
    begin(sid, tab.id, label || (op === "mouse" ? "点击页面" : op === "keys" ? "按键" : "输入文字"), true);
    const settleMs = Math.max(0, Math.min(Number(a.settle_ms) || 0, 8000));
    const consoleSeq = rec.console.length ? rec.console[rec.console.length - 1].seq : 0;
    // 准备步骤并行（R3）：AI 动作窗口（期间网页弹窗按 AI 策略应答）+ 开始记录页面变化
    const [win] = await Promise.all([
      mainExec(rec, "aiWindow", { ms: settleMs + 4000, accept: a.on_dialog !== "dismiss", prompt_text: a.prompt_text || "" }).catch(() => null),
      evalKit(rec, "watch", {}, 4000).catch(() => null), // 页面在跳转：settle 时只看导航
      // 应用窗口在后台时：页面导航后若还没出过帧，先补一帧，否则输入事件全被丢弃（plan-20261017-hidden-input）。
      // plan-20261018-operator-boost：是否在后台交给主进程判断（它看得到窗口最小化 / 遮挡）——AI 操作期间主窗口
      // 不节流，页面可见性不再可靠；窗口在前台或无需补帧时主进程立即返回。
      mainExec(rec, "ensureFrame", {}).catch(() => null),
    ]);
    const netSeq = win && typeof win.net_seq === "number" ? win.net_seq : null;
    const since = rec.navSeq;
    const domSince = rec.domSeq;
    const zoom = zoomOf(rec);
    let release = () => {};
    let aimed: Record<string, any> | null = null;
    let deliveredVia = "";
    try {
      if (op === "mouse") {
        const act = String(a.action || "click");
        if (act !== "wheel") await politeness();
        let x = Number(a.x);
        let y = Number(a.y);
        // 光标先到位再点击，让用户看清 AI 要点哪里；与点击前校准重叠进行，不额外加等待
        let cursor: Promise<void> = Promise.resolve();
        if (visible && act !== "wheel") {
          showTarget(sid, a.rect, x, y, zoom);
          cursor = sleep(100);
        }
        if (a.ref && (act === "click" || act === "move")) {
          // 点击前最终校准（R11）：等目标停止移动（展开动画）、复核命中，按最终坐标派发
          const [r] = await Promise.all([evalKit(rec, "aim", { ref: a.ref }, 4000), cursor]);
          aimed = r;
          if (Math.abs(r.x - x) > 2 || Math.abs(r.y - y) > 2) {
            x = r.x;
            y = r.y;
            if (visible) showTarget(sid, r.rect, x, y, zoom);
          }
        } else {
          await cursor;
        }
        release = guardFocus(rec.el, hostFocus());
        // activate：目标被遮挡时直接在元素上派发点击（不经过坐标命中，遮挡物拦不住）
        if (act === "activate") await evalKit(rec, "activate", { ref: a.ref }, 8000);
        else if (act === "click") {
          const checkable = await probeInstall(rec, x, y);
          await dispatchMouse(rec.el, { ...a, x, y }, zoom);
          if (checkable && (await probeCheck(rec, since)) === false) {
            // 没送达（plan-20261017-hidden-input）：先强制补一帧按原通道重派；仍不行改走 CDP；再不行如实报错
            const bg = hostBackground();
            let viaErr = "";
            await mainExec(rec, "ensureFrame", { force: true }).catch(() => null);
            await probeInstall(rec, x, y);
            await dispatchMouse(rec.el, { ...a, x, y }, zoom);
            let ok = await probeCheck(rec, since);
            if (ok === false) {
              try {
                await probeInstall(rec, x, y);
                // CDP 坐标是页面 CSS 像素，不乘缩放
                await mainExec(rec, "mouseCdp", {
                  x, y, button: a.button, count: a.count, modifiers: Array.isArray(a.modifiers) ? a.modifiers : [],
                });
              } catch (e) { viaErr = errText(e); }
              ok = viaErr ? false : await probeCheck(rec, since);
              if (ok !== false) deliveredVia = "cdp";
            } else deliveredVia = "retry";
            if (ok === false) {
              throw new Error(`点击没有送达页面${bg ? "：应用窗口在后台（被遮挡或最小化）时网页收不到鼠标事件" : ""}`
                + `${viaErr ? `（备用通道失败：${viaErr}）` : ""}，请把应用窗口切到前台后重试`);
            }
          }
        } else await dispatchMouse(rec.el, { ...a, x, y }, zoom);
        if (visible && (act === "click" || act === "activate")) {
          store.patchAgent(sid, { ripple: { x: x * zoom, y: y * zoom, seq: ++vizSeq } });
        }
      } else if (op === "keys") {
        if (visible && a.rect) showTarget(sid, a.rect, undefined, undefined, zoom);
        const keys: string[] = Array.isArray(a.keys) ? a.keys.map(String) : [];
        const repeat = Math.max(1, Math.min(Number(a.repeat) || 1, 50));
        for (let r = 0; r < repeat; r++) {
          for (const combo of keys) {
            sendKeyCombo(rec.el, combo);
            if (keys.length > 1 || repeat > 1) await sleep(12);
          }
        }
      } else {
        if (visible && a.rect) showTarget(sid, a.rect, undefined, undefined, zoom);
        await Promise.resolve(rec.el.insertText(String(a.text ?? "")));
      }
      const settle = await settleAfter(sid, tab.id, rec, since, settleMs, {
        netSeq, consoleSeq, domSince, shot: !!a.shot, visible,
      });
      return { ...(await baseOf(sid, tab.id, rec)), settle, ...(aimed ? { aimed } : {}), ...(deliveredVia ? { delivered_via: deliveredVia } : {}) };
    } finally {
      // 焦点守卫覆盖整个稳定观察期：网页随后抢焦点也会被即时还原（取代原来的固定 30 ms 等待）
      release();
    }
  }

  if (op === "capture") {
    begin(sid, tab.id, label || "截图", true);
    let marks: Record<string, any> | null = null;
    if (a.marks) {
      try { marks = await evalKit(rec, "marks", {}, 8000); } catch { marks = null; }
    }
    let res = await mainExec(rec, "capture", a);
    if (marks?.items?.length) {
      try { res = { ...res, ...(await drawMarks(res, marks.items)) }; } catch { /* 画框失败时返回原图 */ }
      res.marks = marks.items.map((m: any) => ({ n: m.n, ref: m.ref, label: m.label }));
    }
    if (visible) store.patchAgent(sid, { flash: store.getSessionState(sid).agent.flash + 1 });
    return { ...(await baseOf(sid, tab.id, rec)), ...res };
  }

  if (op === "logs") {
    if (a.kind === "network") {
      const res = await mainExec(rec, "network", a);
      if (res && res.available === false) {
        return { tab: tab.id, entries: [], total: 0, note: "网络记录不可用（调试通道未连接）" };
      }
      return { tab: tab.id, ...res };
    }
    let entries = rec.console.slice();
    if (a.only_errors) entries = entries.filter((e) => e.level === "error");
    const pat = String(a.pattern || "").toLowerCase();
    if (pat) entries = entries.filter((e) => e.text.toLowerCase().includes(pat));
    const total = entries.length;
    const limit = Math.max(1, Math.min(Number(a.limit) || 50, 300));
    entries = entries.slice(-limit);
    if (a.clear) rec.console.length = 0;
    return { tab: tab.id, entries, total };
  }

  if (op === "net_body") {
    const res = await mainExec(rec, "netBody", a);
    return { tab: tab.id, ...res };
  }

  throw new Error(`未知的浏览器原语：${op}`);
}

async function handleRequest(p: Record<string, any>) {
  const requestId = String(p.request_id || "");
  const sid = sessionKey(p.session_id as number);
  try {
    const result = await dispatch(sid, String(p.op || ""), (p.args || {}) as Record<string, any>);
    globalWsClient.send("browser.response", { request_id: requestId, ok: true, result });
  } catch (e) {
    globalWsClient.send("browser.response", { request_id: requestId, ok: false, error: errText(e) });
  } finally {
    scheduleIdle(sid);
  }
}

// ── 主进程事件：网页弹窗、新窗口 ─────────────────────────
function onMainEvent(ev: { type: string; wcId: number; [k: string]: unknown }) {
  const rec = findFrameByWcId(Number(ev.wcId));
  if (!rec) return;
  const store = useBrowserStore.getState();
  if (ev.type === "dialog") {
    store.setDialog(rec.sid, {
      tabId: rec.tabId, wcId: Number(ev.wcId), type: String(ev.dialogType || "alert"),
      message: String(ev.message || ""), defaultPrompt: String(ev.defaultPrompt || ""),
    });
    return;
  }
  if (ev.type === "dialog-closed") {
    const cur = store.getSessionState(rec.sid).dialog;
    if (cur && cur.wcId === Number(ev.wcId)) store.setDialog(rec.sid, null);
    return;
  }
  if (ev.type === "download") {
    // plan-20261018-operator-boost：下载不再弹系统保存框，完成时告诉用户文件存到了哪里
    const state = String(ev.state || "");
    const name = String(ev.filename || "");
    if (state === "completed") notify.success(`已下载「${name}」，保存在 ${String(ev.path || "")}`);
    else if (state === "interrupted") notify.warning(`「${name}」下载中断`);
    return;
  }
  if (ev.type === "popup") {
    const url = String(ev.url || "");
    if (!url) return;
    const st = store.getSessionState(rec.sid);
    const source = st.tabs.find((t) => t.id === rec.tabId);
    const aiActive = st.agent.phase === "working" && agentTab.get(rec.sid) === rec.tabId;
    const activate = st.activeTabId === rec.tabId && (!aiActive || !tabOnScreen(rec.sid, rec.tabId) || Date.now() - st.userActiveAt > 6000);
    const id = store.newTab(rec.sid, url, { owner: source?.owner ?? "user", activate });
    if (aiActive) {
      agentTab.set(rec.sid, id);
      const list = pendingEvents.get(rec.sid) ?? [];
      list.push({ type: "popup", tab: id, url });
      pendingEvents.set(rec.sid, list);
    }
  }
}

/** 应用启动时调用（仅桌面端）；返回停止函数。 */
export function startBrowserAgentHost(): () => void {
  const api = window.chatcoderAPI;
  if (!api?.browserAgentExec || !api?.browserAgentPrepare) return () => {};
  const onKey = () => { lastKeyAt = Date.now(); };
  const onCompStart = () => { composing = true; };
  const onCompEnd = () => { composing = false; };
  window.addEventListener("keydown", onKey, true);
  window.addEventListener("compositionstart", onCompStart, true);
  window.addEventListener("compositionend", onCompEnd, true);

  const offMsg = globalWsClient.on((ev) => {
    if (ev.event === "browser.request") {
      void handleRequest(ev.payload as Record<string, any>);
    } else if (ev.event === "browser.host.welcome") {
      const p = ev.payload as Record<string, unknown>;
      kitSource = String(p.kit_source || "");
      kitVersion = Number(p.kit_version || 0);
    }
  });
  const offOpen = globalWsClient.onOpen(() => {
    globalWsClient.send("browser.host.hello", {
      host_id: HOST_ID,
      protocol: PROTOCOL,
      capabilities: ["tabs", "nav", "kit", "eval", "mouse", "keys", "text", "capture", "logs", "network", "dialogs", "downloads"],
    });
  });
  const offMain = api.onBrowserAgentEvent?.((ev) => onMainEvent(ev));

  return () => {
    globalWsClient.send("browser.host.bye", { host_id: HOST_ID });
    offMsg();
    offOpen();
    offMain?.();
    window.removeEventListener("keydown", onKey, true);
    window.removeEventListener("compositionstart", onCompStart, true);
    window.removeEventListener("compositionend", onCompEnd, true);
  };
}

/** 面板「停止」按钮等需要的：会话内 AI 当前标签（无则 null） */
export function agentTabOf(sid: string | number | null | undefined): string | null {
  return agentTab.get(sessionKey(sid)) ?? null;
}
