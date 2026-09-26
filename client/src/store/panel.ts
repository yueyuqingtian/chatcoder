/** 右侧面板状态（v21）：按会话分桶 + 折叠保活（plan-41-233）。
 *
 *  两个用户诉求驱动本次改造：
 *  ① 折叠再展开不能丢状态：右面板容器改为常驻挂载（见 App.tsx），终端/浏览器实例不再因
 *     折叠被卸载重建；
 *  ② 每个会话独立：tabs / activeKey / closedStack / 预览状态按会话分桶存储，切换会话只
 *     切换投影，不再出现「A 会话的子代理标签出现在 B 会话」。
 *
 *  为什么顶层仍保留同名字段：ToolTree / TaskProgressCapsule / FileTreePanel 等调用点都按
 *  「当前会话」语义直接读写这些字段，保留同名投影让它们零改动继续工作；真正的数据源是
 *  `buckets`，顶层字段只是当前桶的镜像（任何写入都会同步回桶），读取方无需感知分桶。
 */
import { create } from "zustand";
import { applyPaneUpdate } from "../perf/paneTransition";

export type PanelTabId = "task-summary" | "browser" | "terminal" | "files" | "subagent" | "debug";
export interface PanelTab {
  id: PanelTabId;
  instance: number;
  /** v2.2 (对齐 zcode 3.13): 子代理详情 tab 参数（threadId=agent_id） */
  meta?: { threadId?: number; agentName?: string };
}

/** v11: 变更审核 diff 预览（右面板「文件」标签页的 Monaco DiffEditor 数据源）。 */
export interface DiffPreview {
  path: string;
  before: string | null;
  after: string | null;
  truncated: boolean;
}

/** 单个会话（或无会话的 home）持有的面板内容状态。 */
export interface PanelBucket {
  tabs: PanelTab[];
  activeKey: string | null;
  /** v2.2 (对齐 zcode 3.3.3): 最近关闭标签（上限 10），供加号菜单恢复 */
  closedStack: PanelTab[];
  previewPath: string | null;
  /** v2.2 (对齐 zcode 3.14.2): 预览文件定位行号（grep path:line 跳转，Monaco revealLine） */
  previewLine: number | null;
  /** v11: 变更审核 diff 预览（path 匹配 previewPath 时 FileTreePanel 展示 DiffEditor） */
  diffPreview: DiffPreview | null;
}

const STORAGE_KEY = "chatcoder.panel";
const DEFAULT_WIDTH = 420;
const MAX_WIDTH = 1200;

/** 无会话（首页 / 新建空白态）时的桶 key。 */
const HOME_BUCKET = "home";
/** 会话 id → 桶 key（无会话时为 home）。 */
export function bucketKeyOf(sessionId: number | null | undefined): string {
  return sessionId == null ? HOME_BUCKET : `s${sessionId}`;
}
/** 桶 key → 所属会话 id（home → null）。子面板据此确定 cwd / 浏览器分桶。 */
export function bucketSessionId(key: string): number | null {
  if (!key.startsWith("s")) return null;
  const n = Number(key.slice(1));
  return Number.isFinite(n) ? n : null;
}

function emptyBucket(): PanelBucket {
  return { tabs: [], activeKey: null, closedStack: [], previewPath: null, previewLine: null, diffPreview: null };
}
/** 桶 → 顶层投影（当前桶字段镜像）。 */
function projectionOf(b: PanelBucket) {
  return {
    tabs: b.tabs,
    activeKey: b.activeKey,
    closedStack: b.closedStack,
    previewPath: b.previewPath,
    previewLine: b.previewLine,
    diffPreview: b.diffPreview,
  };
}
function tabKey(t: PanelTab) { return `${t.id}-${t.instance}`; }

