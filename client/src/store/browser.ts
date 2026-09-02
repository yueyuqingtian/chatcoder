/**
 * 会话级浏览器状态管理（BrowserStore）
 *
 * 核心目标：
 * 1. 每个会话（Session）拥有各自完全独立的浏览器多标签实例状态；
 * 2. 默认空起始页（url=""），不加载任何外部页面；
 * 3. 支持多个网页标签（BrowserTabState），标签独立历史、URL、标题与状态；
 * 4. 视口模式、视图模式、DOM快照与控制台调试缓存；
 * 5. 响应服务端 browser.mirror 镜像事件（AI 自动打开/导航/点击/输入可视化）。
 */
import { create } from "zustand";

export type ViewportMode = "responsive" | "desktop" | "tablet" | "mobile";
export type BrowserTabView = "preview" | "dom" | "console";

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

export interface AnnotState {
  x: number;
  y: number;
  source: string;
  info?: ElementInfo;
  screenshotBase64?: string;
}

export interface BrowserTabState {
  id: string;
  url: string;
  current: string;
  title: string;
  favicon?: string;
  loading: boolean;
  history: string[];
  hIdx: number;
}

export interface SessionBrowserState {
  tabs: BrowserTabState[];
  activeTabId: string;
  tabView: BrowserTabView;
  viewportMode: ViewportMode;
  domSnapshot: string;
  evalCode: string;
  evalResult: string;
  selecting: boolean;
  annotState: AnnotState | null;
  annotText: string;
  // AI 镜像事件状态（用于向用户展示 AI 当前操作反馈）
  mirrorActivity: {
    action: "navigate" | "click" | "type" | "screenshot" | "snapshot" | "evaluate" | "none";
    text?: string;
    selector?: string;
    timestamp: number;
  } | null;
}

