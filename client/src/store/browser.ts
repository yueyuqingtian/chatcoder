/**
 * 会话级浏览器状态管理（BrowserStore）
 *
 * 核心目标：每个会话（Session）拥有各自完全独立的浏览器实例状态：
 * 1. 独立 URL 输入与当前加载页面 (url, current)
 * 2. 独立导航历史记录与指针 (history, hIdx)
 * 3. 独立视口模式 (responsive / desktop / tablet / mobile)
 * 4. 独立视图切换 (preview / dom / console)
 * 5. 独立 DOM 快照、控制台日志、元素标注与调试求值缓存
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
}

export interface AnnotState {
  x: number;
  y: number;
  source: string;
  info?: ElementInfo;
}

export interface SessionBrowserState {
  url: string;
  current: string;
  history: string[];
  hIdx: number;
  tabView: BrowserTabView;
  viewportMode: ViewportMode;
  domSnapshot: string;
  evalCode: string;
  evalResult: string;
  selecting: boolean;
  annotState: AnnotState | null;
  annotText: string;
}

export const DEFAULT_SESSION_BROWSER_STATE: SessionBrowserState = {
  url: "http://127.0.0.1:5173",
  current: "http://127.0.0.1:5173",
  history: ["http://127.0.0.1:5173"],
  hIdx: 0,
  tabView: "preview",
  viewportMode: "responsive",
  domSnapshot: "",
  evalCode: "document.title",
  evalResult: "",
  selecting: false,
  annotState: null,
  annotText: "",
};

interface BrowserStore {
  sessions: Record<string, SessionBrowserState>;
  getSessionState: (sessionId: string | number | null | undefined) => SessionBrowserState;
  updateSessionState: (
    sessionId: string | number | null | undefined,
    patch: Partial<SessionBrowserState> | ((prev: SessionBrowserState) => Partial<SessionBrowserState>)
  ) => void;
  navigate: (sessionId: string | number | null | undefined, targetUrl: string) => void;
  goBack: (sessionId: string | number | null | undefined) => void;
  goForward: (sessionId: string | number | null | undefined) => void;
  resetSession: (sessionId: string | number | null | undefined) => void;
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
    if (existing) return existing;
    return { ...DEFAULT_SESSION_BROWSER_STATE };
  },

  updateSessionState: (sid, patch) => {
    const key = normalizeSessionId(sid);
    set((state) => {
      const current = state.sessions[key] || { ...DEFAULT_SESSION_BROWSER_STATE };
      const nextPatch = typeof patch === "function" ? patch(current) : patch;
      return {
        sessions: {
          ...state.sessions,
          [key]: { ...current, ...nextPatch },
        },
      };
    });
  },

  navigate: (sid, target) => {
    const raw = (target || "").trim();
    if (!raw) return;
    const formatted = /^https?:\/\//i.test(raw) ? raw : `http://${raw}`;
    const key = normalizeSessionId(sid);

    set((state) => {
      const cur = state.sessions[key] || { ...DEFAULT_SESSION_BROWSER_STATE };
      const newHistory = cur.history.slice(0, cur.hIdx + 1);
      newHistory.push(formatted);
      return {
        sessions: {
          ...state.sessions,
          [key]: {
            ...cur,
            url: formatted,
            current: formatted,
            history: newHistory,
            hIdx: newHistory.length - 1,
            selecting: false,
            annotState: null,
          },
        },
      };
    });
  },

  goBack: (sid) => {
    const key = normalizeSessionId(sid);
    set((state) => {
      const cur = state.sessions[key] || { ...DEFAULT_SESSION_BROWSER_STATE };
      if (cur.hIdx <= 0) return state;
      const nextIdx = cur.hIdx - 1;
      const prevUrl = cur.history[nextIdx];
      return {
        sessions: {
          ...state.sessions,
          [key]: {
            ...cur,
            hIdx: nextIdx,
            url: prevUrl,
            current: prevUrl,
          },
        },
      };
    });
  },

  goForward: (sid) => {
    const key = normalizeSessionId(sid);
    set((state) => {
      const cur = state.sessions[key] || { ...DEFAULT_SESSION_BROWSER_STATE };
      if (cur.hIdx >= cur.history.length - 1) return state;
      const nextIdx = cur.hIdx + 1;
      const nextUrl = cur.history[nextIdx];
      return {
        sessions: {
          ...state.sessions,
          [key]: {
            ...cur,
            hIdx: nextIdx,
            url: nextUrl,
            current: nextUrl,
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
        [key]: { ...DEFAULT_SESSION_BROWSER_STATE },
      },
    }));
  },
}));
