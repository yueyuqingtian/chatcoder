/** 右侧面板状态（v21）：按会话分桶 + 折叠保活（plan-41-233）。
 *
 *  两个用户诉求驱动本次改造：
 *  ① 折叠再展开不能丢状态：右面板容器改为常驻挂载（见 App.tsx），终端/浏览器实例不再因
 *     折叠被卸载重建；
 *  ② 每个会话独立：tabs / activeKey / closedStack / 预览状态按会话分桶存储，切换会话只
 *     切换投影，不再出现「A 会话的子代理标签出现在 B 会话」。
 *
 *  为什么顶层仍保留同名字段：ToolTree / ComposerCapsule / FileTreePanel 等调用点都按
 *  「当前会话」语义直接读写这些字段，保留同名投影让它们零改动继续工作；真正的数据源是
 *  `buckets`，顶层字段只是当前桶的镜像（任何写入都会同步回桶），读取方无需感知分桶。
 */
import { api, previewApi, type PreviewSource, type FileChangeOut } from "../api/client";
import { openGallery } from "./gallery";
import { create } from "zustand";
import { applyPaneUpdate } from "../perf/paneTransition";

export type PanelTabId = "task-summary" | "browser" | "terminal" | "files" | "subagent" | "debug" | "workflow" | "side" | "git" | "microapp";
export interface PanelTab {
  id: PanelTabId;
  /** 同类型标签的展示序号（只用于文案「文件 2」，不再承担唯一标识） */
  instance: number;
  /** 标签的唯一标识（plan-20261027-panel-tab：此前的 key 是 `id-instance`，关闭后重新打开会复用同一个
   *  instance，于是在同一会话里出现两个 key 完全相同的标签——React 会复用同一个 DOM 节点、
   *  事件处理器绑到错误的元素上，表现为「点关闭没反应 / 点标签切不过去、但 hover 正常」。 */
  uid?: string;
  /** v2.2 (对齐 zcode 3.13): 子代理详情 tab 参数（threadId=agent_id） */
  /** plan-693-2868：微应用标签的绑定信息——appId 是去重键，version/entry 决定加载哪个版本的界面；
   *  与子代理的 threadId/agentName 共存于同一 meta，避免再引入第二套标签参数。
   *  `uid` 是标签自身 `uid` 的镜像（协议 §6 按 meta 里的身份描述实例）：权威值仍是顶层的
   *  `PanelTab.uid`（`panelTabKey` 用它做唯一 key），这里复制一份供「按实例身份找标签」的调用方
   *  少写一次顶层字段；重建标签（`reopenClosedTab`）时会与新 uid 一起刷新，不会留旧值。
   *  它同时是宿主桥的实例标识来源：`microapp#3` → webview 的 `?uid=` → 页面 preload 的 instance.uid。 */
  meta?: {
    threadId?: number; agentName?: string;
    appId?: string; version?: string; entry?: string;
    instanceId?: string; bindingId?: string; uid?: string;
    /** plan-external-microapps：外置微应用（自带前后端与进程）——有值就直连该 http 地址，
     *  不再走 `app://microapp/<appId>/<version>/<entry>` 包装，也不注入宿主桥 preload。
     *  它同时也是「是否外置」的唯一判据（渲染层不再单独存一个布尔镜像，避免两处判断分叉）。 */
    externalUrl?: string;
  };
}

/** 标签唯一 key（渲染、比较、关闭都走它）。历史标签没有 uid 时回退到旧口径，保证老状态不崩。 */
export function panelTabKey(t: PanelTab): string {
  return t.uid ?? `${t.id}-${t.instance}`;
}

/** v11: 变更审核 diff 预览（右面板「文件」标签页的 Monaco DiffEditor 数据源）。 */
export interface DiffPreview {
  path: string;
  before: string | null;
  after: string | null;
  truncated: boolean;
  reason?: string | null;
  /** plan-20261019-toolcard-v2：文件在本轮被删除——磁盘上已不存在，预览不读盘、不提供「当前内容」 */
  deleted?: boolean;
}