function loadState(): { width: number } {
  if (typeof window === "undefined") return { width: DEFAULT_WIDTH };
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return { width: DEFAULT_WIDTH };
    return { width: JSON.parse(raw).width ?? DEFAULT_WIDTH };
  } catch { return { width: DEFAULT_WIDTH }; }
}
function saveWidth(width: number) { try { localStorage.setItem(STORAGE_KEY, JSON.stringify({ width })); } catch {} }

interface PanelState extends PanelBucket {
  expanded: boolean;
  width: number;
  fullscreen: boolean;
  /** v10: 主窗口顶部任务卡显隐开关（TitleBar 折叠按钮控制） */
  taskCardVisible: boolean;
  /** 按会话分桶的面板内容（key = bucketKeyOf(sessionId)） */
  buckets: Record<string, PanelBucket>;
  /** 当前投影对应的桶 key（App 跟随 currentSessionId 同步） */
  activeBucket: string;
  /** 切换当前会话：把顶层投影替换为目标桶的内容（不删除旧桶，实例继续保活）。 */
  setActiveSession: (sessionId: number | null) => void;
  /** 会话被删除后清理其桶（终端等实例随卸载自然回收）。 */
  dropBucket: (sessionId: number) => void;
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
  buckets: {},
  activeBucket: HOME_BUCKET,
  ...projectionOf(emptyBucket()),

  setActiveSession: (sessionId) => {
    const key = bucketKeyOf(sessionId);
    set((s) => {
      if (s.activeBucket === key) return {};
      const b = s.buckets[key] ?? emptyBucket();
      return { activeBucket: key, ...projectionOf(b) };
    });
  },

  dropBucket: (sessionId) => {
    const key = bucketKeyOf(sessionId);
    set((s) => {
      if (!s.buckets[key]) return {};
      const buckets = { ...s.buckets };
      delete buckets[key];
      if (s.activeBucket !== key) return { buckets };
      return { buckets, activeBucket: HOME_BUCKET, ...projectionOf(emptyBucket()) };
    });
  },

  openPanel: () => {
    if (get().expanded) return;
    // plan-31-152 S5-1：展开是一次几何变更——交 applyPaneUpdate 统一收尾（CSS 过渡 + 收敛序列）
    applyPaneUpdate(() => set({ expanded: true }));
  },
  closePanel: () => {
    if (!get().expanded) return;
    // 关闭面板同时清掉当前会话的 diff 预览（预览是「变更审核」的一次性临时态）
    const key = get().activeBucket;
    applyPaneUpdate(() => {
      set({ expanded: false, fullscreen: false });
      writeBucket(key, (b) => ({ ...b, diffPreview: null }));
    });
  },
  togglePanel: () => applyPaneUpdate(() => set((s) => ({ expanded: !s.expanded }))),
  toggleFullscreen: () => applyPaneUpdate(() => set((s) => ({ fullscreen: !s.fullscreen }))),
  toggleTaskCard: () => set((s) => ({ taskCardVisible: !s.taskCardVisible })),
  setWidth: (w) => {
    const clamped = Math.max(200, Math.min(MAX_WIDTH, Math.round(w)));
    set({ width: clamped });
    saveWidth(clamped);
  },

  openTab: (id) => {
    const key = get().activeBucket;
    const b = get().buckets[key] ?? emptyBucket();
    const existing = b.tabs.find((t) => t.id === id);
    if (existing) { writeBucket(key, (cur) => ({ ...cur, activeKey: tabKey(existing) })); return; }
    const tab: PanelTab = { id, instance: b.tabs.filter((t) => t.id === id).length + 1 };
    writeBucket(key, (cur) => ({ ...cur, tabs: [...cur.tabs, tab], activeKey: tabKey(tab) }));
  },

  openNewTab: (id) => {
    const key = get().activeBucket;
    const b = get().buckets[key] ?? emptyBucket();
    const tab: PanelTab = { id, instance: b.tabs.filter((t) => t.id === id).length + 1 };
    const apply = () => writeBucket(key, (cur) => ({ ...cur, tabs: [...cur.tabs, tab], activeKey: tabKey(tab) }));
    if (!get().expanded) { applyPaneUpdate(() => { set({ expanded: true }); apply(); }); return; }
    apply();
  },

