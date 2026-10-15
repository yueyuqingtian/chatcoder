/**
 * 会话级浏览器状态（plan-20261015-browser-agent 重构）。
 *
 * 1. 每个会话拥有独立的多标签浏览器；标签区分归属（用户 / AI），AI 默认只在自己的标签里工作；
 * 2. 标签的地址 / 标题 / 图标 / 加载态 / 前进后退能力以 webview 导航事件为准（此前只记“想去哪”，
 *    页内点链接后地址栏不更新）；导航一律走 webview 命令式 API——`src` 只在首次挂载时使用，
 *    改 src 会让 webview 二次加载；
 * 3. AI 活动态（当前动作文案、步数、光标、高亮、点击波纹、截图闪白）供面板可视化；
 * 4. 网页弹窗（AI 未在操作时由用户处理）以应用内对话框呈现。
 */
import { create } from "zustand";
import { getFrame } from "../browserAgent/frames";

export type BrowserTabView = "preview" | "dom" | "console";
export type TabOwner = "user" | "ai";

export interface ElementInfo {
  tag: string;
  id: string;
  className: string;
  width: number;
  height: number;
  x: number;
  y: number;
  display: string;
  position: string;
  color: string;
  backgroundColor: string;
  fontSize: string;
  padding: string;
  margin: string;
  text: string;
  selector?: string;
  htmlSnippet?: string;
  role?: string;
  ariaLabel?: string;
  placeholder?: string;
}

export interface BrowserTabState {
  id: string;
  /** 首次挂载 webview 时加载的地址（之后不再改动，避免二次加载） */
  src: string;
  /** 实时地址（webview 导航事件回填；iframe 模式等于最近一次导航目标） */
  url: string;
  title: string;
  favicon?: string;
  loading: boolean;
  canGoBack: boolean;
  canGoForward: boolean;
  owner: TabOwner;
  /** iframe 模式（浏览器预览环境）没有 guest 历史，保留自管历史栈 */
  history: string[];
  hIdx: number;
  /** 加载失败 / 渲染进程崩溃等提示（下一次成功导航清除） */
  error?: string;
}

export interface AgentActivity {
  /** idle：无活动；working：AI 正在操作；done：刚结束（短暂展示「已完成 N 步」） */
  phase: "idle" | "working" | "done";
  tabId: string | null;
  label: string;
  steps: number;
  startedAt: number;
  updatedAt: number;
  /** AI 光标位置（页面 CSS 像素） */
  cursor: { x: number; y: number } | null;
  /** 当前目标元素矩形 */
  highlight: { x: number; y: number; width: number; height: number; seq: number } | null;
  /** 点击波纹（seq 变化即播放一次） */
  ripple: { x: number; y: number; seq: number } | null;
  /** 截图闪白（序号变化即播放一次） */
  flash: number;
}

export interface PageDialog {
  tabId: string;
  wcId: number;
  type: string;
  message: string;
  defaultPrompt: string;
}

export interface SessionBrowserState {
  tabs: BrowserTabState[];
  activeTabId: string;
  tabView: BrowserTabView;
  domSnapshot: string;
  selecting: boolean;
  agent: AgentActivity;
  dialog: PageDialog | null;
  /** AI 正在另一个标签里工作（用户在看自己的标签，不强行切换）时的提示 */
  agentElsewhere: string | null;
  /** 用户最近一次与浏览器面板交互的时间（AI 据此决定要不要切换显示标签） */
  userActiveAt: number;
}

export const IDLE_AGENT: AgentActivity = {
  phase: "idle", tabId: null, label: "", steps: 0, startedAt: 0, updatedAt: 0,
  cursor: null, highlight: null, ripple: null, flash: 0,
};