/** plan-20261105-file-review：导航固定在点击的变更卡，不混入其他轮次。 */
export interface TurnFileReview {
  turnId: number;
  extraTurnIds: number[];
  files: FileChangeOut[];
  index: number;
  mode: "content" | "diff";
  loading?: boolean;
  error?: string;
}

/** 单个会话（或无会话的 home）持有的面板内容状态。 */
export interface PanelBucket {
  tabs: PanelTab[];
  activeKey: string | null;
  /** v2.2 (对齐 zcode 3.3.3): 最近关闭标签（上限 10），供加号菜单恢复 */
  closedStack: PanelTab[];
  previewRequestId: number;
  previewTarget: PreviewSource | null;
  previewPath: string | null;
  /** v2.2 (对齐 zcode 3.14.2): 预览文件定位行号（grep path:line 跳转，Monaco revealLine） */
  previewLine: number | null;
  /** v11: 变更审核 diff 预览（path 匹配 previewPath 时 FileTreePanel 展示 DiffEditor） */
  diffPreview: DiffPreview | null;
  turnFileReview: TurnFileReview | null;
  /** 从计划卡打开完整计划后置位：文件管理在可见时消费一次，收起目录树（用户仍可手动展开） */
  planCollapsePending: boolean;
}

const STORAGE_KEY = "chatcoder.panel";
const DEFAULT_WIDTH = 400;
/** 只做数值兜底：真实上限由可用空间决定（ResizeHandle 拖拽钳制 + .app-pane-right 渲染期钳制，plan-20261016-ux） */
const MAX_WIDTH = 4096;

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
  return { tabs: [], activeKey: null, closedStack: [], previewRequestId: 0, previewTarget: null, previewPath: null, previewLine: null, diffPreview: null, turnFileReview: null, planCollapsePending: false };
}
/** 桶 → 顶层投影（当前桶字段镜像）。 */
function projectionOf(b: PanelBucket) {
  return {
    tabs: b.tabs,
    activeKey: b.activeKey,
    closedStack: b.closedStack,
    previewRequestId: b.previewRequestId ?? 0,
    previewTarget: b.previewTarget ?? null,
    previewPath: b.previewPath,
    previewLine: b.previewLine,
    diffPreview: b.diffPreview,
    turnFileReview: b.turnFileReview ?? null,
    planCollapsePending: b.planCollapsePending,
  };
}

/** 标签 uid 的单调序号（plan-20261027-panel-tab）：跨桶全局唯一，避免同类型标签反复开关后 key 撞车。 */
let uidSeq = 0;
function nextUid(id: PanelTabId): string {
  uidSeq += 1;
  return `${id}#${uidSeq}`;
}
/** 同类型标签的展示序号：取现有最大值 +1，关闭前面的标签也不会与仍在的标签重名。 */
function nextInstance(tabs: PanelTab[], id: PanelTabId): number {
  const used = tabs.filter((t) => t.id === id).map((t) => t.instance);
  return used.length ? Math.max(...used) + 1 : 1;
}

/** 桶内容归一化（plan-20261027-panel-tab）：
 *  ① 去掉重复 key 的标签（历史上关闭后重开复用 instance 会撞 key，重复 key 会让 React 复用同一个
 *      DOM 节点、点击落到错误的元素上——用户看到的「点了没反应、hover 正常」）；
 *  ② activeKey 悬空（指向已关闭的标签）时回落到第一个标签，避免「标签条高亮一个不存在的标签、
 *     点别的标签也切不过去」的死状态。 */