function createDefaultTab(id?: string, initialUrl = ""): BrowserTabState {
  const tabId = id || `tab-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
  return {
    id: tabId,
    url: initialUrl,
    current: initialUrl,
    title: initialUrl ? initialUrl.replace(/^https?:\/\//i, "") : "新标签页",
    loading: false,
    history: initialUrl ? [initialUrl] : [],
    hIdx: initialUrl ? 0 : -1,
  };
}

export const createInitialSessionState = (): SessionBrowserState => {
  const initialTab = createDefaultTab();
  return {
    tabs: [initialTab],
    activeTabId: initialTab.id,
    tabView: "preview",
    viewportMode: "responsive",
    domSnapshot: "",
    evalCode: "document.title",
    evalResult: "",
    selecting: false,
    annotState: null,
    annotText: "",
    mirrorActivity: null,
  };
};

// 模块级稳定默认会话实例：getSessionState 在会话缺失时复用该引用，
// 避免每次返回新对象导致 useBrowserStore 选择器反复触发组件重渲染。
const DEFAULT_SESSION_STATE = createInitialSessionState();

interface BrowserStore {
  sessions: Record<string, SessionBrowserState>;
  getSessionState: (sessionId: string | number | null | undefined) => SessionBrowserState;
  updateSessionState: (
    sessionId: string | number | null | undefined,
    patch: Partial<SessionBrowserState> | ((prev: SessionBrowserState) => Partial<SessionBrowserState>)
  ) => void;
  // 多标签操作
  newTab: (sessionId: string | number | null | undefined, initialUrl?: string) => string;
  closeTab: (sessionId: string | number | null | undefined, tabId: string) => void;
  setActiveTab: (sessionId: string | number | null | undefined, tabId: string) => void;
  updateTab: (
    sessionId: string | number | null | undefined,
    tabId: string,
    patch: Partial<BrowserTabState> | ((prev: BrowserTabState) => Partial<BrowserTabState>)
  ) => void;
  // 导航操作（作用于活动标签）
  navigate: (sessionId: string | number | null | undefined, targetUrl: string, tabId?: string) => void;
  goBack: (sessionId: string | number | null | undefined, tabId?: string) => void;
  goForward: (sessionId: string | number | null | undefined, tabId?: string) => void;
  resetSession: (sessionId: string | number | null | undefined) => void;
  // AI 镜像事件响应
  applyMirror: (sessionId: string | number | null | undefined, payload: {
    action: string;
    url?: string;
    selector?: string;
    text?: string;
    ok?: boolean;
    summary?: string;
  }) => void;
}

function normalizeSessionId(sid: string | number | null | undefined): string {
  if (sid === null || sid === undefined || sid === "") return "default";
  return String(sid);
}

export const useBrowserStore = create<BrowserStore>((set, get) => ({
  sessions: {},

  getSessionState: (sid) => {
    const key = normalizeSessionId(sid);
    const existing = get().sessions[key];
    if (existing && existing.tabs && existing.tabs.length > 0) return existing;
    return DEFAULT_SESSION_STATE;
  },

  updateSessionState: (sid, patch) => {
    const key = normalizeSessionId(sid);
    set((state) => {
      const current = state.sessions[key] || createInitialSessionState();
      const nextPatch = typeof patch === "function" ? patch(current) : patch;
      return {
        sessions: {
          ...state.sessions,
          [key]: { ...current, ...nextPatch },
        },
      };
    });
  },

  newTab: (sid, initialUrl = "") => {
    const key = normalizeSessionId(sid);
    const newTabObj = createDefaultTab(undefined, initialUrl);
    set((state) => {
      const current = state.sessions[key] || createInitialSessionState();
      return {
        sessions: {
          ...state.sessions,
          [key]: {
            ...current,
            tabs: [...current.tabs, newTabObj],
            activeTabId: newTabObj.id,
            selecting: false,
            annotState: null,
          },
        },
      };
    });
    return newTabObj.id;
  },

  closeTab: (sid, tabId) => {
    const key = normalizeSessionId(sid);
    set((state) => {
      const current = state.sessions[key] || createInitialSessionState();
      const nextTabs = current.tabs.filter((t) => t.id !== tabId);
      if (nextTabs.length === 0) {
        const fallback = createDefaultTab();
        return {
          sessions: {
            ...state.sessions,
            [key]: {
              ...current,
              tabs: [fallback],
              activeTabId: fallback.id,
              annotState: null,
            },
          },
        };
      }
      let nextActiveId = current.activeTabId;
      if (current.activeTabId === tabId) {
        const closedIdx = current.tabs.findIndex((t) => t.id === tabId);
        const fallback = nextTabs[Math.min(closedIdx, nextTabs.length - 1)];
        nextActiveId = fallback ? fallback.id : nextTabs[0].id;
      }
      return {
        sessions: {
          ...state.sessions,
          [key]: {
            ...current,
            tabs: nextTabs,
            activeTabId: nextActiveId,
            annotState: null,
          },
        },
      };
    });
  },

  setActiveTab: (sid, tabId) => {
    const key = normalizeSessionId(sid);
    set((state) => {
      const current = state.sessions[key] || createInitialSessionState();
      return {
        sessions: {
          ...state.sessions,
          [key]: {
            ...current,
            activeTabId: tabId,
            selecting: false,
            annotState: null,
          },
        },
      };
    });
  },

  updateTab: (sid, tabId, patch) => {
    const key = normalizeSessionId(sid);
    set((state) => {
      const current = state.sessions[key] || createInitialSessionState();
      const nextTabs = current.tabs.map((t) => {
        if (t.id !== tabId) return t;
        const next = typeof patch === "function" ? patch(t) : patch;
        return { ...t, ...next };
      });
      return {
        sessions: {
          ...state.sessions,
          [key]: { ...current, tabs: nextTabs },
        },
      };
    });
  },

  navigate: (sid, target, tabId) => {
    const raw = (target || "").trim();
    if (!raw) return;
    const formatted = /^https?:\/\//i.test(raw) || /^file:\/\//i.test(raw) || /^about:/i.test(raw)
      ? raw
      : `https://${raw}`;
    const key = normalizeSessionId(sid);

    set((state) => {
      const cur = state.sessions[key] || createInitialSessionState();
      const targetTabId = tabId || cur.activeTabId;
      const targetTab = cur.tabs.find((t) => t.id === targetTabId) || cur.tabs[0];
      if (!targetTab) return state;

      const newHistory = targetTab.history.slice(0, targetTab.hIdx + 1);
      newHistory.push(formatted);
      const updatedTab: BrowserTabState = {
        ...targetTab,
        url: formatted,
        current: formatted,
        title: formatted.replace(/^https?:\/\//i, ""),
        history: newHistory,
        hIdx: newHistory.length - 1,
      };

      return {
        sessions: {
          ...state.sessions,
          [key]: {
            ...cur,
            tabs: cur.tabs.map((t) => (t.id === updatedTab.id ? updatedTab : t)),
            selecting: false,
            annotState: null,
          },
        },
      };
    });
  },

  goBack: (sid, tabId) => {
    const key = normalizeSessionId(sid);
    set((state) => {
      const cur = state.sessions[key] || createInitialSessionState();
      const targetTabId = tabId || cur.activeTabId;
      const targetTab = cur.tabs.find((t) => t.id === targetTabId);
      if (!targetTab || targetTab.hIdx <= 0) return state;

      const nextIdx = targetTab.hIdx - 1;
      const prevUrl = targetTab.history[nextIdx];
      const updatedTab: BrowserTabState = {
        ...targetTab,
        hIdx: nextIdx,
        url: prevUrl,
        current: prevUrl,
        title: prevUrl.replace(/^https?:\/\//i, ""),
      };

      return {
        sessions: {
          ...state.sessions,
          [key]: {
            ...cur,
            tabs: cur.tabs.map((t) => (t.id === updatedTab.id ? updatedTab : t)),
          },
        },
      };
    });
  },

  goForward: (sid, tabId) => {
    const key = normalizeSessionId(sid);
    set((state) => {
      const cur = state.sessions[key] || createInitialSessionState();
      const targetTabId = tabId || cur.activeTabId;
      const targetTab = cur.tabs.find((t) => t.id === targetTabId);
      if (!targetTab || targetTab.hIdx >= targetTab.history.length - 1) return state;

      const nextIdx = targetTab.hIdx + 1;
      const nextUrl = targetTab.history[nextIdx];
      const updatedTab: BrowserTabState = {
        ...targetTab,
        hIdx: nextIdx,
        url: nextUrl,
        current: nextUrl,
        title: nextUrl.replace(/^https?:\/\//i, ""),
      };

      return {
        sessions: {
          ...state.sessions,
          [key]: {
            ...cur,
            tabs: cur.tabs.map((t) => (t.id === updatedTab.id ? updatedTab : t)),
          },
        },
      };
    });
  },

  resetSession: (sid) => {
    const key = normalizeSessionId(sid);
    set((state) => ({
      sessions: {
        ...state.sessions,
        [key]: createInitialSessionState(),
      },
    }));
  },

  applyMirror: (sid, payload) => {
    const key = normalizeSessionId(sid);
    const action = (payload.action || "none") as any;
    set((state) => {
      const cur = state.sessions[key] || createInitialSessionState();
      let nextTabs = cur.tabs;
      let nextActiveId = cur.activeTabId;

      if (action === "navigate" && payload.url) {
        const url = payload.url.trim();
        const activeTab = cur.tabs.find((t) => t.id === cur.activeTabId);
        if (activeTab && (!activeTab.current || activeTab.current === "about:blank")) {
          // 当前活动标签为空，直接复用
          const newHist = [url];
          nextTabs = cur.tabs.map((t) =>
            t.id === activeTab.id
              ? { ...t, url, current: url, title: url.replace(/^https?:\/\//i, ""), history: newHist, hIdx: 0 }
              : t
          );
        } else {
          // 查找是否已打开相同 URL，有则激活，无则新开标签
          const existing = cur.tabs.find((t) => t.current === url);
          if (existing) {
            nextActiveId = existing.id;
          } else {
            const newTab = createDefaultTab(undefined, url);
            nextTabs = [...cur.tabs, newTab];
            nextActiveId = newTab.id;
          }
        }
      }

      return {
        sessions: {
          ...state.sessions,
          [key]: {
            ...cur,
            tabs: nextTabs,
            activeTabId: nextActiveId,
            mirrorActivity: {
              action,
              text: payload.text || payload.summary || "",
              selector: payload.selector,
              timestamp: Date.now(),
            },
          },
        },
      };
    });
  },
}));