  openSubagent: (threadId, agentName) => {
    const key = get().activeBucket;
    const b = get().buckets[key] ?? emptyBucket();
    // 同线程已开则激活，否则新开实例
    const existing = b.tabs.find((t) => t.id === "subagent" && t.meta?.threadId === threadId);
    const needExpand = !get().expanded;
    if (existing) {
      const activate = () => writeBucket(key, (cur) => ({ ...cur, activeKey: tabKey(existing) }));
      if (needExpand) { applyPaneUpdate(() => { set({ expanded: true }); activate(); }); return; }
      activate();
      return;
    }
    const tab: PanelTab = {
      id: "subagent",
      instance: b.tabs.filter((t) => t.id === "subagent").length + 1,
      meta: { threadId, agentName },
    };
    const apply = () => writeBucket(key, (cur) => ({ ...cur, tabs: [...cur.tabs, tab], activeKey: tabKey(tab) }));
    if (needExpand) { applyPaneUpdate(() => { set({ expanded: true }); apply(); }); return; }
    apply();
  },

  closeTab: (key) => {
    writeBucket(get().activeBucket, (b) => {
      const closed = b.tabs.find((t) => tabKey(t) === key);
      const next = b.tabs.filter((t) => tabKey(t) !== key);
      let nextActive = b.activeKey;
      if (b.activeKey === key) {
        const closedIdx = b.tabs.findIndex((t) => tabKey(t) === key);
        const fallback = next[Math.min(closedIdx, next.length - 1)];
        nextActive = fallback ? tabKey(fallback) : null;
      }
      // v2.2: 最近关闭入栈（上限 10）
      const stack = closed
        ? [closed, ...b.closedStack.filter((t) => tabKey(t) !== key)].slice(0, 10)
        : b.closedStack;
      return { ...b, tabs: next, activeKey: nextActive, closedStack: stack };
    });
  },

  reopenClosedTab: (index) => {
    writeBucket(get().activeBucket, (b) => {
      const closed = b.closedStack[index];
      if (!closed) return b;
      const stack = b.closedStack.filter((_, i) => i !== index);
      // 同 key 已存在则只激活；否则把标签加回（原实现只出栈未回填，恢复后标签不出现）
      if (b.tabs.some((t) => tabKey(t) === tabKey(closed))) {
        return { ...b, closedStack: stack, activeKey: tabKey(closed) };
      }
      return { ...b, tabs: [...b.tabs, closed], closedStack: stack, activeKey: tabKey(closed) };
    });
  },

  setActiveTab: (key) => writeBucket(get().activeBucket, (b) => ({ ...b, activeKey: key })),
  // v11: 切换预览文件时清空 diff（diff 视图仅由变更审核卡片进入时提供）
  // v2.2: 支持行号定位（grep path:line 跳转）
  setPreviewPath: (path, line = null) => writeBucket(get().activeBucket,
    (b) => ({ ...b, previewPath: path, previewLine: line, diffPreview: null })),
  setDiffPreview: (diff) => writeBucket(get().activeBucket, (b) => ({ ...b, diffPreview: diff })),

  reset: () => set({ expanded: false, fullscreen: false, buckets: {}, activeBucket: HOME_BUCKET, ...projectionOf(emptyBucket()) }),
}));

/** 对指定桶做一次不可变更新，并同步顶层投影（仅当该桶是当前桶）。
 *  所有内容写入都经这里落库，保证「buckets 是唯一数据源、顶层字段是镜像」。 */
function writeBucket(key: string, fn: (b: PanelBucket) => PanelBucket) {
  usePanelStore.setState((s) => {
    const next = fn(s.buckets[key] ?? emptyBucket());
    const buckets = { ...s.buckets, [key]: next };
    return key === s.activeBucket ? { buckets, ...projectionOf(next) } : { buckets };
  });
}