function normalizeBucket(b: PanelBucket): PanelBucket {
  const seen = new Set<string>();
  const tabs: PanelTab[] = [];
  for (const t of b.tabs) {
    const k = panelTabKey(t);
    if (seen.has(k)) continue;
    seen.add(k);
    tabs.push(t);
  }
  let activeKey = b.activeKey;
  if (activeKey != null && !seen.has(activeKey)) activeKey = tabs.length ? panelTabKey(tabs[0]) : null;
  if (tabs.length === b.tabs.length && activeKey === b.activeKey) return b;
  return { ...b, tabs, activeKey };
}

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
  /** plan-20261015-browser-agent：右面板此刻是否真的显示在屏幕上（展开且不在设置/拓展等页面态）。
   *  由 App 按 rightShown 同步；浏览器面板据此决定 webview 是正常显示还是“停泊”在后台继续运行。 */
  onScreen: boolean;
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
  /** plan-20261015-browser-agent：确保**指定会话**的面板里有该标签（AI 在后台会话里用浏览器时也能挂上），
   *  不展开面板；面板没在显示该会话时把它设为当前标签——用户一展开就停在 AI 打开的网页上。 */
  ensureTabIn: (sessionId: number | null, id: PanelTabId) => void;
  setOnScreen: (on: boolean) => void;
  /** v2.2 (对齐 zcode 3.15): 强制新开实例（终端多标签等场景，不去重） */
  openNewTab: (id: PanelTabId) => void;
  /** v2.2 (对齐 zcode 3.13): 打开子代理详情 tab（每线程独立实例，可多开） */
  openSubagent: (threadId: number, agentName?: string) => void;
  /** plan-693-2868：打开微应用标签。同 appId 聚焦已有标签（升级换版本仍复用），不同 appId 各开一个、不设上限。
   *  plan-external-microapps：`externalUrl` 有值时是外置微应用——面板直连该 http 地址。 */
  openMicroApp: (appId: string, version: string, entry?: string, externalUrl?: string) => void;
  /** plan-693-2868 回归修复：只在**指定会话**的桶里挂上微应用标签，绝不展开面板。
   *  AI 调 `microapp_call` 时用它——界面（webview）挂上就开始加载，但「打开面板」的权力仍只在用户手上。 */
  ensureMicroAppTab: (sessionId: number | null, appId: string, version: string, entry?: string, externalUrl?: string) => void;
  /** plan-156-620：待在 Git 面板展开差异的文件（工具卡点击文件时写入，GitPanel 消费后清空）。
   *  plan-156-621：带上项目标识——面板跨项目保活时，旧项目的路径不能拿到当前仓库请求差异。
   *  plan-20261019-git-multirepo：repo 是文件所属仓库（相对项目根），面板先切到该仓库再展开。 */
  gitFocus: { path: string; staged: boolean; projectId: number | null; repo: string } | null;
  /** plan-156-620：打开 Git 标签并展开某文件的差异。 */
  openGitFile: (path: string, staged: boolean, projectId?: number | null, repo?: string) => void;
  clearGitFocus: () => void;
  closeTab: (key: string) => void;
  /** v2.2 (对齐 zcode 3.3.3): 恢复最近关闭的标签（同时从栈中移除） */
  reopenClosedTab: (index: number) => void;
  setActiveTab: (key: string) => void;
  openResource: (target: PreviewSource, viewSessionId?: number | null) => void;
  setPreviewPath: (path: string | null, line?: number | null) => void;
  /** plan-20261018-turn-changes：一次性打开「文件 + 该文件的 diff」。
   *  卡片点「审查」时用：先设预览路径再写 diff 会让 DiffEditor 先卸载再挂载，
   *  Monaco 会抛 "TextModel got disposed before DiffEditorWidget model got reset"。 */
  openDiffPreview: (path: string, diff: DiffPreview) => void;
  /** 计划模式「查看完整计划」：预览计划文档，并请求文件管理收起目录树 */
  openPlanPreview: (path: string) => void;
  /** 文件管理在可见时消费「收起目录树」请求 */
  ackPlanCollapse: () => void;
  setDiffPreview: (diff: DiffPreview | null) => void;
  openTurnFile: (review: TurnFileReview, index: number, mode: "content" | "diff") => Promise<void>;
  reset: () => void;
}

