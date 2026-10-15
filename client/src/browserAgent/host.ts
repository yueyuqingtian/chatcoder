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
import { IDLE_AGENT, normalizeUrl, sessionKey, useBrowserStore, type BrowserTabState } from "../store/browser";
import { bucketKeyOf, usePanelStore } from "../store/panel";
import {
  findFrameByWcId, getFrame, waitFrameReady, waitNavigationSettled, type FrameRecord,
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

const sleep = (ms: number) => new Promise<void>((r) => window.setTimeout(r, ms));

function withTimeout<T>(p: Promise<T>, ms: number, msg: string): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const t = window.setTimeout(() => reject(new Error(msg)), ms);
    p.then((v) => { window.clearTimeout(t); resolve(v); }, (e) => { window.clearTimeout(t); reject(e); });
  });
}

function errText(e: unknown): string {
  const s = e instanceof Error ? e.message : String(e);
  // Electron 把页面脚本异常包装成固定英文提示，换成可读说明
  if (/Script failed to execute/i.test(s)) return "页面脚本执行失败（页面可能正在跳转或被弹窗阻塞）";
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
function showTarget(sid: string, rect: unknown, x?: number, y?: number) {
  const store = useBrowserStore.getState();
  const r = rect && typeof rect === "object" ? (rect as Record<string, number>) : null;
  const patch: Record<string, unknown> = {};
  if (r && Number.isFinite(r.width) && Number.isFinite(r.height)) {
    patch.highlight = { x: r.x, y: r.y, width: r.width, height: r.height, seq: ++vizSeq };
  }
  if (Number.isFinite(x) && Number.isFinite(y)) patch.cursor = { x: x as number, y: y as number };
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

/** 动作后的「稳定」判定：先看是否发生跳转（跳转则等加载结束），再轮询页面最近一次 DOM 变更距今多久，
 *  静默 120 ms 即返回。轮询在宿主侧做，不依赖网页自己的计时器（网页计时器被节流时等待会被拉长到秒级）。 */
async function settleAfter(sid: string, tabId: string, rec: FrameRecord, since: number, settleMs: number) {
  if (!settleMs) return { navigated: rec.navSeq !== since };
  const navigated = await waitNavigationSettled(sid, tabId, since, Math.max(settleMs, 12000));
  const budget = navigated ? 800 : settleMs;
  const t0 = performance.now();
  let st: Record<string, any> = {};
  try {
    for (;;) {
      st = await evalKit(rec, "state", {}, 4000);
      if (Number(st.idle_ms ?? 1e9) >= 120 || performance.now() - t0 >= budget) break;
      await sleep(40);
    }
  } catch { /* 跳转中或页面忙：只回报导航结论 */ }
  return { ...st, navigated };
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

async function dispatchMouse(el: any, a: Record<string, any>) {
  const x = Math.round(Number(a.x) || 0);
  const y = Math.round(Number(a.y) || 0);
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
    const tx = Math.round(Number(a.to_x) || x);
    const ty = Math.round(Number(a.to_y) || y);
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

async function navigateTab(sid: string, tab: BrowserTabState, a: Record<string, any>) {
  const action = a.action as string | undefined;
  const timeout = Math.max(3000, Math.min(Number(a.timeout_ms) || 30000, 60000));
  if (action === "back" || action === "forward" || action === "reload") {
    const rec = await ensureFrame(sid, tab);
    const since = rec.navSeq;
    if (action === "back") { if (!rec.el.canGoBack()) throw new Error("没有可后退的页面"); rec.el.goBack(); }
    else if (action === "forward") { if (!rec.el.canGoForward()) throw new Error("没有可前进的页面"); rec.el.goForward(); }
    else rec.el.reload();
    await waitNavigationSettled(sid, tab.id, since, timeout);
    return { rec, status: undefined };
  }
  const url = normalizeUrl(String(a.url || ""));
  if (!url) throw new Error("url 不能为空");
  ensurePanelTab(sid);
  const store = useBrowserStore.getState();
  const rec0 = getFrame(sid, tab.id);
  if (rec0?.ready && typeof rec0.el?.loadURL === "function") {
    store.updateTab(sid, tab.id, { url, loading: true, error: undefined });
    try {
      await withTimeout(Promise.resolve(rec0.el.loadURL(url)), timeout, "__TIMEOUT__");
    } catch (e) {
      const m = errText(e);
      if (m !== "__TIMEOUT__") throw new Error(`页面加载失败：${loadErrorText(m, url)}`);
    }
    return { rec: rec0, status: undefined };
  }
  store.navigate(sid, url, tab.id);
  const rec = await waitFrameReady(sid, tab.id, 15000);
  if (!rec) throw new Error("浏览器标签未能就绪，请稍后重试");
  const t0 = performance.now();
  while (performance.now() - t0 < timeout) {
    const t = tabsOf(sid).find((x) => x.id === tab.id);
    if (t?.error) throw new Error(`页面加载失败：${loadErrorText(t.error, url)}`);
    if (!rec.loading && !t?.loading) break;
    await sleep(60);
  }
  return { rec, status: undefined };
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
    // AI 动作窗口：期间网页弹窗按 AI 策略应答（不弹系统对话框、不卡页面）
    try {
      await mainExec(rec, "aiWindow", { ms: settleMs + 4000, accept: a.on_dialog !== "dismiss", prompt_text: a.prompt_text || "" });
    } catch { /* 主进程能力缺失时降级 */ }
    try { await evalKit(rec, "watch", {}, 4000); } catch { /* 页面在跳转：settle 时只看导航 */ }
    const since = rec.navSeq;
    if (op === "mouse") {
      if (a.action !== "wheel") await politeness();
      if (visible) {
        showTarget(sid, a.rect, Number(a.x), Number(a.y));
        if (a.action !== "wheel") await sleep(150); // 光标先到位再点击，让用户看清 AI 要点哪里
      }
      const release = guardFocus(rec.el, hostFocus());
      try {
        // activate：目标被遮挡时直接在元素上派发点击（不经过坐标命中，遮挡物拦不住）
        if (a.action === "activate") await evalKit(rec, "activate", { ref: a.ref }, 8000);
        else await dispatchMouse(rec.el, a);
      } finally {
        await sleep(30);
        release();
      }
      if (visible && ((a.action || "click") === "click" || a.action === "activate")) {
        store.patchAgent(sid, { ripple: { x: Number(a.x), y: Number(a.y), seq: ++vizSeq } });
      }
    } else if (op === "keys") {
      if (visible && a.rect) showTarget(sid, a.rect);
      const keys: string[] = Array.isArray(a.keys) ? a.keys.map(String) : [];
      const repeat = Math.max(1, Math.min(Number(a.repeat) || 1, 50));
      for (let r = 0; r < repeat; r++) {
        for (const combo of keys) {
          sendKeyCombo(rec.el, combo);
          if (keys.length > 1 || repeat > 1) await sleep(12);
        }
      }
    } else {
      if (visible && a.rect) showTarget(sid, a.rect);
      await Promise.resolve(rec.el.insertText(String(a.text ?? "")));
    }
    const settle = await settleAfter(sid, tab.id, rec, since, settleMs);
    return { ...(await baseOf(sid, tab.id, rec)), settle };
  }

  if (op === "capture") {
    begin(sid, tab.id, label || "截图", true);
    const res = await mainExec(rec, "capture", a);
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
      capabilities: ["tabs", "nav", "kit", "eval", "mouse", "keys", "text", "capture", "logs", "network", "dialogs"],
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
