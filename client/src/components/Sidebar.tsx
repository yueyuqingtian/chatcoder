/** 左侧栏（v7 完全对齐 ZCode）：
 * 顶部 logo + 前进/后退
 * 主导航（新建任务 Ctrl+N / 搜索 Ctrl+K / 自动化 / 技能）
 * 分组|项目胶囊切换 + 操作图标（展开/排序/新建项目）
 * 项目→会话两级列表（会话行带相对时间）
 * 底部用户条（设置 + 更新徽标）
 */
import { notify } from "../store/toast";
import { IxNewChat, IxSearch, IxAutomation, IxExtensions, IxSettings, IxWorkflow } from "./iconkit/StackIcons";
import { Fragment, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { ProjectOut, SessionOut } from "../api/client";
import { api } from "../api/client";
import { patchSession, useChatStore } from "../store/chat";
import { sessionWorkflowRunning, useWorkflowStore } from "../store/workflow";
import { useDraftsStore } from "../store/drafts";
import { useUpdaterStore } from "../store/updater";
import { useI18n } from "../store/i18n";
import { formatRelativeTime, parseUtc } from "../utils/time";
import { ConfirmDialog } from "./ConfirmDialog";
import { MergeDialog } from "./worktree/MergeDialog";
import { WorktreeCreateDialog } from "./worktree/WorktreeCreateDialog";
import { Collapse, Empty, Input, Segmented, Tooltip } from "./ui";
import { ProjectIcon } from "./project/ProjectIcon";
import { openProjectDialog } from "../store/projectDialog";
import {
  IconChevronDown,
  IconFolder, IconLayers, IconSortDesc, IconPencil,
  IconMoreHorizontal, IconPin, IconPlus, IconRefresh,
  IconFolderPlus, IconArrowDown, IconCopy, IconClock, IconSortAlpha, IconArchive, IconGitBranch, IconTrash, IconSpinner,
} from "./icons";

export type NavKey = "chat" | "scheduled" | "skills" | "mcp" | "settings";

interface SidebarProps {
  active: NavKey | null;
  onChange: (key: NavKey) => void;
  onSessionFocus: () => void;
  /** 兼容插件槽位契约：侧栏渲染不再依赖折叠态（见组件注释） */
  collapsed?: boolean;
  onToggleCollapse?: () => void;
}

const STORAGE_KEY_COLLAPSED_PROJECTS = "chatcoder:collapsed-projects";

/** plan-387-1825 E1：项目下会话列表的分批展开步长与「一次全开」阈值。
 *  语义：首次显示 5 条，每次点「显示更多」+5；已显示达到 10 条后，
 *  按钮文案变为「展开全部（剩余 N 项）」，点击一次全部展开。 */
/** 会话条目上的工作流标记（aria-label 与样式共用同一状态名） */
const WORKFLOW_STATE_LABEL: Record<string, string> = {
  attention: "工作流等待你的决定",
  paused: "工作流已暂停",
  running: "工作流进行中",
  draft: "工作流待确认",
};

const SESSION_PAGE_STEP = 5;
const SESSION_EXPAND_ALL_THRESHOLD = 10;

/** 更新入口：主进程发现新版本时在设置按钮右侧出现（plan-405-1900）。
 * 三态共用同一个固定宽度胶囊槽位——可更新（点击下载）/ 下载中（胶囊底色即进度）/ 已就绪（点击重启），
 * 状态切换只换内容不换尺寸，避免此前「描边圆钮 → 绿色进度胶囊 → 重启按钮」来回跳动。
 * 悬停不再弹出整篇更新说明（遮挡内容、聚焦即弹），只用 Tooltip 一句话说明；完整内容在「设置 › 关于」。 */
function UpdateBadge() {
  const { t } = useI18n();
  const status = useUpdaterStore((s) => s.status);
  const downloadUpdate = useUpdaterStore((s) => s.downloadUpdate);
  const installUpdate = useUpdaterStore((s) => s.installUpdate);
  if (status.state !== "available" && status.state !== "downloading" && status.state !== "downloaded") return null;

  const version = status.version ?? "";
  if (status.state === "downloading") {
    const percent = Math.max(0, Math.min(100, Math.round(status.percent ?? 0)));
    // plan-20261021-usage-update：点击后立刻是「连接中」（带转圈），有数据后才是百分比；
    // 断网 / 抖动 / 校验各有自己的一句话，全部落在同一个固定宽度胶囊里，切换不改尺寸
    const phase = status.phase ?? "downloading";
    const label =
      phase === "connecting" ? t("sidebar.update_connecting")
      : phase === "waiting" ? t("sidebar.update_waiting")
      : phase === "retrying" ? t("sidebar.update_retrying")
      : phase === "verifying" ? t("sidebar.update_verifying")
      : `${percent}%`;
    const tip =
      phase === "connecting" ? t("sidebar.update_connecting_tip")
      : phase === "waiting" ? t("sidebar.update_waiting_tip")
      : phase === "retrying" ? t("sidebar.update_retrying_tip")
      : phase === "verifying" ? t("sidebar.update_verifying_tip")
      : t("sidebar.downloading_update", { percent });
    return (
      <Tooltip content={tip} side="top">
        <span
          className="sb-upd is-downloading"
          role="progressbar"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={phase === "connecting" ? undefined : percent}
          aria-label={tip}
          style={{ ["--p" as string]: `${percent}%` }}
        >
          {phase === "connecting" && <IconSpinner size={12} />}
          <span className="sb-upd-label">{label}</span>
        </span>
      </Tooltip>
    );
  }
  const ready = status.state === "downloaded";
  const tip = ready ? t("sidebar.restart_update_tip", { version }) : t("sidebar.download_update_tip", { version });
  return (
    <Tooltip content={tip} side="top">
      <button
        type="button"
        className={"sb-upd " + (ready ? "is-ready" : "is-available")}
        aria-label={tip}
        onClick={() => { void (ready ? installUpdate() : downloadUpdate()); }}
      >
        {ready ? <IconRefresh size={13} /> : <IconArrowDown size={13} />}
        <span className="sb-upd-label">{ready ? t("sidebar.restart") : t("sidebar.update")}</span>
      </button>
    </Tooltip>
  );
}

function loadCollapsedProjects(): Record<number, boolean> {
  try {
    const raw = localStorage.getItem(STORAGE_KEY_COLLAPSED_PROJECTS);
    if (!raw) return {};
    return JSON.parse(raw);
  } catch {
    return {};
  }
}

function saveCollapsedProjects(val: Record<number, boolean>) {
  try {
    localStorage.setItem(STORAGE_KEY_COLLAPSED_PROJECTS, JSON.stringify(val));
  } catch { /* ignore */ }
}

/** 会话是否执行中：服务端的轮次 / 工作流标记，或本地得知的后台工作流在跑（工作流在跑时轮次可能已结束） */
/** plan-156-620：左栏子会话折叠状态的本地存储键 */
const SUBS_COLLAPSED_KEY = "chatcoder:sidebar-subs-collapsed";

function sessionBusy(s: SessionOut, busyIds: ReadonlySet<number>): boolean {
  return !!s.has_running || !!s.workflow_running || busyIds.has(s.id);
}

function shortPath(path: string): string {
  const clean = path.replace(/\\/g, "/").replace(/\/$/, "");
  const parts = clean.split("/");
  return parts[parts.length - 1] || clean;
}

// plan-128-518：折叠状态不再改变侧栏自身渲染——内容始终按展开态挂载，由左面板的宽度收窄 + overflow 裁切呈现，
// 折叠时左侧内容原地不动、只有右边缘（边框）与中间面板向左移；logo/折叠/前进后退已提到全局固定层（WindowNavCluster）。
export function Sidebar({ active, onChange, onSessionFocus }: SidebarProps) {
  const { t, language } = useI18n();
  const projects = useChatStore((s) => s.projects);
  const sessions = useChatStore((s) => s.sessions);
  const currentSessionId = useChatStore((s) => s.currentSessionId);
  const currentProjectId = useChatStore((s) => s.currentProjectId);
  const loadBootstrap = useChatStore((s) => s.loadBootstrap);
  /** plan-156-619 S5：主会话 id → 子会话列表（子会话不进入 list_sessions，必须单独取）。 */
  const subsessionsByParent = useChatStore((s) => s.subsessionsByParent);
  const switchSession = useChatStore((s) => s.switchSession);
  const deleteSession = useChatStore((s) => s.deleteSession);
  const renameSession = useChatStore((s) => s.renameSession);
  const forkSession = useChatStore((s) => s.forkSession);
  // 本地得知的后台工作流执行中的会话（只在 bySession 变化时重算，避免每次工作流事件都重绘侧栏）；
  // 加上「待唤醒」的会话（工作流结束到主代理被唤醒之间的空档，转圈不断；plan-20261014-actions-wait）
  const wfBySession = useWorkflowStore((s) => s.bySession);
  const wfWakeupPending = useWorkflowStore((s) => s.wakeupPending);
  const wfBusyIds = useMemo(() => {
    const ids = new Set(Object.entries(wfBySession).filter(([, runs]) => sessionWorkflowRunning(runs)).map(([sid]) => Number(sid)));
    for (const sid of Object.keys(wfWakeupPending)) ids.add(Number(sid));
    return ids;
  }, [wfBySession, wfWakeupPending]);
  // 当前会话的执行态以 store 为准（消息流与输入框同源）；列表标记刷新有延迟，故合并判定，两侧才能一致
  const currentRunning = useChatStore((s) => s.isRunning || s.runningTurnId != null);
  const busyIds = useMemo(() => {
    const ids = new Set(wfBusyIds);
    if (currentRunning && currentSessionId != null) ids.add(currentSessionId);
    return ids;
  }, [wfBusyIds, currentRunning, currentSessionId]);
  // plan-31-151 S3：侧栏头部拖拽/双击改由系统 -webkit-app-region:drag 处理（见 global.css），
  //   自研 useWindowDrag 已随 frame:false 伪最大化架构一并移除。

  const [view, setView] = useState<"group" | "project">(() => {
    return (localStorage.getItem("chatcoder:sidebar-view") as "group" | "project") || "project";
  });
  // plan-20261016-ux（问题 9）：置顶会话单独成区后「按置顶」排序已无意义（旧存储值按「最近」处理）
  const [sort, setSort] = useState<"recent" | "name">(() => {
    return localStorage.getItem("chatcoder:sidebar-sort") === "name" ? "name" : "recent";
  });
  const [sortMenuOpen, setSortMenuOpen] = useState(false);
  const [collapsedProjects, setCollapsedProjects] = useState<Record<number, boolean>>(loadCollapsedProjects);
  const [projectLimits, setProjectLimits] = useState<Record<number, number>>({});
  const [renaming, setRenaming] = useState<{ id: number; value: string } | null>(null);
  /** plan-156-620：折叠了子会话的主会话 id（本地记忆；默认展开） */
  const [collapsedSubs, setCollapsedSubs] = useState<Set<number>>(() => {
    try {
      const raw = JSON.parse(localStorage.getItem(SUBS_COLLAPSED_KEY) || "[]");
      return new Set(Array.isArray(raw) ? raw.filter((x): x is number => typeof x === "number") : []);
    } catch { return new Set(); }
  });
  const toggleSubs = (parentId: number) => {
    setCollapsedSubs((prev) => {
      const next = new Set(prev);
      if (next.has(parentId)) next.delete(parentId); else next.add(parentId);
      try { localStorage.setItem(SUBS_COLLAPSED_KEY, JSON.stringify([...next])); } catch { /* 存储不可用时只在本次生效 */ }
      return next;
    });
  };
  const [menuFor, setMenuFor] = useState<number | null>(null);
  /** plan-156-622：会话菜单的浮层坐标。
   *  旧实现用行内的 position:absolute 菜单，子会话行会被 .sb-subsessions-inner 的
   *  overflow:hidden 整体裁掉（点击后 DOM 里确实有菜单，但屏幕上看不见），
   *  用户反馈就是「三个点点击无反应」。改为 portal 到 body + fixed 定位，不受任何祖先裁切。 */
  const [menuPos, setMenuPos] = useState<{ left: number; top: number } | null>(null);
  const sessionMenuRef = useRef<HTMLDivElement>(null);
  /** plan-156-622：当前悬停的会话行。行内图钉默认 pointer-events:none（防隐形热区误触），
   *  只靠 CSS :hover 放开在快速移入后立即点击时会点空；用 React 状态提前放开，点击能稳定命中。 */
  const [hoveredRow, setHoveredRow] = useState<number | null>(null);
  const [projectMenuFor, setProjectMenuFor] = useState<number | null>(null);
  /** 新建工作树弹窗的目标项目（null = 关闭） */
  const [worktreeFor, setWorktreeFor] = useState<ProjectOut | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<SessionOut | null>(null);
  // plan-41-233：页面态 = nav 落在「拓展/定时任务/设置」等页面（会话页 chat 不算）。
  // 页面态下抑制会话行的聚焦高亮——避免 nav 项与会话行同时「双聚焦」。
  const pageMode = active != null && active !== "chat";

  // ── S1（plan-41-197）：重命名稳定性 ────────────────────────────────
  // 用户反馈“会话运行时点重命名会很快失去焦点，导致重命名失败”。三层根因：
  //   ① onBlur 无条件提交退出——运行期列表高频重写，任何一次被动失焦都会终止编辑；
  //   ② Enter 未判中文输入法合成态（选词回车直接结束）；
  //   ③ 无焦点保活。
  // 对策：失焦后 120ms 自检——焦点已离开且用户没有主动点击别处 → 恢复焦点继续编辑；
  //   用户主动点击别处时才提交。输入值走 ref 读取，规避 onBlur 闭包过期。
  const renameInputRef = useRef<HTMLInputElement>(null);
  const renameValueRef = useRef<{ id: number; value: string } | null>(null);
  const allowRenameBlurRef = useRef(false);
  useEffect(() => { renameValueRef.current = renaming; }, [renaming]);

  /** S1：提交重命名（读 ref 拿最新值；标题未变化则不请求） */
  const commitRename = (id: number, originalTitle: string) => {
    const cur = renameValueRef.current;
    setRenaming(null);
    renameValueRef.current = null;
    const title = (cur && cur.id === id ? cur.value : "").trim();
    if (title && title !== originalTitle) void renameSession(id, title);
  };

  // ── S1：浮窗外点关闭 + Esc 关闭（会话/项目/排序三处菜单共用） ──
  // 此前三处菜单只能再次点击触发按钮关闭，点击空白处无反应
  // （用户要求“所有浮窗，点击空白处应该自动关闭”）。触发按钮自身除外，避免“先关后开”抖动。
  const anyMenuOpen = menuFor !== null || projectMenuFor !== null || sortMenuOpen;
  useEffect(() => {
    if (!anyMenuOpen) return;
    const closeAll = () => { setMenuFor(null); setMenuPos(null); setProjectMenuFor(null); setSortMenuOpen(false); };
    const onDown = (e: PointerEvent) => {
      const el = e.target as HTMLElement | null;
      if (el?.closest(".sb-context-menu, .sb-session-actions, .sb-project-actions, .sb-icon-btn")) return;
      closeAll();
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") closeAll(); };
    document.addEventListener("pointerdown", onDown, true);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onDown, true);
      document.removeEventListener("keydown", onKey);
    };
  }, [anyMenuOpen]);

  // ── S1：重命名编辑期间，仅“用户主动点击别处”才允许 blur 提交 ──
  useEffect(() => {
    if (!renaming) { allowRenameBlurRef.current = false; return; }
    const onDown = (e: PointerEvent) => {
      const el = e.target as HTMLElement | null;
      if (el?.closest(".sb-rename-input")) return;
      allowRenameBlurRef.current = true;
    };
    document.addEventListener("pointerdown", onDown, true);
    return () => document.removeEventListener("pointerdown", onDown, true);
  }, [renaming]);

  /* plan-156-622：菜单挂到 body 后量一次实际尺寸，越出视口就夹回来——
     行可能贴着窗口底部，按行底边展开会把菜单推到屏幕外。 */
  useLayoutEffect(() => {
    if (menuFor === null) return;
    const el = sessionMenuRef.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    const pad = 8;
    let { left, top } = menuPos ?? { left: r.left, top: r.top };
    if (r.right > window.innerWidth - pad) left -= r.right - (window.innerWidth - pad);
    if (r.bottom > window.innerHeight - pad) top -= r.bottom - (window.innerHeight - pad);
    if (left < pad) left = pad;
    if (top < pad) top = pad;
    if (Math.abs(left - r.left) > 0.5 || Math.abs(top - r.top) > 0.5) setMenuPos({ left, top });
    // 只在打开与内容变化时校正；夹取后再渲染不会形成回路（位置已收敛）
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [menuFor]);

  const navItems = useMemo(() => [
    { key: "chat", label: t("sidebar.new_task"), icon: <IxNewChat size={16} />, shortcut: "Ctrl+N" },
    { key: "search", label: t("sidebar.search"), icon: <IxSearch size={16} />, shortcut: "Ctrl+K" },
    { key: "scheduled", label: t("sidebar.automation"), icon: <IxAutomation size={16} /> },
    // plan-282-1441（#6）：原「技能」入口改为「拓展」（插件/技能/连接器三合一）
    { key: "skills", label: t("sidebar.extensions"), icon: <IxExtensions size={16} /> },
  ], [t]);

  // Ctrl+N 新建任务由 App 统一处理（chatcoder:new-task），侧栏不再自行监听，避免设置页等场景下失效或重复触发
  const openCommandCenter = () => {
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "k", ctrlKey: true, bubbles: true, cancelable: true }));
  };

  useEffect(() => {
    try { localStorage.setItem("chatcoder:sidebar-view", view); } catch { /* ignore */ }
  }, [view]);

  useEffect(() => {
    try { localStorage.setItem("chatcoder:sidebar-sort", sort); } catch { /* ignore */ }
  }, [sort]);

  /* plan-156-620：子会话桶由 store.loadSubsessionIndex 一次性重建（启动 + subsession.created），
     不再按会话逐个请求——旧做法每次进侧栏发上百个 /subsessions，拖慢全部接口。 */

  /** 主项目（排除工作树——工作树作为其父项目下的独立工作区单独渲染） */
  const visibleProjects = useMemo(() => {
    return projects.filter((p) => !p.archived && !p.is_worktree);
  }, [projects]);

  /** plan-282-1441（#5）：工作树按父项目分组（parent_project_id → 工作树列表） */
  const worktreesByParent = useMemo(() => {
    const m = new Map<number, ProjectOut[]>();
    for (const p of projects) {
      if (p.archived || !p.is_worktree) continue;
      const key = p.parent_project_id ?? 0;
      const arr = m.get(key) ?? [];
      arr.push(p);
      m.set(key, arr);
    }
    return m;
  }, [projects]);

  /** 侧栏「合并到主工作区」目标（弹窗状态） */
  const [mergeWorktree, setMergeWorktree] = useState<ProjectOut | null>(null);
  /** 合并方向：to_main=工作树→主工作区；from_main=主工作区→工作树 */
  const [mergeDirection, setMergeDirection] = useState<"to_main" | "from_main">("to_main");
  const [dropWorktree, setDropWorktree] = useState<ProjectOut | null>(null);

  /** plan-20261016-ux（问题 9）：置顶会话脱离项目，单独放在「置顶」分区（分组/项目切换条之下、项目标题之上）。
   *
   *  两个分区的排序规则一致地以「运行中优先」开头：
   *  - 运行中的会话排在前面，组内按「开始执行时间」倒序（后运行的在上）。不用 last_activity_at——
   *    它随每条流式消息刷新，多个并发任务会互相超车、上下跳动；running_started_at 运行期间恒定，顺序不跳变；
   *  - 置顶分区其余会话按置顶时间倒序（后置顶的在上）；项目 / 任务列表按所选排序（最近 / 名称）。
   *  id 兜底保证任何情况下顺序确定。 */
  const { pinnedSessions, filteredSessions } = useMemo(() => {
    const runKey = (s: SessionOut) => parseUtc(s.running_started_at);
    const byRunning = (a: SessionOut, b: SessionOut): number | null => {
      const busyA = sessionBusy(a, busyIds);
      const busyB = sessionBusy(b, busyIds);
      if (busyA !== busyB) return busyA ? -1 : 1;
      if (busyA && busyB) {
        const ra = runKey(a);
        const rb = runKey(b);
        return rb !== ra ? rb - ra : b.id - a.id;
      }
      return null;
    };
    const live = sessions.filter((s) => s.status !== "archived");
    /* plan-156-623：子会话与主会话同一套置顶语义——置顶后同样移到「置顶」分区。
       此前只把 sessions 里的主会话放进分区，子会话置顶后原地不动，
       用户点了置顶除了图钉变个色看不到别的变化，自然判定为「没反应」。 */
    const liveChildren = Object.values(subsessionsByParent)
      .flat()
      .filter((c) => c.status !== "archived");
    const pinned = [...live, ...liveChildren].filter((s) => s.pinned).sort((a, b) => {
      const r = byRunning(a, b);
      if (r !== null) return r;
      // pinned_at 缺失（极老数据）时回退最近活动时间
      const pa = parseUtc(a.pinned_at) || parseUtc(a.last_activity_at);
      const pb = parseUtc(b.pinned_at) || parseUtc(b.last_activity_at);
      return pb !== pa ? pb - pa : b.id - a.id;
    });
    const rest = live.filter((s) => !s.pinned).sort((a, b) => {
      const r = byRunning(a, b);
      if (r !== null) return r;
      if (sort === "name") return (a.title || "").localeCompare(b.title || "");
      const ta = parseUtc(a.last_activity_at);
      const tb = parseUtc(b.last_activity_at);
      return tb !== ta ? tb - ta : b.id - a.id;
    });
    return { pinnedSessions: pinned, filteredSessions: rest };
    // 依赖用 busyIds（含当前会话的运行态）——此前写成 wfBusyIds，当前会话开始运行时列表不会重排
  }, [sessions, subsessionsByParent, sort, busyIds]);

  /** 置顶 / 取消置顶：先本地更新（会话立刻移入 / 移出置顶分区），再落库；失败时以服务端为准重新拉取 */
  const togglePin = (s: SessionOut) => {
    const pinned = !s.pinned;
    const name = (s.title || `会话 ${s.id}`).slice(0, 20);
    // plan-156-623：主会话与子会话同一套语义——置顶后都会移到「置顶」分区。
    // 同时给一条 toast：点击后即使列表滚动位置没变（会话本来就在首屏），也有明确反馈。
    notify.success(pinned ? `已置顶「${name}」` : `已取消置顶「${name}」`);
    // plan-156-620：子会话不在 sessions 里，统一走 patchSession（主会话列表或子会话桶）
    useChatStore.setState((st) => patchSession(st, s.id, (x) => ({
      ...x, pinned, pinned_at: pinned ? new Date().toISOString() : null,
    })));
    api.updateSession(s.id, { pinned })
      .then(() => loadBootstrap())
      .catch(() => {
        notify.warning("置顶状态保存失败，已按服务端数据刷新");
        loadBootstrap();
      });
  };

  /** plan-156-622：关闭会话菜单（同时清掉浮层坐标，下次打开按新行位置重算）。 */
  const closeSessionMenu = () => { setMenuFor(null); setMenuPos(null); };

  /** plan-156-622：打开会话菜单。
   *  坐标按触发按钮所在行的右缘对齐、行下方展开；此时先给一个估算值，
   *  菜单挂载后由 useLayoutEffect 量实际尺寸并夹回视口内。 */
  const openSessionMenu = (e: React.MouseEvent<HTMLElement>, id: number) => {
    if (menuFor === id) { closeSessionMenu(); return; }
    const row = (e.currentTarget as HTMLElement).closest(".sb-session") as HTMLElement | null;
    if (!row) return;
    const r = row.getBoundingClientRect();
    setMenuPos({ left: Math.max(8, r.right - 180), top: r.bottom + 2 });
    setMenuFor(id);
  };

  const isProjectOpen = (p: ProjectOut) => {
    if (collapsedProjects[p.id] === true) return false;
    if (collapsedProjects[p.id] === false) return true;
    return p.id === currentProjectId;
  };

  const toggleProject = (id: number) => {
    setCollapsedProjects((prev) => {
      const next = { ...prev };
      const currentlyOpen = prev[id] === false || (prev[id] === undefined && id === currentProjectId);
      if (currentlyOpen) {
        next[id] = true;
      } else {
        next[id] = false;
      }
      saveCollapsedProjects(next);
      return next;
    });
    // plan-234-1171 R5: 折叠时清掉「显示更多」的条数上限——
    // 否则重新展开后仍按之前放大的 limit 渲染，上次展开的内容原样留着，
    // 与「重新展开应回到默认 5 条」的预期不符。
    setProjectLimits((prev) => {
      if (!(id in prev)) return prev;
      const next = { ...prev };
      delete next[id];
      return next;
    });
  };

  /** plan-20261009-ui U1：不再直接弹系统选目录——先打开项目弹窗（名称 / 文件夹 / 图标 / 颜色 / 索引） */
  const handleNewProject = () => openProjectDialog({ mode: "create" });

  const handleNewTaskAt = (projectId: number) => {
    useDraftsStore.getState().patchDraft("home", { projectId });
    useChatStore.setState({ currentProjectId: projectId });
    onChange("chat");
  };

  /** 新建工作树：打开命名弹窗（侧栏悬停入口与右键菜单共用）；创建成功后展开父项目，让新工作树立即可见 */
  const handleCreateWorktree = (p: ProjectOut) => setWorktreeFor(p);
  const onWorktreeCreated = (parent: ProjectOut, name: string, branch: string) => {
    setCollapsedProjects((prev) => ({ ...prev, [parent.id]: false }));
    notify.success(`已创建工作树「${name}」（分支 ${branch}）`);
  };

  /** 删除工作树（未提交变更时后端会拒绝）。
   *  plan-308-1542 需求3-C：后端会级联删除该工作树下所有会话；若被删的正是当前选中
   *  项目/会话，需要清理选中态，否则界面会指向已不存在的项目。 */
  const handleDeleteWorktree = async (wt: ProjectOut, force: boolean) => {
    try {
      const res = await api.deleteWorktreeProject(wt.id, force);
      setDropWorktree(null);
      const store = useChatStore.getState();
      if (res?.detached && store.currentProjectId === wt.id) {
        useChatStore.setState({ currentProjectId: null, currentSessionId: null });
      }
      await loadBootstrap();
      const n = res?.deleted_sessions ?? 0;
      useChatStore.setState({
        error: `已删除工作树「${wt.name}」${n > 0 ? `（并级联删除 ${n} 个会话）` : ""}`,
      });
    } catch (e) {
      useChatStore.setState({ error: String(e) });
    }
  };

  /** plan-156-620：主会话与子会话共用同一套行渲染（悬停跑马灯、置顶、归档、三点菜单完全一致），
   *  子会话只多一级缩进；`child` 时不再向下展开子级，也不提供分支 / 工作树（它们属于主会话）。
   *  `inPinnedSection`：该行渲染在「置顶」分区里——分区标题已表达状态，行内不再常驻钉子；
   *  此时子会话也不再缩进（它已脱离父会话组，缩进会让人误以为上一条是它的父级）。 */
  const renderSessionRow = (s: SessionOut, child: boolean, inPinnedSection = false) => {
    const isCurrent = s.id === currentSessionId && !pageMode;
    const editing = renaming?.id === s.id;
    const busy = sessionBusy(s, busyIds);
    // plan-156-619 修复（用户口径）：置顶入口不再绑定「聚焦」，而是**悬停会话行即出现**，
    // 且始终是同一枚普通图钉（不因已置顶换成别的图标）。
    // 槽位宽度恒定，图标只在 hover 时淡入 —— 标题起点不会因为出现/消失而位移。
    // 运行中优先显示转圈：此时不提供置顶入口，避免「边跑边改排序」造成列表跳动。
    // plan-156-619 S5：主会话下的子会话（不含子会话自身——子项不再向下嵌套，一层扁平）
    // plan-156-623：已置顶的子会话移到「置顶」分区，父组里不再重复显示。
    const children = !child && s.side_parent_id == null
      ? [...(subsessionsByParent[s.id] || [])].filter((c) => !c.pinned).sort((a, b) => a.id - b.id)
      : [];
    // plan-156-620：当前就在某个子会话里时强制展开，避免活动会话被折叠藏起来
    const subsOpen = !collapsedSubs.has(s.id) || children.some((c) => c.id === currentSessionId);
    const childRunning = children.some((c) => sessionBusy(c, busyIds));
    const indent = child && !inPinnedSection;
    return (
      <Fragment key={s.id}>
      <div
        className={`sb-session${indent ? " sb-subsession" : ""}${isCurrent ? " active" : ""}${menuFor === s.id ? " menu-open" : ""}`}
        onPointerEnter={() => setHoveredRow(s.id)}
        onPointerLeave={() => setHoveredRow((cur) => (cur === s.id ? null : cur))}
        onClick={() => { if (editing) return; switchSession(s.id); onSessionFocus(); }}
      >
        {/* plan-156-619 修复：行首固定槽位，悬停时淡入置顶按钮。
            已置顶与未置顶共用同一枚图钉图标（用户明确要求「非置顶图标」），
            状态差异只由颜色（accent）与 tooltip 文案表达，不换图标形状。
            plan-156-623：已置顶的行统一在「置顶」分区，那里不再重复钉子（分区标题已表达状态）。 */}
        <span className="sb-session-slot">
          {busy ? (
            <span className="sb-session-pulse on" aria-hidden="true" />
          ) : (
            <Tooltip content={s.pinned ? t("sidebar.ctx_unpin") : t("sidebar.ctx_pin")} side="top">
              {/* plan-156-622：改用 button——旧实现是 span+role，键盘不可达；
                  is-armed 由行的 pointerenter 驱动，不依赖 CSS :hover 的重算时机（快速移入后点击不会点空）。 */}
              <button
                type="button"
                className={"sb-slot-pin"
                  + (s.pinned ? " is-pinned" : "")
                  + (hoveredRow === s.id ? " is-armed" : "")}
                aria-label={s.pinned ? t("sidebar.ctx_unpin") : t("sidebar.ctx_pin")}
                aria-pressed={s.pinned}
                onClick={(e) => { e.stopPropagation(); togglePin(s); }}
              >
                <IconPin size={13} />
              </button>
            </Tooltip>
          )}
        </span>

        {/* plan-20261016-ux（问题 9）：置顶会话只出现在「置顶」分区，分区标题已说明状态，行内不再重复钉子图标 */}
        {editing ? (
          <Input className="sb-rename-input" ref={renameInputRef} autoFocus value={renaming.value} onFocus={(e) => e.target.select()}
            onChange={(e) => setRenaming({ id: s.id, value: e.target.value })}
            onClick={(e) => e.stopPropagation()}
            onPointerDown={(e) => e.stopPropagation()}
            onBlur={() => {
              const el = renameInputRef.current;
              window.setTimeout(() => {
                if (!el || !document.contains(el)) return;      // 已提交/卸载
                if (document.activeElement === el) return;       // 焦点还在（未真失焦）
                if (allowRenameBlurRef.current) { allowRenameBlurRef.current = false; commitRename(s.id, s.title || ""); return; }
                // 被动失焦（列表刷新/重排）→ 恢复焦点继续编辑，不再让重命名"半途而废"
                el.focus();
                el.select();
              }, 120);
            }}
            onKeyDown={(e) => {
              const native = e.nativeEvent as KeyboardEvent & { isComposing?: boolean };
              // S1：中文输入法选词回车不算提交（isComposing / keyCode 229）
              if (e.key === "Enter" && !native.isComposing && e.keyCode !== 229) {
                e.preventDefault();
                allowRenameBlurRef.current = true;
                commitRename(s.id, s.title || "");
                return;
              }
              if (e.key === "Escape") { e.preventDefault(); setRenaming(null); }
            }} />
        ) : (
          <Tooltip content={s.title || `会话 ${s.id}`}>
          <span className="sb-session-title"
            onMouseEnter={(e) => {
              // S1：标题溢出时 hover 跑马灯浏览全文（测量后写 CSS 变量，不改 state 避免重渲染）
              const el = e.currentTarget;
              const dx = el.scrollWidth - el.clientWidth;
              if (dx <= 2) return;
              el.style.setProperty("--marquee-x", `-${dx}px`);
              el.style.setProperty("--marquee-dur", `${Math.min(6, Math.max(1, dx / 36)).toFixed(2)}s`);
              el.classList.add("is-marquee");
            }}
            onMouseLeave={(e) => e.currentTarget.classList.remove("is-marquee")}
          >
            <span className="sb-title-inner">{s.title || t("sidebar.new_task")}</span>
          </span>
          </Tooltip>
        )}
        {/* plan-156-620：子会话开关——点击折叠 / 展开该会话下的子会话（记忆选择）。
            plan-20261018-sidebar-toggle：由「数量 + 箭头」胶囊改为与行尾归档 / 三点同规格的
            20px 图标按钮——胶囊比图标宽 15px，且与行尾图标相隔 18px（行 gap 8 + 槽位左空 10），
            视觉上明显偏散；数量移入 Tooltip，图标只剩折叠箭头。 */}
        {children.length > 0 && !editing && (
          <Tooltip
            content={`${children.length} 个子会话 · ${subsOpen ? "收起" : "展开"}`}
            side="top"
          >
            <button
              type="button"
              className={"sb-sub-toggle" + (childRunning ? " is-running" : "")}
              aria-expanded={subsOpen}
              aria-label={`${children.length} 个子会话，${subsOpen ? "收起" : "展开"}`}
              onClick={(e) => { e.stopPropagation(); toggleSubs(s.id); }}
            >
              <IconChevronDown size={13} />
            </button>
          </Tooltip>
        )}
        {/* plan-156-619 S1：工作流标记从「标题左侧」移到「标题右侧、时间之前」——
            它此前与运行指示并排，两者同时出现会把标题起点往右推，破坏与项目名的左对齐。 */}
        {s.workflow_state && (
          <span
            className={"sb-session-wf is-" + s.workflow_state}
            aria-label={WORKFLOW_STATE_LABEL[s.workflow_state]}
          >
            <IxWorkflow size={12} />
          </span>
        )}
        {/* plan-156-620：行尾固定宽度槽位——平时显示时间，悬停 / 菜单打开时同一位置换成归档与三点按钮。
            槽位宽度不变，标题与子会话开关不会因悬停而位移（此前时间隐藏、按钮出现会让左侧元素跳动）。 */}
        <span className="sb-session-trail">
          <span className="sb-session-time">{formatRelativeTime(s.last_activity_at, language)}</span>
          {/* v7: 一键归档（三点菜单图标左侧）。运行中的会话禁止归档。
              plan-156-622：改 button，并用 is-armed 跟随行 hover；归档会改数据，
              因此仍保持「看不见不可点」，仅 ⋯ 常驻命中。 */}
          <Tooltip content={sessionBusy(s, busyIds) ? t("sidebar.archive_disabled") : t("sidebar.ctx_archive")} side="top">
            <button
              type="button"
              className={"sb-session-actions sb-session-archive"
                + (sessionBusy(s, busyIds) ? " disabled" : "")
                + (hoveredRow === s.id ? " is-armed" : "")}
              aria-label={t("sidebar.ctx_archive")}
              aria-disabled={sessionBusy(s, busyIds)}
              onClick={(e) => {
                e.stopPropagation();
                if (sessionBusy(s, busyIds)) return;
                api.updateSession(s.id, { status: "archived" }).then(() => loadBootstrap());
              }}
            >
              <IconArchive size={14} />
            </button>
          </Tooltip>
          {/* plan-156-622：⋯ 常驻可点（只靠 opacity 淡入）——它只弹菜单、不改数据，
              常驻命中可以彻底避开「悬停太早/太快→样式没跟上→点空」的时序问题。 */}
          <Tooltip content={t("sidebar.ctx_more")} side="top">
            <button
              type="button"
              className={"sb-session-actions sb-session-more" + (menuFor === s.id ? " is-open" : "")}
              aria-label={t("sidebar.ctx_more")}
              aria-haspopup="menu"
              aria-expanded={menuFor === s.id}
              onClick={(e) => { e.stopPropagation(); openSessionMenu(e, s.id); }}
            >
              <IconMoreHorizontal size={14} />
            </button>
          </Tooltip>
        </span>
        {/* plan-156-622：菜单 portal 到 body——旧实现挂在行内，子会话行会被
            .sb-subsessions-inner 的 overflow:hidden 整体裁掉（DOM 里有菜单、屏幕上看不见），
            用户看到的就是「三个点点击无反应」。 */}
        {menuFor === s.id && createPortal(
          <div
            ref={sessionMenuRef}
            className="context-menu sb-context-menu sb-floating-menu"
            style={menuPos ?? undefined}
            role="menu"
            aria-label={t("sidebar.ctx_more")}
            onClick={closeSessionMenu}
          >
            <div className="context-menu-item" onClick={() => { setRenaming({ id: s.id, value: s.title || "" }); closeSessionMenu(); }}><IconPencil size={13} /> {t("sidebar.ctx_rename")}</div>
            {!child && <div className="context-menu-item" onClick={() => { forkSession(s.id); onSessionFocus(); closeSessionMenu(); }}><IconCopy size={13} /> {t("sidebar.ctx_fork")}</div>}
            <div className="context-menu-item" onClick={() => { togglePin(s); closeSessionMenu(); }}><IconPin size={13} /> {s.pinned ? t("sidebar.ctx_unpin") : t("sidebar.ctx_pin")}</div>
            {!child && <div className="context-menu-item" onClick={() => { api.createWorktree(s.id); closeSessionMenu(); }}><IconGitBranch size={13} /> {t("sidebar.ctx_worktree")}</div>}
            <div
              className={"context-menu-item" + (sessionBusy(s, busyIds) ? " disabled" : "")}
              onClick={() => {
                if (sessionBusy(s, busyIds)) return;
                closeSessionMenu();
                api.updateSession(s.id, { status: "archived" }).then(() => loadBootstrap());
              }}
            ><IconArchive size={13} /> {t("sidebar.ctx_archive")}</div>
            <div className="context-menu-divider" />
            <div className="context-menu-item danger" onClick={() => { setConfirmDelete(s); closeSessionMenu(); }}><IconTrash size={13} /> {t("sidebar.ctx_delete")}</div>
          </div>,
          document.body,
        )}
      </div>
      {/* plan-156-619 S5：父会话下缩进一级列出子会话（一层扁平，子会话不再向下嵌套）。
          几何与普通会话行一致（同一 20px 状态槽），只多一个「子会话」细标识——
          不加色块、不加左侧竖线（项目规则禁止装饰性竖线表达层级）。 */}
      {/* plan-20261019-motion-sweep：展开 / 收起改用统一 Collapse（--dur-3 + emphasized-decel，带淡入淡出），
          此前自带的 grid 过渡只有 --dur-2 的 120ms，视觉上几乎等同瞬切。 */}
      {children.length > 0 && (
        <Collapse open={subsOpen} className="sb-subsessions">
          <div className="sb-subsessions-inner">
            {children.map((c) => renderSessionRow(c, true))}
          </div>
        </Collapse>
      )}
      </Fragment>
    );
  };

  /** 列表入口（可直接传给 `.map`：不能把下标误当成 child 参数） */
  const renderSession = (s: SessionOut) => renderSessionRow(s, false);

  /** 项目行：普通项目与工作树共用同一套结构与样式（问题3——
   * 工作树此前被塞在父项目子层级、额外缩进且名称变灰，与普通项目割裂）。
   * 工作树仅额外带一个「工作树」标识，其余（图标位、+、更多菜单）完全一致。 */
  const renderProjectRow = (p: ProjectOut, isWorktree: boolean) => {
    // S1（plan-41-197）：唯一聚焦——选中会话后其所属项目行不再同时高亮，
    // 只有“未选中任何会话”（首页新建态）时项目才显示 current。
    const isCurrent = p.id === currentProjectId && currentSessionId == null;
    const displayName = p.name || shortPath(p.path);
    // plan-20261017-sbwt：分支名与工作树名相同（最常见）时不再重复显示标签，避免一行里出现两遍同一串文字
    const branchTag = isWorktree && p.worktree_branch && p.worktree_branch !== displayName ? p.worktree_branch : null;
    const menuOpen = projectMenuFor === p.id;
    return (
      <div
        className={`sb-project${isCurrent ? " current" : ""}${isWorktree ? " is-wt" : ""}${menuOpen ? " menu-open" : ""}`}
        onClick={() => toggleProject(p.id)}
      >
        {/* plan-156-619 S1：去掉展开/折叠箭头——整行点击即展开收起，展开态由子项是否出现表达。
            图标套进固定宽度槽（--sb-icon-slot），使项目名与会话标题文本起点严格一致。 */}
        <span className="sb-project-slot">
          {isWorktree ? <IconGitBranch size={14} /> : <ProjectIcon icon={p.icon} color={p.color} size={15} />}
        </span>
        {/* plan-20261017-sbwt：名称与标签放进同一主区，悬停操作以叠加层浮在右侧并淡出主区尾部，
            不再参与排版——此前操作按钮 display:none→flex 会挤压名称与标签造成跳变 */}
        <span className="sb-project-main">
          <Tooltip content={isWorktree ? `${p.worktree_branch || ""} · ${p.path}` : p.path}>
            <span className="sb-project-name">
              {displayName}
            </span>
          </Tooltip>
          {branchTag && (
            <Tooltip content={`分支 ${branchTag}`} side="top">
              <span className="sb-worktree-tag">{branchTag}</span>
            </Tooltip>
          )}
        </span>
        <span className="sb-project-tools">
          {!isWorktree && (
            <Tooltip content="新建工作树" side="top">
              <span
                className="sb-project-actions"
                aria-label="新建工作树"
                onClick={(e) => { e.stopPropagation(); handleCreateWorktree(p); }}
              >
                <IconGitBranch size={12} />
              </span>
            </Tooltip>
          )}
          <Tooltip content={t("sidebar.new_task_at_project")} side="top">
            <span
              className="sb-project-actions"
              aria-label={t("sidebar.new_task_at_project")}
              onClick={(e) => { e.stopPropagation(); handleNewTaskAt(p.id); }}
            >
              <IconPlus size={12} />
            </span>
          </Tooltip>
          <span className="sb-project-actions" onClick={(e) => { e.stopPropagation(); setProjectMenuFor(menuOpen ? null : p.id); }}>
            <IconMoreHorizontal size={13} />
          </span>
        </span>
        {projectMenuFor === p.id && (
          <div className="context-menu sb-context-menu" onClick={() => setProjectMenuFor(null)}>
            {!isWorktree && (
              <div className="context-menu-item" onClick={() => openProjectDialog({ mode: "edit", projectId: p.id })}>
                <IconPencil size={13} /> 编辑项目
              </div>
            )}
            <div className="context-menu-item" onClick={() => window.chatcoderAPI?.openPath?.(p.path)}><IconFolder size={13} /> {t("sidebar.ctx_open_in_folder")}</div>
            {isWorktree ? (
              <>
                <div className="context-menu-item" onClick={() => { setMergeDirection("to_main"); setMergeWorktree(p); }}>
                  <IconGitBranch size={13} /> 合并到主工作区
                </div>
                <div className="context-menu-item" onClick={() => { setMergeDirection("from_main"); setMergeWorktree(p); }}>
                  <IconGitBranch size={13} /> 从主工作区更新
                </div>
                <div className="context-menu-divider" />
                <div className="context-menu-item danger" onClick={() => setDropWorktree(p)}>
                  <IconTrash size={13} /> 删除工作树
                </div>
              </>
            ) : (
              <>
                <div className="context-menu-item" onClick={() => { handleCreateWorktree(p); }}>
                  <IconGitBranch size={13} /> {t("sidebar.ctx_worktree")}
                </div>
                <div className="context-menu-divider" />
                <div className="context-menu-item danger" onClick={() => { api.updateProject(p.id, { archived: true }).then(() => loadBootstrap()); }}><IconArchive size={13} /> {t("sidebar.ctx_archive_project")}</div>
              </>
            )}
          </div>
        )}
      </div>
    );
  };

  /** 项目下的会话列表（普通项目与工作树共用；含「显示更多」分页） */
  const renderProjectChildren = (p: ProjectOut, emptyHint = false) => {
    const projSessions = filteredSessions.filter((s) => s.project_id === p.id);
    const limit = projectLimits[p.id] || SESSION_PAGE_STEP;
    const shown = projSessions.slice(0, limit);
    const remaining = projSessions.length - limit;
    // 达到阈值后按钮语义从「再显示一批」切换为「一次全开」
    const canExpandAll = limit >= SESSION_EXPAND_ALL_THRESHOLD && remaining > 0;
    // plan-20261019-motion-sweep：首批之外的每一批各包一层 Collapse（appear），点「显示更多」时
    // 新增的行从 0 高度平滑展开、下方按钮随之滑动，而不是整块瞬间弹出。批的 key 用序号：
    // 排序变化只换批内的行，不会让已展开的批重播入场。
    const batches: SessionOut[][] = [];
    for (let i = SESSION_PAGE_STEP; i < shown.length; i += SESSION_PAGE_STEP) {
      batches.push(shown.slice(i, i + SESSION_PAGE_STEP));
    }
    return (
      <div className="sb-project-children">
        {shown.slice(0, SESSION_PAGE_STEP).map(renderSession)}
        {batches.map((rows, n) => (
          <Collapse key={`batch-${n}`} open appear className="in-gap">
            <div className="sb-batch-list">{rows.map(renderSession)}</div>
          </Collapse>
        ))}
        {emptyHint && projSessions.length === 0 && (
          <div className="sb-worktree-empty">还没有会话，点 + 开始</div>
        )}
        {/* 剩余 0 项时按钮随同一条曲线收拢；收起期间 Collapse 冻结最后一次的文案，不会闪成「剩余 0 项」 */}
        <Collapse open={remaining > 0} lazy className="in-gap">
          {() => (
            <div className="sb-more-wrapper">
              <button
                type="button"
                className="sb-more-btn"
                onClick={(e) => {
                  e.stopPropagation();
                  const total = projSessions.length;
                  setProjectLimits((prev) => {
                    const cur = prev[p.id] ?? SESSION_PAGE_STEP;
                    // 已达阈值：一次展开全部（slice 会自然收敛到总数，按钮随之收拢）
                    const next = cur >= SESSION_EXPAND_ALL_THRESHOLD ? total : cur + SESSION_PAGE_STEP;
                    return { ...prev, [p.id]: next };
                  });
                }}
              >
                <IconChevronDown size={12} />
                <span>
                  {canExpandAll
                    ? t("sidebar.expand_all", { count: remaining })
                    : t("sidebar.show_more", { count: remaining })}
                </span>
              </button>
            </div>
          )}
        </Collapse>
      </div>
    );
  };

  return (
    <nav className="sidebar sb sb-v2">
      {/* 头部占位：logo / 折叠 / 前进后退由 App 层的 WindowNavCluster 悬浮在此位置，折叠前后坐标恒定 */}
      <div className="sb-head" aria-hidden />

      {/* 主导航 */}
      <div className="sb-nav">
        {navItems.map((item) => (
          <div key={item.key}
            className={`sb-nav-item${item.key === active ? " active" : ""}`}
            onClick={() => (item.key === "search" ? openCommandCenter() : onChange(item.key as NavKey))}
            title={item.label}>
            <span className="sb-nav-icon">{item.icon}</span>
            <span className="sb-nav-label">{item.label}</span>
            {item.shortcut && <kbd className="sb-kbd">{item.shortcut}</kbd>}
          </div>
        ))}
      </div>

      {(
        <>
          {/* 分组|项目切换 + 操作图标 */}
          <div className="sb-viewbar">
            {/* plan-20261009-ui U4：分组 / 项目切换改用统一胶囊分段控件（滑块随选中项平移） */}
            <Segmented
              size="sm"
              value={view}
              onChange={setView}
              aria-label={t("sidebar.view_project")}
              options={[
                { value: "group", label: t("sidebar.view_group"), icon: <IconLayers size={13} /> },
                { value: "project", label: t("sidebar.view_project"), icon: <IconFolder size={13} /> },
              ]}
            />
            <div className="sb-view-actions">
              <Tooltip content={t("sidebar.sort_title")}><button className={"sb-icon-btn" + (sortMenuOpen ? " active" : "")} onClick={() => setSortMenuOpen(!sortMenuOpen)}><IconSortDesc size={14} /></button></Tooltip>
              <Tooltip content={t("sidebar.new_project")}><button className="sb-icon-btn" onClick={handleNewProject}><IconFolderPlus size={14} /></button></Tooltip>
            </div>
            {sortMenuOpen && (
              <div className="context-menu sb-context-menu sb-sort-menu" onClick={() => setSortMenuOpen(false)}>
                <div className={"context-menu-item" + (sort === "recent" ? " active" : "")} onClick={() => setSort("recent")}><IconClock size={13} /> {t("sidebar.sort_recent")}</div>
                <div className={"context-menu-item" + (sort === "name" ? " active" : "")} onClick={() => setSort("name")}><IconSortAlpha size={13} /> {t("sidebar.sort_name")}</div>
              </div>
            )}
          </div>

          {/* 列表区 */}
          <div className="sb-list">
            {/* plan-20261016-ux（问题 9）：置顶分区——在「项目 / 任务」标题之上；置顶会话从项目列表移出。
                plan-156-623：子会话置顶后同样进这里（`child` 只用于限制菜单项，缩进由 inPinnedSection 关掉）。 */}
            {pinnedSessions.length > 0 && (
              <div className="sb-pinned">
                <div className="sb-section-label">{t("sidebar.section_pinned")}</div>
                {pinnedSessions.map((p) => renderSessionRow(p, p.side_parent_id != null, true))}
              </div>
            )}
            {/* plan-20261019-motion-sweep：分组 / 项目切换时内容整体淡入（key=view 让两种视图各自挂载一次）。
                包一层不影响布局：内部仍是自上而下的块流，与此前直接铺在 .sb-list 里一致。 */}
            <div key={view} className="sb-view-pane">
            {view === "project" ? (
              <>
                <div className="sb-section-label">{t("sidebar.section_projects")}</div>
                {visibleProjects.map((p) => (
                  <div key={p.id} className="sb-project-group">
                    {renderProjectRow(p, false)}
                    {/* plan-20261019-motion-sweep：项目展开 / 收起走 Collapse（lazy：收起过渡结束才卸载会话行，
                        children 用函数——收起的项目不构造会话行元素）。此前是 `isProjectOpen && …` 条件渲染，
                        点一下整块瞬间出现 / 消失。 */}
                    <Collapse open={isProjectOpen(p)} lazy>{() => renderProjectChildren(p)}</Collapse>
                    {/* 问题3：工作树与父项目**同级**、紧随其后（不再嵌在父项目子层级），
                        独立折叠不随父项目收起；样式与普通项目完全一致，仅多一个「工作树」标识 */}
                    {(worktreesByParent.get(p.id) ?? []).map((wt) => (
                      <div key={`wt-${wt.id}`} className="sb-project-group sb-worktree-entry">
                        {renderProjectRow(wt, true)}
                        <Collapse open={isProjectOpen(wt)} lazy>{() => renderProjectChildren(wt, true)}</Collapse>
                      </div>
                    ))}
                  </div>
                ))}
                {visibleProjects.length === 0 && (
                  <Empty size="sm" title={t("sidebar.empty_projects")}
                    action={<button className="btn btn-secondary btn-sm" onClick={handleNewProject}><IconPlus size={13} /> {t("sidebar.new_project")}</button>} />
                )}
              </>
            ) : (
              <>
                <div className="sb-section-label">{t("sidebar.section_tasks")}</div>
                {filteredSessions.map(renderSession)}
                {filteredSessions.length === 0 && pinnedSessions.length === 0 && (
                  <Empty size="sm" title={t("sidebar.empty_sessions")}
                    action={<button className="btn btn-secondary btn-sm" onClick={() => onChange("chat")}><IconPlus size={13} /> {t("sidebar.create_first_task")}</button>} />
                )}
              </>
            )}
            </div>
          </div>
        </>
      )}

      <WorktreeCreateDialog
        open={worktreeFor != null}
        subtitle={worktreeFor ? `基于「${worktreeFor.name || worktreeFor.path}」创建` : undefined}
        withName
        explain="在单独的目录里检出新分支，并行尝试方案时不会干扰主工作区；合并回主分支可在侧栏完成。"
        loadRepos={worktreeFor ? () => api.listRepoCandidates(worktreeFor.id) : undefined}
        onClose={() => setWorktreeFor(null)}
        onCreate={async (opts) => {
          const parent = worktreeFor;
          if (!parent) return;
          const res = await api.createWorktreeForProject(parent.id, opts);
          await loadBootstrap();
          onWorktreeCreated(parent, res.name, res.branch);
        }}
      />

      {/* 底部设置入口 */}
      <div className="sb-userbar">
        <Tooltip content={t("cmd.settings")}><button className={`sb-user-settings${active === "settings" ? " active" : ""}`} onClick={() => onChange("settings")}><IxSettings size={16} /><span>{t("cmd.settings")}</span></button></Tooltip>
        <UpdateBadge />
      </div>


      <ConfirmDialog
        open={confirmDelete !== null}
        title={t("common.delete_session_title")}
        message={t("common.delete_session_msg", { title: confirmDelete?.title || t("titlebar.new_task") })}
        confirmLabel={t("common.delete")}
        cancelLabel={t("common.cancel")}
        danger
        onConfirm={async () => { if (confirmDelete) await deleteSession(confirmDelete.id); setConfirmDelete(null); }}
        onCancel={() => setConfirmDelete(null)}
      />

      {/* plan-282-1441（#5）：工作树合并 / 删除 */}
      <MergeDialog
        open={mergeWorktree != null}
        direction={mergeDirection}
        worktree={mergeWorktree ? {
          id: mergeWorktree.id,
          name: mergeWorktree.name,
          path: mergeWorktree.path,
          branch: mergeWorktree.worktree_branch ?? null,
          parent_project_id: mergeWorktree.parent_project_id ?? null,
          parent_path: null,
          dirty: false,
          ahead: 0,
          behind: 0,
        } : null}
        onClose={() => setMergeWorktree(null)}
        onMerged={() => { setMergeWorktree(null); void loadBootstrap(); }}
      />
      <ConfirmDialog
        open={dropWorktree !== null}
        title="删除工作树"
        message={
          `将删除工作树「${dropWorktree?.name ?? ""}」的目录与登记，` +
          `并连带删除本地分支 ${dropWorktree?.worktree_branch || "（工作树分支）"}，此操作不可恢复。`
        }
        confirmLabel="删除"
        cancelLabel={t("common.cancel")}
        danger
        onConfirm={() => { if (dropWorktree) void handleDeleteWorktree(dropWorktree, false); }}
        onCancel={() => setDropWorktree(null)}
      />
    </nav>
  );
}