const initial = loadState();
export const usePanelStore = create<PanelState>((set, get) => ({
  expanded: false,
  onScreen: false,
  width: initial.width,
  fullscreen: false,
  taskCardVisible: false, // v13: 对齐 zcode——待办在消息流内嵌展示，底部浮窗默认关闭（标题栏可再开）
  buckets: {},
  activeBucket: HOME_BUCKET,
  ...projectionOf(emptyBucket()),

  setActiveSession: (sessionId) => {
    const key = bucketKeyOf(sessionId);
    set((s) => {
      // plan-20261027-panel-tab：即使 activeBucket 没变也重算一次投影并归一化桶内容。
      // 早退会让「投影与桶脱节」的坏状态永久卡住（标签条显示 A 桶的标签、写入却落到 B 桶，
      // 表现就是关闭 / 切换点击全部无效）；这里每次切换都做一次校正，成本可忽略。
      const raw = s.buckets[key] ?? emptyBucket();
      const b = normalizeBucket(raw);
      const buckets = b === raw ? s.buckets : { ...s.buckets, [key]: b };
      return { buckets, activeBucket: key, ...projectionOf(b) };
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
      writeBucket(key, (b) => ({ ...b, diffPreview: null, turnFileReview: null }));
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
    if (existing) { writeBucket(key, (cur) => ({ ...cur, activeKey: panelTabKey(existing) })); return; }
    const tab: PanelTab = { id, instance: nextInstance(b.tabs, id), uid: nextUid(id) };
    writeBucket(key, (cur) => ({ ...cur, tabs: [...cur.tabs, tab], activeKey: panelTabKey(tab) }));
  },

  ensureTabIn: (sessionId, id) => {
    const key = bucketKeyOf(sessionId);
    const s = get();
    const b = s.buckets[key] ?? emptyBucket();
    const existing = b.tabs.find((t) => t.id === id);
    // 用户此刻正看着这个会话的面板：只补标签不切换（不打断用户）；否则设为当前标签
    const userWatching = s.onScreen && s.activeBucket === key && b.activeKey != null;
    if (existing) {
      if (!userWatching && b.activeKey !== panelTabKey(existing)) writeBucket(key, (cur) => ({ ...cur, activeKey: panelTabKey(existing) }));
      return;
    }
    const tab: PanelTab = { id, instance: nextInstance(b.tabs, id), uid: nextUid(id) };
    writeBucket(key, (cur) => ({ ...cur, tabs: [...cur.tabs, tab], activeKey: userWatching ? cur.activeKey : panelTabKey(tab) }));
  },

  setOnScreen: (on) => { if (get().onScreen !== on) set({ onScreen: on }); },

  openNewTab: (id) => {
    const key = get().activeBucket;
    const b = get().buckets[key] ?? emptyBucket();
    const tab: PanelTab = { id, instance: nextInstance(b.tabs, id), uid: nextUid(id) };
    const apply = () => writeBucket(key, (cur) => ({ ...cur, tabs: [...cur.tabs, tab], activeKey: panelTabKey(tab) }));
    if (!get().expanded) { applyPaneUpdate(() => { set({ expanded: true }); apply(); }); return; }
    apply();
  },

  gitFocus: null,
  openGitFile: (path, staged, projectId = null, repo = "") => {
    set({ gitFocus: { path, staged, projectId, repo } });
    get().openPanel();
    get().openTab("git");
  },
  clearGitFocus: () => set({ gitFocus: null }),

  openSubagent: (threadId, agentName) => {
    const key = get().activeBucket;
    const b = get().buckets[key] ?? emptyBucket();
    // 同线程已开则激活，否则新开实例
    const existing = b.tabs.find((t) => t.id === "subagent" && t.meta?.threadId === threadId);
    const needExpand = !get().expanded;
    if (existing) {
      const activate = () => writeBucket(key, (cur) => ({ ...cur, activeKey: panelTabKey(existing) }));
      if (needExpand) { applyPaneUpdate(() => { set({ expanded: true }); activate(); }); return; }
      activate();
      return;
    }
    const tab: PanelTab = {
      id: "subagent",
      instance: nextInstance(b.tabs, "subagent"),
      uid: nextUid("subagent"),
      meta: { threadId, agentName },
    };
    const apply = () => writeBucket(key, (cur) => ({ ...cur, tabs: [...cur.tabs, tab], activeKey: panelTabKey(tab) }));
    if (needExpand) { applyPaneUpdate(() => { set({ expanded: true }); apply(); }); return; }
    apply();
  },

  openMicroApp: (appId, version, entry, externalUrl) => {
    const key = get().activeBucket;
    const b = get().buckets[key] ?? emptyBucket();
    // 同 appId 已开则激活：升级后 meta.version 变化，但用户视角还是同一个应用，不该再堆一个标签
    const existing = b.tabs.find((t) => t.id === "microapp" && t.meta?.appId === appId);
    const needExpand = !get().expanded;
    if (existing) {
      writeBucket(key, (cur) => ({
        ...cur,
        tabs: cur.tabs.map((t) => (panelTabKey(t) === panelTabKey(existing) ? { ...t, meta: { ...t.meta, version, entry, externalUrl } } : t)),
        activeKey: panelTabKey(existing),
      }));
      if (needExpand) applyPaneUpdate(() => set({ expanded: true }));
      return;
    }
    // uid 由 store 统一分配（跨桶单调递增），是宿主桥的实例标识来源（协议 §6）：
    // webview 的 `?uid=`、主进程的 uid→guest 映射、页面的 instance.uid 全靠它，
    // 所以**每一条**创建微应用标签的路径都必须写入顶层的 uid，并在 meta 里留一份镜像。
    const uid = nextUid("microapp");
    const tab: PanelTab = { id: "microapp", instance: nextInstance(b.tabs, "microapp"), uid, meta: { appId, version, entry, uid, externalUrl } };
    const apply = () => writeBucket(key, (cur) => ({ ...cur, tabs: [...cur.tabs, tab], activeKey: panelTabKey(tab) }));
    if (needExpand) { applyPaneUpdate(() => { set({ expanded: true }); apply(); }); return; }
    apply();
  },

  /** plan-693-2868 回归修复：AI 调用 `microapp_call` 前由宿主侧调用它。
   *
   *  为什么需要：微应用界面（guest webview）**唯一**来源是右侧面板里的那个微应用标签，
   *  此前只「找」不「造」——用户没手动打开界面时调用必然报「没有找到 xxx 的界面」。
   *
   *  三条约束：
   *  ① 绝不 `set({ expanded: true })`：面板的打开权只在用户手上（docs/app1/README.md 四条界面约定），
   *     `openMicroApp` 里的 needExpand 逻辑不能复用；容器常驻挂载（App.tsx），折叠状态下 webview 照样加载。
   *  ② 桶 key 取 `bucketKeyOf(sessionId)` 而不是 `activeBucket`：AI 可能在后一个会话里调用，
   *     标签必须落在**发起调用的那个会话**，不能塞进用户此刻正在看的会话。
   *  ③ 用户正在看这个桶的面板时不抢 activeKey（不打断），否则设为当前标签——用户一展开就停在这个应用上
   *     （与 `ensureTabIn` 同语义）。
   */
  ensureMicroAppTab: (sessionId, appId, version, entry, externalUrl) => {
    const key = bucketKeyOf(sessionId);
    const s = get();
    const b = s.buckets[key] ?? emptyBucket();
    // 同 appId 复用已有标签：升级后 meta.version 变化，但用户视角还是同一个应用，不该再堆一个标签
    const existing = b.tabs.find((t) => t.id === "microapp" && t.meta?.appId === appId);
    const userWatching = s.onScreen && s.activeBucket === key && b.activeKey != null;
    if (existing) {
      const existingKey = panelTabKey(existing);
      // 只刷新 meta（版本 / 入口 / 外置地址），不新建实例：webview 的 src 会随之变化并重新加载
      writeBucket(key, (cur) => ({
        ...cur,
        tabs: cur.tabs.map((t) => (panelTabKey(t) === existingKey ? { ...t, meta: { ...t.meta, version, entry, externalUrl } } : t)),
        activeKey: userWatching ? cur.activeKey : existingKey,
      }));
      return;
    }
    // 与 openMicroApp 同一口径：uid 必须写在顶层（宿主桥按它寻址），meta 里同步一份镜像
    const uid = nextUid("microapp");
    const tab: PanelTab = { id: "microapp", instance: nextInstance(b.tabs, "microapp"), uid, meta: { appId, version, entry, uid, externalUrl } };
    writeBucket(key, (cur) => ({
      ...cur,
      tabs: [...cur.tabs, tab],
      activeKey: userWatching ? cur.activeKey : panelTabKey(tab),
    }));
  },


  closeTab: (key) => {
    const bucket = get().activeBucket;
    const inBucket = (get().buckets[bucket]?.tabs ?? []).some((t) => panelTabKey(t) === key);
    if (!inBucket) {
      // plan-20261027-panel-tab 自愈：当前桶里找不到这个标签，说明渲染用的标签条与 activeBucket 已经打偏
      // （历史坏状态，或切换会话与写入并发）。画面上它明明还在，直接不处理就成了「叉不掉」；
      // 这里从所有桶里摘掉同名标签，让已经卡住的标签也能被关掉，不靠重启。
      set((s) => {
        const buckets = { ...s.buckets };
        let changed = false;
        for (const [k, b] of Object.entries(s.buckets)) {
          const next = b.tabs.filter((t) => panelTabKey(t) !== key);
          if (next.length === b.tabs.length) continue;
          changed = true;
          buckets[k] = normalizeBucket({ ...b, tabs: next });
        }
        if (!changed) return {};
        const active = buckets[s.activeBucket] ?? emptyBucket();
        return { buckets, ...projectionOf(active) };
      });
      return;
    }
    writeBucket(bucket, (b) => {
      const closed = b.tabs.find((t) => panelTabKey(t) === key);
      const next = b.tabs.filter((t) => panelTabKey(t) !== key);
      let nextActive = b.activeKey;
      if (b.activeKey === key) {
        const closedIdx = b.tabs.findIndex((t) => panelTabKey(t) === key);
        const fallback = next[Math.min(closedIdx, next.length - 1)] ?? next[0];
        nextActive = fallback ? panelTabKey(fallback) : null;
      }
      // v2.2: 最近关闭入栈（上限 10）
      const stack = closed
        ? [closed, ...b.closedStack.filter((t) => panelTabKey(t) !== key)].slice(0, 10)
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
      const existing = b.tabs.find((t) => panelTabKey(t) === panelTabKey(closed));
      if (existing) return { ...b, closedStack: stack, activeKey: panelTabKey(existing) };
      // 恢复的标签重新分配 uid / instance：旧 key 可能已被后来新开的同类型标签占用，
      // 直接放回会造成重复 key（点击串到别的标签上）。
      // 微应用标签的 meta.uid 镜像必须跟着换：否则宿主桥会把恢复后的界面当成旧实例，
      // 下行日志按 uid 投递就会找不到目标（协议 §6 的实例寻址）。
      const revivedUid = nextUid(closed.id);
      const revived: PanelTab = {
        ...closed,
        instance: nextInstance(b.tabs, closed.id),
        uid: revivedUid,
        meta: closed.meta && closed.meta.uid != null ? { ...closed.meta, uid: revivedUid } : closed.meta,
      };
      return { ...b, tabs: [...b.tabs, revived], closedStack: stack, activeKey: panelTabKey(revived) };
    });
  },

  // plan-20261027-panel-tab：只接受「确实存在于当前桶」的 key。否则 activeKey 会悬空指向一个
  // 已关闭的标签，标签条高亮与内容区同时错位，点其他标签也无法恢复。
  setActiveTab: (key) => {
    const bucket = get().activeBucket;
    const exists = (get().buckets[bucket]?.tabs ?? []).some((t) => panelTabKey(t) === key);
    if (!exists) return;
    writeBucket(bucket, (b) => ({ ...b, activeKey: key }));
  },
  // v11: 切换预览文件时清空 diff（diff 视图仅由变更审核卡片进入时提供）
  // v2.2: 支持行号定位（grep path:line 跳转）
  openResource: (target, viewSessionId) => {
    const key = bucketKeyOf(viewSessionId === undefined ? target.session_id : viewSessionId);
    writeBucket(key, (b) => ({ ...b, previewRequestId: (b.previewRequestId ?? 0) + 1, previewTarget: target, previewPath: null, previewLine: null, diffPreview: null, turnFileReview: null }));
    if (get().activeBucket === key) { get().openPanel(); get().openTab("files"); }
  },
  setPreviewPath: (path, line = null) => writeBucket(get().activeBucket,
    (b) => ({ ...b, previewRequestId: (b.previewRequestId ?? 0) + 1, previewTarget: null, previewPath: path, previewLine: line, diffPreview: null, turnFileReview: null })),
  openPlanPreview: (path) => writeBucket(get().activeBucket,
    (b) => ({ ...b, previewRequestId: (b.previewRequestId ?? 0) + 1, previewTarget: null, previewPath: path, previewLine: null, diffPreview: null, turnFileReview: null, planCollapsePending: true })),
  ackPlanCollapse: () => writeBucket(get().activeBucket, (b) => ({ ...b, planCollapsePending: false })),
  setDiffPreview: (diff) => writeBucket(get().activeBucket, (b) => ({ ...b, diffPreview: diff, turnFileReview: null })),
  // plan-20261018-turn-changes: 预览路径与 diff 一次写入，避免中间态让 DiffEditor 卸载重建
  openDiffPreview: (path, diff) => writeBucket(get().activeBucket,
    (b) => ({ ...b, previewRequestId: (b.previewRequestId ?? 0) + 1, previewTarget: null, previewPath: path, previewLine: null, diffPreview: diff, turnFileReview: null })),

  openTurnFile: async (review, index, requestedMode) => {
    const file = review.files[index];
    if (!file) return;
    const key = get().activeBucket;
    const mode = file.action === "deleted" ? "diff" : requestedMode;
    const context: TurnFileReview = { ...review, index, mode, loading: mode === "diff", error: undefined };
    writeBucket(key, b => ({ ...b, previewRequestId: b.previewRequestId + 1, previewTarget: null,
      previewPath: file.path, previewLine: null, diffPreview: null, turnFileReview: context, planCollapsePending: true }));
    const requestId = get().buckets[key].previewRequestId;
    get().openPanel();
    get().openTab("files");
    if (mode === "content") {
      if (/\.(png|jpe?g|gif|webp|bmp|svg|avif)$/i.test(file.path)) {
        const chat = (await import("./chat")).useChatStore.getState();
        const sessionId = bucketSessionId(key);
        const projectId = chat.sessions.find(s => s.id === sessionId)?.project_id;
        if (sessionId != null && projectId != null && get().activeBucket === key && get().buckets[key]?.previewRequestId === requestId) {
          openGallery([{ url: previewApi.contentUrl({ source: "project", session_id: sessionId, project_id: projectId, path: file.path }), name: file.path.split(/[\\/]/).pop() ?? file.path }]);
        }
      }
      return;
    }
    try {
      const d = await api.getFileDiff(review.turnId, file.path, undefined, review.extraTurnIds, true);
      writeBucket(key, b => b.previewRequestId !== requestId || b.turnFileReview !== context ? b : ({ ...b,
        diffPreview: { path: file.path, before: d.before, after: d.after, truncated: d.truncated, reason: d.reason, deleted: d.action === "deleted" },
        turnFileReview: { ...context, loading: false } }));
    } catch (error) {
      writeBucket(key, b => b.previewRequestId !== requestId || b.turnFileReview !== context ? b : ({ ...b,
        turnFileReview: { ...context, loading: false, error: `无法读取本轮变更：${String(error)}` } }));
    }
  },

  reset: () => set({ expanded: false, fullscreen: false, buckets: {}, activeBucket: HOME_BUCKET, ...projectionOf(emptyBucket()) }),
}));

/** 对指定桶做一次不可变更新，并同步顶层投影（仅当该桶是当前桶）。
 *  所有内容写入都经这里落库，保证「buckets 是唯一数据源、顶层字段是镜像」。 */
function writeBucket(key: string, fn: (b: PanelBucket) => PanelBucket) {
  usePanelStore.setState((s) => {
    // 所有写入都过一遍归一化：重复 key 与悬空 activeKey 在落库前就被修正，
    // 不给渲染层留「点不动」的机会（plan-20261027-panel-tab）。
    const next = normalizeBucket(fn(s.buckets[key] ?? emptyBucket()));
    const buckets = { ...s.buckets, [key]: next };
    return key === s.activeBucket ? { buckets, ...projectionOf(next) } : { buckets };
  });
}
