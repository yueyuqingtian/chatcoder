/** 右侧面板状态（v5）：expanded/width/tabs + 全屏模式 + 最大宽度限制。 */
import { create } from "zustand";

export type PanelTabId = "task-summary" | "browser" | "terminal" | "files" | "subagent";
export interface PanelTab {
  id: PanelTabId;
  instance: number;
  /** v2.2 (对齐 zcode 3.13): 子代理详情 tab 参数（threadId=agent_id） */
  meta?: { threadId?: number; agentName?: string };
}

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

/** v11: 变更审核 diff 预览（右面板「文件」标签页的 Monaco DiffEditor 数据源）。 */
export interface DiffPreview {
  path: string;
  before: string | null;
  after: string | null;
  truncated: boolean;
}

interface PanelState {
  expanded: boolean;
  width: number;
  fullscreen: boolean;
  /** v10: 主窗口顶部任务卡显隐开关（TitleBar 折叠按钮控制） */
  taskCardVisible: boolean;
  tabs: PanelTab[];
  activeKey: string | null;
  /** v2.2 (对齐 zcode 3.3.3): 最近关闭标签（上限 10），供加号菜单恢复 */
  closedStack: PanelTab[];
  previewPath: string | null;
  /** v2.2 (对齐 zcode 3.14.2): 预览文件定位行号（grep path:line 跳转，Monaco revealLine） */
  previewLine: number | null;
  /** v11: 变更审核 diff 预览（path 匹配 previewPath 时 FileTreePanel 展示 DiffEditor）。 */
  diffPreview: DiffPreview | null;
  openPanel: () => void;
  closePanel: () => void;
  togglePanel: () => void;
  toggleFullscreen: () => void;
  toggleTaskCard: () => void;
  setWidth: (w: number) => void;
  openTab: (id: PanelTabId) => void;
  /** v2.2 (对齐 zcode 3.15): 强制新开实例（终端多标签等场景，不去重） */
  openNewTab: (id: PanelTabId) => void;
  /** v2.2 (对齐 zcode 3.13): 打开子代理详情 tab（每线程独立实例，可多开） */
  openSubagent: (threadId: number, agentName?: string) => void;
  closeTab: (key: string) => void;
  /** v2.2 (对齐 zcode 3.3.3): 恢复最近关闭的标签（同时从栈中移除） */
  reopenClosedTab: (index: number) => void;
  setActiveTab: (key: string) => void;
  setPreviewPath: (path: string | null, line?: number | null) => void;
  setDiffPreview: (diff: DiffPreview | null) => void;
  reset: () => void;
}

const initial = loadState();
export const usePanelStore = create<PanelState>((set, get) => ({
  expanded: false,
  width: initial.width,
  fullscreen: false,
  taskCardVisible: false, // v13: 对齐 zcode——待办在消息流内嵌展示，底部浮窗默认关闭（标题栏可再开）
  tabs: [],
  activeKey: null,
  closedStack: [],
  previewPath: null,
  previewLine: null,
  diffPreview: null,
  openPanel: () => set({ expanded: true }),
  closePanel: () => set({ expanded: false, fullscreen: false, diffPreview: null }),
  togglePanel: () => set((s) => ({ expanded: !s.expanded })),
  toggleFullscreen: () => set((s) => ({ fullscreen: !s.fullscreen })),
  toggleTaskCard: () => set((s) => ({ taskCardVisible: !s.taskCardVisible })),
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
  openNewTab: (id) => {
    const { tabs } = get();
    const tab: PanelTab = { id, instance: tabs.filter((t) => t.id === id).length + 1 };
    set({ expanded: true, tabs: [...tabs, tab], activeKey: tabKey(tab) });
  },
  openSubagent: (threadId, agentName) => {
    const { tabs } = get();
    // 同线程已开则激活，否则新开实例
    const existing = tabs.find((t) => t.id === "subagent" && t.meta?.threadId === threadId);
    if (existing) { set({ expanded: true, activeKey: tabKey(existing) }); return; }
    const tab: PanelTab = {
      id: "subagent",
      instance: tabs.filter((t) => t.id === "subagent").length + 1,
      meta: { threadId, agentName },
    };
    set({ expanded: true, tabs: [...tabs, tab], activeKey: tabKey(tab) });
  },
  closeTab: (key) => {
    const { tabs, activeKey, closedStack } = get();
    const closed = tabs.find((t) => tabKey(t) === key);
    const next = tabs.filter((t) => tabKey(t) !== key);
    let nextActive = activeKey;
    if (activeKey === key) {
      const closedIdx = tabs.findIndex((t) => tabKey(t) === key);
      const fallback = next[Math.min(closedIdx, next.length - 1)];
      nextActive = fallback ? tabKey(fallback) : null;
    }
    // v2.2: 最近关闭入栈（上限 10）
    const stack = closed
      ? [closed, ...closedStack.filter((t) => tabKey(t) !== key)].slice(0, 10)
      : closedStack;
    set({ tabs: next, activeKey: nextActive, closedStack: stack });
  },
  reopenClosedTab: (index) => {
    const { closedStack, tabs } = get();
    const closed = closedStack[index];
    if (!closed) return;
    const stack = closedStack.filter((_, i) => i !== index);
    // 同 key 已存在则只激活
    if (tabs.some((t) => tabKey(t) === tabKey(closed))) {
      set({ closedStack: stack, activeKey: tabKey(closed) });
      return;
    }
    set({ expanded: true, tabs: [...tabs, closed], activeKey: tabKey(closed), closedStack: stack });
  },
  setActiveTab: (key) => set({ activeKey: key }),
  // v11: 切换预览文件时清空 diff（diff 视图仅由变更审核卡片进入时提供）
  // v2.2: 支持行号定位（grep path:line 跳转）
  setPreviewPath: (path, line = null) => set({ previewPath: path, previewLine: line, diffPreview: null }),
  setDiffPreview: (diff) => set({ diffPreview: diff }),
  reset: () => set({ expanded: false, fullscreen: false, tabs: [], activeKey: null, previewPath: null, previewLine: null, diffPreview: null }),
}));
