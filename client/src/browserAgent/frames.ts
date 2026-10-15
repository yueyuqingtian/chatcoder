/**
 * 内置浏览器 webview 注册表（plan-20261015-browser-agent）。
 *
 * BrowserPanel 负责渲染 webview；AI 宿主（host.ts）与面板工具栏都要拿到同一个元素去操作。
 * 这里按「会话桶 + 标签 id」登记元素、就绪状态、guest webContents id、控制台日志与导航序号，
 * 任何一方都不必持有 React 引用。
 *
 * 为什么记导航序号：AI 的点击/按键之后要判断「页面是否开始跳转」，只看 URL 不够
 * （同 URL 刷新、表单 POST 回到同一地址）。did-start-navigation（主框架）每次 +1。
 */

export interface ConsoleEntry {
  seq: number;
  level: string;
  text: string;
  source: string;
}

export interface FrameRecord {
  key: string;
  sid: string;
  tabId: string;
  el: any;
  wcId: number | null;
  ready: boolean;
  prepared: boolean;
  navSeq: number;
  /** 主框架 DOM 就绪次数：导航默认等到 DOM 就绪即返回，不等图片、字体等全部加载完 */
  domSeq: number;
  loading: boolean;
  console: ConsoleEntry[];
}

const CONSOLE_MAX = 300;
const frames = new Map<string, FrameRecord>();
const waiters = new Map<string, Array<(rec: FrameRecord | null) => void>>();
let consoleSeq = 0;

export const frameKey = (sid: string, tabId: string) => `${sid}::${tabId}`;

export function getFrame(sid: string, tabId: string): FrameRecord | undefined {
  return frames.get(frameKey(sid, tabId));
}

export function findFrameByWcId(wcId: number): FrameRecord | undefined {
  for (const f of frames.values()) if (f.wcId === wcId) return f;
  return undefined;
}

function wake(key: string, rec: FrameRecord | null) {
  const list = waiters.get(key);
  if (!list) return;
  waiters.delete(key);
  for (const fn of list) fn(rec);
}

/** 登记（或替换）某标签的宿主元素。元素替换时沿用控制台记录，就绪状态重置。 */
export function registerFrame(sid: string, tabId: string, el: any): FrameRecord {
  const key = frameKey(sid, tabId);
  const prev = frames.get(key);
  if (prev && prev.el === el) return prev;
  const rec: FrameRecord = {
    key, sid, tabId, el, wcId: null, ready: false, prepared: false,
    navSeq: prev?.navSeq ?? 0, domSeq: prev?.domSeq ?? 0, loading: false, console: prev?.console ?? [],
  };
  frames.set(key, rec);
  return rec;
}

export function unregisterFrame(sid: string, tabId: string, el: any) {
  const key = frameKey(sid, tabId);
  const rec = frames.get(key);
  if (!rec || rec.el !== el) return;
  frames.delete(key);
  wake(key, null);
}

/** guest 就绪（webview: dom-ready / iframe: load）：取 guest id，并交主进程做浏览器能力准备（一次）。 */
export function markFrameReady(sid: string, tabId: string, el: any) {
  const rec = frames.get(frameKey(sid, tabId));
  if (!rec || rec.el !== el) return;
  rec.ready = true;
  rec.domSeq++;
  if (typeof el.getWebContentsId === "function") {
    try { rec.wcId = el.getWebContentsId(); } catch { rec.wcId = null; }
  }
  if (rec.wcId && !rec.prepared && window.chatcoderAPI?.browserAgentPrepare) {
    rec.prepared = true;
    void window.chatcoderAPI.browserAgentPrepare(rec.wcId).catch(() => { rec.prepared = false; });
  }
  wake(rec.key, rec);
}

export function noteNavigationStart(sid: string, tabId: string) {
  const rec = frames.get(frameKey(sid, tabId));
  if (!rec) return;
  rec.navSeq++;
  rec.loading = true;
}

export function noteLoadingStop(sid: string, tabId: string) {
  const rec = frames.get(frameKey(sid, tabId));
  if (rec) rec.loading = false;
}

export function pushConsole(sid: string, tabId: string, level: string, text: string, source: string) {
  const rec = frames.get(frameKey(sid, tabId));
  if (!rec) return;
  rec.console.push({ seq: ++consoleSeq, level, text: text.slice(0, 2000), source: source.slice(0, 200) });
  if (rec.console.length > CONSOLE_MAX) rec.console.splice(0, rec.console.length - CONSOLE_MAX);
}

/** 宿主侧等待（plan-20261015-browser-perf）：应用窗口被遮挡/最小化时宿主页面被判为 hidden，
 *  window.setTimeout 会被节流到约 1 秒；改由主进程计时（IPC 回包不受节流），无桌面 API 时退回页面计时器。 */
export function hostSleep(ms: number): Promise<void> {
  const api = window.chatcoderAPI;
  if (ms > 0 && api?.browserAgentExec) {
    return api.browserAgentExec(0, "sleep", { ms }).then(() => undefined, () => new Promise<void>((r) => window.setTimeout(r, ms)));
  }
  return new Promise<void>((r) => window.setTimeout(r, ms));
}

/** 等待某标签的 webview 挂载并就绪；超时返回 null（调用方给出可读错误）。 */
export function waitFrameReady(sid: string, tabId: string, timeoutMs: number): Promise<FrameRecord | null> {
  const key = frameKey(sid, tabId);
  const rec = frames.get(key);
  if (rec?.ready) return Promise.resolve(rec);
  return new Promise((resolve) => {
    let done = false;
    const timer = window.setTimeout(() => {
      if (done) return;
      done = true;
      const list = waiters.get(key);
      if (list) waiters.set(key, list.filter((f) => f !== finish));
      resolve(frames.get(key)?.ready ? frames.get(key)! : null);
    }, timeoutMs);
    const finish = (r: FrameRecord | null) => {
      if (done) return;
      if (r && !r.ready) return;
      done = true;
      window.clearTimeout(timer);
      resolve(r);
    };
    const list = waiters.get(key) ?? [];
    list.push(finish);
    waiters.set(key, list);
  });
}

/** 等待「导航结束」：navSeq 自 since 起有变化且（DOM 就绪或加载结束），或超时。
 *  untilLoad=false 时 DOM 就绪即返回（不等图片等资源）；没有发生跳转时 graceMs 后即返回 false。 */
export async function waitNavigationSettled(
  sid: string, tabId: string, since: number, timeoutMs: number,
  opts: { untilLoad?: boolean; graceMs?: number; domSince?: number } = {},
): Promise<boolean> {
  const t0 = performance.now();
  const grace = opts.graceMs ?? 120;
  let navigated = false;
  while (performance.now() - t0 < timeoutMs) {
    const rec = frames.get(frameKey(sid, tabId));
    if (!rec) return navigated;
    if (rec.navSeq !== since) navigated = true;
    if (navigated && rec.ready) {
      if (!rec.loading) return true;
      if (!opts.untilLoad && opts.domSince !== undefined && rec.domSeq > opts.domSince) return true;
    }
    if (!navigated && performance.now() - t0 > grace) return false;
    await hostSleep(30);
  }
  return navigated;
}
