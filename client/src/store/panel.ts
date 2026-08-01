/** 右侧面板状态（v5）：expanded/width/tabs + 全屏模式 + 最大宽度限制。 */
import { create } from "zustand";

export type PanelTabId = "task-summary" | "browser" | "terminal" | "files";
export interface PanelTab { id: PanelTabId; instance: number; }

const STORAGE_KEY = "chatcoder.panel";
const DEFAULT_WIDTH = 420;
const MAX_WIDTH = 1200;

function loadState(): { width: number } {
  if (typeof window === "undefined") return { width: DEFAULT_WIDTH };
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return { width: DEFAULT_WIDTH };
    return { width: JSON.parse(raw).width ?? DEFAULT_WIDTH };
  } catch { return { width: DEFAULT_WIDTH }; }
}
function saveWidth(width: number) { try { localStorage.setItem(STORAGE_KEY, JSON.stringify({ width })); } catch {} }
function tabKey(t: PanelTab) { return `${t.id}-${t.instance}`; }

interface PanelState {
  expanded: boolean;
  width: number;
  fullscreen: boolean;
  tabs: PanelTab[];
  activeKey: string | null;
  previewPath: string | null;
  openPanel: () => void;
  closePanel: () => void;
  togglePanel: () => void;
  toggleFullscreen: () => void;
  setWidth: (w: number) => void;
  openTab: (id: PanelTabId) => void;
  closeTab: (key: string) => void;
  setActiveTab: (key: string) => void;
  setPreviewPath: (path: string | null) => void;
  reset: () => void;
}

const initial = loadState();
export const usePanelStore = create<PanelState>((set, get) => ({
  expanded: false,
  width: initial.width,
  fullscreen: false,
  tabs: [],
  activeKey: null,
  previewPath: null,
  openPanel: () => set({ expanded: true }),
  closePanel: () => set({ expanded: false, fullscreen: false }),
  togglePanel: () => set((s) => ({ expanded: !s.expanded })),
  toggleFullscreen: () => set((s) => ({ fullscreen: !s.fullscreen })),
  setWidth: (w) => {
    const clamped = Math.max(200, Math.min(MAX_WIDTH, Math.round(w)));
    set({ width: clamped });
    saveWidth(clamped);
  },
  openTab: (id) => {
    const { tabs } = get();
    const existing = tabs.find((t) => t.id === id);
    if (existing) { set({ activeKey: tabKey(existing) }); return; }
    const tab: PanelTab = { id, instance: tabs.filter((t) => t.id === id).length + 1 };
    set({ tabs: [...tabs, tab], activeKey: tabKey(tab) });
  },
  closeTab: (key) => {
    const { tabs, activeKey } = get();
    const next = tabs.filter((t) => tabKey(t) !== key);
    let nextActive = activeKey;
    if (activeKey === key) {
      const closedIdx = tabs.findIndex((t) => tabKey(t) === key);
      const fallback = next[Math.min(closedIdx, next.length - 1)];
      nextActive = fallback ? tabKey(fallback) : null;
    }
    set({ tabs: next, activeKey: nextActive });
  },
  setActiveTab: (key) => set({ activeKey: key }),
  setPreviewPath: (path) => set({ previewPath: path }),
  reset: () => set({ expanded: false, fullscreen: false, tabs: [], activeKey: null }),
}));