let tabSeq = 0;
function makeTab(initialUrl = "", owner: TabOwner = "user"): BrowserTabState {
  return {
    id: `tab-${Date.now().toString(36)}-${(++tabSeq).toString(36)}`,
    src: initialUrl,
    url: initialUrl,
    title: initialUrl ? initialUrl.replace(/^https?:\/\//i, "") : "",
    loading: Boolean(initialUrl),
    canGoBack: false,
    canGoForward: false,
    owner,
    history: initialUrl ? [initialUrl] : [],
    hIdx: initialUrl ? 0 : -1,
  };
}

export const createInitialSessionState = (): SessionBrowserState => {
  const tab = makeTab();
  return {
    tabs: [tab],
    activeTabId: tab.id,
    tabView: "preview",
    domSnapshot: "",
    selecting: false,
    agent: IDLE_AGENT,
    dialog: null,
    agentElsewhere: null,
    userActiveAt: 0,
  };
};

// 模块级稳定默认实例：会话缺失时复用同一引用，避免选择器反复返回新对象导致重渲染
const DEFAULT_SESSION_STATE = createInitialSessionState();

/** 与服务端 driver.normalize_url 同口径：本地地址补 http://，其余补 https://。 */
export function normalizeUrl(raw: string): string {
  const u = (raw || "").trim();
  if (!u) return "";
  if (/^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(u) && !/^[\w.-]+:\d+/.test(u)) return u;
  const host = u.split("/")[0].split(":")[0].toLowerCase();
  if (host === "localhost" || host.endsWith(".localhost") || /^\d+\.\d+\.\d+\.\d+$/.test(host) || host === "[::1]") return `http://${u}`;
  return `https://${u}`;
}

export function sessionKey(sid: string | number | null | undefined): string {
  if (sid === null || sid === undefined || sid === "") return "default";
  return String(sid);
}

interface BrowserStore {
  sessions: Record<string, SessionBrowserState>;
  getSessionState: (sid: string | number | null | undefined) => SessionBrowserState;
  /** 确保会话状态已创建并返回它（写入前调用：getSessionState 对缺失会话返回共享默认对象，
   *  拿它的标签 id 去写会落空） */
  ensureSession: (sid: string | number | null | undefined) => SessionBrowserState;
  updateSessionState: (
    sid: string | number | null | undefined,
    patch: Partial<SessionBrowserState> | ((prev: SessionBrowserState) => Partial<SessionBrowserState>),
  ) => void;
  newTab: (sid: string | number | null | undefined, initialUrl?: string, opts?: { owner?: TabOwner; activate?: boolean }) => string;
  closeTab: (sid: string | number | null | undefined, tabId: string) => void;
  setActiveTab: (sid: string | number | null | undefined, tabId: string) => void;
  updateTab: (
    sid: string | number | null | undefined,
    tabId: string,
    patch: Partial<BrowserTabState> | ((prev: BrowserTabState) => Partial<BrowserTabState>),
  ) => void;
  /** 导航（地址栏 / 起始页 / AI）：webview 已就绪时命令式 loadURL，否则改 src 等挂载后加载 */
  navigate: (sid: string | number | null | undefined, targetUrl: string, tabId?: string) => void;
  goBack: (sid: string | number | null | undefined, tabId?: string) => void;
  goForward: (sid: string | number | null | undefined, tabId?: string) => void;
  reload: (sid: string | number | null | undefined, tabId?: string) => void;
  stop: (sid: string | number | null | undefined, tabId?: string) => void;
  patchAgent: (sid: string | number | null | undefined, patch: Partial<AgentActivity>) => void;
  setDialog: (sid: string | number | null | undefined, dialog: PageDialog | null) => void;
  noteUserActive: (sid: string | number | null | undefined) => void;
  resetSession: (sid: string | number | null | undefined) => void;
}

/** 已就绪的 webview 元素（iframe / 未挂载时为 null）。 */
function liveWebview(key: string, tabId: string): any | null {
  const f = getFrame(key, tabId);
  if (!f || !f.ready || !f.el || typeof f.el.loadURL !== "function") return null;
  return f.el;
}

export const useBrowserStore = create<BrowserStore>((set, get) => {
  const writeSession = (sid: string | number | null | undefined, fn: (cur: SessionBrowserState) => Partial<SessionBrowserState>) => {
    const key = sessionKey(sid);
    set((state) => {
      const cur = state.sessions[key] || createInitialSessionState();
      return { sessions: { ...state.sessions, [key]: { ...cur, ...fn(cur) } } };
    });
  };
  const resolveTabId = (sid: string | number | null | undefined, tabId?: string) =>
    tabId || get().getSessionState(sid).activeTabId;

  return {
    sessions: {},

    getSessionState: (sid) => {
      const existing = get().sessions[sessionKey(sid)];
      if (existing && existing.tabs && existing.tabs.length > 0) return existing;
      return DEFAULT_SESSION_STATE;
    },

    ensureSession: (sid) => {
      const key = sessionKey(sid);
      if (!get().sessions[key]) set((state) => (state.sessions[key] ? {} : { sessions: { ...state.sessions, [key]: createInitialSessionState() } }));
      return get().sessions[key];
    },

    updateSessionState: (sid, patch) => writeSession(sid, (cur) => (typeof patch === "function" ? patch(cur) : patch)),

    newTab: (sid, initialUrl = "", opts) => {
      const tab = makeTab(initialUrl ? normalizeUrl(initialUrl) : "", opts?.owner ?? "user");
      writeSession(sid, (cur) => ({
        tabs: [...cur.tabs, tab],
        ...(opts?.activate === false ? {} : { activeTabId: tab.id, selecting: false }),
      }));
      return tab.id;
    },

    closeTab: (sid, tabId) => writeSession(sid, (cur) => {
      const next = cur.tabs.filter((t) => t.id !== tabId);
      if (next.length === 0) {
        const fallback = makeTab();
        return { tabs: [fallback], activeTabId: fallback.id, agentElsewhere: null };
      }
      let active = cur.activeTabId;
      if (active === tabId) {
        const idx = cur.tabs.findIndex((t) => t.id === tabId);
        active = next[Math.min(idx, next.length - 1)].id;
      }
      return { tabs: next, activeTabId: active, agentElsewhere: cur.agentElsewhere === tabId ? null : cur.agentElsewhere };
    }),

    setActiveTab: (sid, tabId) => writeSession(sid, (cur) => ({
      activeTabId: tabId, selecting: false,
      agentElsewhere: cur.agentElsewhere === tabId ? null : cur.agentElsewhere,
    })),

    updateTab: (sid, tabId, patch) => writeSession(sid, (cur) => ({
      tabs: cur.tabs.map((t) => (t.id === tabId ? { ...t, ...(typeof patch === "function" ? patch(t) : patch) } : t)),
    })),

    navigate: (sid, target, tabId) => {
      const url = normalizeUrl(target);
      if (!url) return;
      const id = resolveTabId(sid, tabId);
      const wv = liveWebview(sessionKey(sid), id);
      if (wv) {
        // 已挂载的 webview：命令式导航，地址/标题由导航事件回填
        get().updateTab(sid, id, { url, loading: true, error: undefined });
        try { void Promise.resolve(wv.loadURL(url)).catch(() => {}); } catch { /* 宿主未就绪：忽略 */ }
        return;
      }
      get().updateTab(sid, id, (t) => {
        const history = t.history.slice(0, t.hIdx + 1);
        history.push(url);
        return {
          src: url, url, title: url.replace(/^https?:\/\//i, ""), loading: true, error: undefined,
          history, hIdx: history.length - 1, canGoBack: history.length > 1, canGoForward: false,
        };
      });
    },

    goBack: (sid, tabId) => {
      const id = resolveTabId(sid, tabId);
      const wv = liveWebview(sessionKey(sid), id);
      if (wv) { try { if (wv.canGoBack()) wv.goBack(); } catch { /* ignore */ } return; }
      get().updateTab(sid, id, (t) => {
        if (t.hIdx <= 0) return {};
        const hIdx = t.hIdx - 1;
        const url = t.history[hIdx];
        return { hIdx, src: url, url, canGoBack: hIdx > 0, canGoForward: true };
      });
    },

    goForward: (sid, tabId) => {
      const id = resolveTabId(sid, tabId);
      const wv = liveWebview(sessionKey(sid), id);
      if (wv) { try { if (wv.canGoForward()) wv.goForward(); } catch { /* ignore */ } return; }
      get().updateTab(sid, id, (t) => {
        if (t.hIdx >= t.history.length - 1) return {};
        const hIdx = t.hIdx + 1;
        const url = t.history[hIdx];
        return { hIdx, src: url, url, canGoBack: true, canGoForward: hIdx < t.history.length - 1 };
      });
    },

    reload: (sid, tabId) => {
      const id = resolveTabId(sid, tabId);
      const wv = liveWebview(sessionKey(sid), id);
      if (wv) { try { wv.reload(); } catch { /* ignore */ } return; }
      const f = getFrame(sessionKey(sid), id);
      if (f?.el && f.el.tagName === "IFRAME") {
        try { f.el.src = f.el.src; } catch { /* 跨域时忽略 */ }
      }
    },

    stop: (sid, tabId) => {
      const id = resolveTabId(sid, tabId);
      const wv = liveWebview(sessionKey(sid), id);
      if (wv) { try { wv.stop(); } catch { /* ignore */ } }
      get().updateTab(sid, id, { loading: false });
    },

    patchAgent: (sid, patch) => writeSession(sid, (cur) => ({ agent: { ...cur.agent, ...patch } })),

    setDialog: (sid, dialog) => writeSession(sid, () => ({ dialog })),

    noteUserActive: (sid) => {
      const key = sessionKey(sid);
      const cur = get().sessions[key];
      // 高频调用（鼠标移动）只在跨过 1 秒时写一次，避免无谓重渲染
      if (cur && Date.now() - cur.userActiveAt < 1000) return;
      writeSession(sid, () => ({ userActiveAt: Date.now() }));
    },

    resetSession: (sid) => set((state) => ({ sessions: { ...state.sessions, [sessionKey(sid)]: createInitialSessionState() } })),
  };
});
