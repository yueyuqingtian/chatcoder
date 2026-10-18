/** 左侧栏（v7 完全对齐 ZCode）：
 * 顶部 logo + 前进/后退
 * 主导航（新建任务 Ctrl+N / 搜索 Ctrl+K / 自动化 / 技能）
 * 分组|项目胶囊切换 + 操作图标（展开/排序/新建项目）
 * 项目→会话两级列表（会话行带相对时间）
 * 底部用户条（设置 + 更新徽标）
 */
import { notify } from "../store/toast";
import { IxNewChat, IxSearch, IxAutomation, IxExtensions, IxSettings, IxWorkflow } from "./iconkit/StackIcons";
import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import type { ProjectOut, SessionOut } from "../api/client";
import { api } from "../api/client";
import { useChatStore } from "../store/chat";
import { sessionWorkflowRunning, useWorkflowStore } from "../store/workflow";
import { useDraftsStore } from "../store/drafts";
import { useUpdaterStore } from "../store/updater";
import { useI18n } from "../store/i18n";
import { formatRelativeTime, parseUtc } from "../utils/time";
import { ConfirmDialog } from "./ConfirmDialog";
import { MergeDialog } from "./worktree/MergeDialog";
import { WorktreeCreateDialog } from "./worktree/WorktreeCreateDialog";
import { Empty, Input, Segmented, Tooltip } from "./ui";
import { ProjectIcon } from "./project/ProjectIcon";
import { openProjectDialog } from "../store/projectDialog";
import {
  IconChevronDown,
  IconFolder, IconLayers, IconSortDesc, IconPencil,
  IconMoreHorizontal, IconPin, IconPinOff, IconPlus, IconRefresh,
  IconFolderPlus, IconArrowDown, IconCopy, IconClock, IconSortAlpha, IconArchive, IconGitBranch, IconTrash,
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
    return (
      <Tooltip content={t("sidebar.downloading_update", { percent })} side="top">
        <span
          className="sb-upd is-downloading"
          role="progressbar"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={percent}
          aria-label={t("sidebar.downloading_update", { percent })}
          style={{ ["--p" as string]: `${percent}%` }}
        >
          <span className="sb-upd-label">{percent}%</span>
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
  const loadSubsessions = useChatStore((s) => s.loadSubsessions);
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
  const [menuFor, setMenuFor] = useState<number | null>(null);
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
    const closeAll = () => { setMenuFor(null); setProjectMenuFor(null); setSortMenuOpen(false); };
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

  const navItems = useMemo(() => [
    { key: "chat", label: t("sidebar.new_task"), icon: <IxNewChat size={16} />, shortcut: "Ctrl+N" },
    { key: "search", label: t("sidebar.search"), icon: <IxSearch size={16} />, shortcut: "Ctrl+K" },
    { key: "scheduled", label: t("sidebar.automation"), icon: <IxAutomation size={16} /> },
    // plan-282-1441（#6）：原「技能」入口改为「拓展」（插件/技能/连接器三合一）
    { key: "skills", label: t("sidebar.extensions"), icon: <IxExtensions size={16} /> },
  ], [t]);

  // Ctrl+N 新建任务
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "n") {
        e.preventDefault();
        onChange("chat");
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onChange]);

  const openCommandCenter = () => {
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "k", ctrlKey: true, bubbles: true, cancelable: true }));
  };

  useEffect(() => {
    try { localStorage.setItem("chatcoder:sidebar-view", view); } catch { /* ignore */ }
  }, [view]);

  useEffect(() => {
    try { localStorage.setItem("chatcoder:sidebar-sort", sort); } catch { /* ignore */ }
  }, [sort]);

  /** plan-156-619 S5：进入侧栏时补齐子会话。
   *  `list_sessions` 过滤掉 side_parent_id 非空的会话，子会话只能单独拉取；
   *  这里对当前可见会话做一次按需加载（已有桶的跳过，避免重复请求）。 */
  useEffect(() => {
    for (const s of sessions) {
      if (s.side_parent_id != null) continue;
      if (subsessionsByParent[s.id] !== undefined) continue;
      void loadSubsessions(s.id);
    }
  }, [sessions, subsessionsByParent, loadSubsessions]);

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
    const pinned = live.filter((s) => s.pinned).sort((a, b) => {
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
  }, [sessions, sort, busyIds]);

  /** 置顶 / 取消置顶：先本地更新（会话立刻移入 / 移出置顶分区），再落库；失败时以服务端为准重新拉取 */
  const togglePin = (s: SessionOut) => {
    const pinned = !s.pinned;
    useChatStore.setState((st) => ({
      sessions: st.sessions.map((x) => (x.id === s.id
        ? { ...x, pinned, pinned_at: pinned ? new Date().toISOString() : null }
        : x)),
    }));
    api.updateSession(s.id, { pinned }).then(() => loadBootstrap()).catch(() => loadBootstrap());
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

  const renderSession = (s: SessionOut) => {
    const isCurrent = s.id === currentSessionId && !pageMode;
    const editing = renaming?.id === s.id;
    const busy = sessionBusy(s, busyIds);
    // plan-156-619 S1：状态槽互斥优先级——运行中转圈 > 聚焦态置顶图标 > 空。
    // 聚焦 = 当前会话，此时槽内给置顶入口（未运行时）；置顶会话在「置顶」分区同样如此。
    const showPin = !busy && isCurrent;
    // plan-156-619 S5：主会话下的子会话（不含子会话自身——子项不再向下嵌套，一层扁平）
    const children = s.side_parent_id == null ? (subsessionsByParent[s.id] || []) : [];
    return (
      <Fragment key={s.id}>
      <div className={`sb-session${isCurrent ? " active" : ""}`} onClick={() => { if (editing) return; switchSession(s.id); onSessionFocus(); }}>
        {/* plan-156-619 修复：状态槽在「未运行 + 聚焦」时提供置顶入口，并区分两种图标——
            未置顶 → IconPin（点击置顶）、已置顶 → IconPinOff（点击取消置顶）。
            此前两种情况都用同一个 IconPin，置顶后图标外形不变、只是变蓝，
            用户看不出“再点一下是取消”，也无法区分“置顶”与“取消置顶”两个相反动作。 */}
        {showPin ? (
          <Tooltip content={s.pinned ? t("sidebar.ctx_unpin") : t("sidebar.ctx_pin")} side="top">
            <span
              className={"sb-session-slot is-pinnable" + (s.pinned ? " is-pinned" : "")}
              role="button"
              aria-label={s.pinned ? t("sidebar.ctx_unpin") : t("sidebar.ctx_pin")}
              aria-pressed={s.pinned}
              onClick={(e) => { e.stopPropagation(); togglePin(s); }}
            >
              {s.pinned ? <IconPinOff size={13} /> : <IconPin size={13} />}
            </span>
          </Tooltip>
        ) : (
          <span className="sb-session-slot" aria-hidden="true">
            {busy && <span className="sb-session-pulse on" />}
          </span>
        )}
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
          <span className="sb-session-title" title={s.title || `会话 ${s.id}`}
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
        )}
        <span className="sb-session-time">{formatRelativeTime(s.last_activity_at, language)}</span>
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
        {/* v7: 一键归档（三点菜单图标左侧）。运行中的会话禁止归档 */}
        <span
          className={"sb-session-actions sb-session-archive" + (sessionBusy(s, busyIds) ? " disabled" : "")}
          title={sessionBusy(s, busyIds) ? t("sidebar.archive_disabled") : t("sidebar.ctx_archive")}
          onClick={(e) => {
            e.stopPropagation();
            if (sessionBusy(s, busyIds)) return;
            api.updateSession(s.id, { status: "archived" }).then(() => loadBootstrap());
          }}
        >
          <IconArchive size={14} />
        </span>
        <span className="sb-session-actions" onClick={(e) => { e.stopPropagation(); setMenuFor(menuFor === s.id ? null : s.id); }}>
          <IconMoreHorizontal size={14} />
        </span>
        {menuFor === s.id && (
          <div className="context-menu sb-context-menu" onClick={() => setMenuFor(null)}>
            <div className="context-menu-item" onClick={() => { setRenaming({ id: s.id, value: s.title || "" }); setMenuFor(null); }}><IconPencil size={13} /> {t("sidebar.ctx_rename")}</div>
            <div className="context-menu-item" onClick={() => { forkSession(s.id); onSessionFocus(); setMenuFor(null); }}><IconCopy size={13} /> {t("sidebar.ctx_fork")}</div>
            <div className="context-menu-item" onClick={() => { togglePin(s); setMenuFor(null); }}>{s.pinned ? <IconPinOff size={13} /> : <IconPin size={13} />} {s.pinned ? t("sidebar.ctx_unpin") : t("sidebar.ctx_pin")}</div>
            <div className="context-menu-item" onClick={() => { api.createWorktree(s.id); setMenuFor(null); }}><IconGitBranch size={13} /> {t("sidebar.ctx_worktree")}</div>
            <div
              className={"context-menu-item" + (sessionBusy(s, busyIds) ? " disabled" : "")}
              title={sessionBusy(s, busyIds) ? t("sidebar.archive_disabled") : ""}
              onClick={() => {
                setMenuFor(null);
                if (sessionBusy(s, busyIds)) return;
                api.updateSession(s.id, { status: "archived" }).then(() => loadBootstrap());
              }}
            ><IconArchive size={13} /> {t("sidebar.ctx_archive")}</div>
            <div className="context-menu-divider" />
            <div className="context-menu-item danger" onClick={() => { setConfirmDelete(s); setMenuFor(null); }}><IconTrash size={13} /> {t("sidebar.ctx_delete")}</div>
          </div>
        )}
      </div>
      {/* plan-156-619 S5：父会话下缩进一级列出子会话（一层扁平，子会话不再向下嵌套）。
          几何与普通会话行一致（同一 20px 状态槽），只多一个「子会话」细标识——
          不加色块、不加左侧竖线（项目规则禁止装饰性竖线表达层级）。 */}
      {children.length > 0 && (
        <div className="sb-subsessions">
          {children.map((c) => renderSubsessionRow(c))}
        </div>
      )}
      </Fragment>
    );
  };

  /** 子会话行（plan-156-619 S5）：复用会话行的状态槽几何，额外带「子会话」细标识。 */
  const renderSubsessionRow = (c: SessionOut) => {
    const isCurrent = c.id === currentSessionId && !pageMode;
    const busy = sessionBusy(c, busyIds);
    return (
      <div
        key={c.id}
        className={"sb-session sb-subsession" + (isCurrent ? " active" : "")}
        onClick={() => { switchSession(c.id); onSessionFocus(); }}
        title={c.title || `子会话 ${c.id}`}
      >
        <span className="sb-session-slot" aria-hidden="true">
          {busy && <span className="sb-session-pulse on" />}
        </span>
        <span className="sb-session-title">
          <span className="sb-title-inner">{c.title || "子会话"}</span>
        </span>
        <span className="sb-subsession-tag">子会话</span>
      </div>
    );
  };

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
          <span className="sb-project-name" title={isWorktree ? `${p.worktree_branch || ""} · ${p.path}` : p.path}>
            {displayName}
          </span>
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
    return (
      <div className="sb-project-children">
        {shown.map(renderSession)}
        {emptyHint && projSessions.length === 0 && (
          <div className="sb-worktree-empty">还没有会话，点 + 开始</div>
        )}
        {remaining > 0 && (
          <div className="sb-more-wrapper">
            <button
              type="button"
              className="sb-more-btn"
              onClick={(e) => {
                e.stopPropagation();
                const total = projSessions.length;
                setProjectLimits((prev) => {
                  const cur = prev[p.id] ?? SESSION_PAGE_STEP;
                  // 已达阈值：一次展开全部（slice 会自然收敛到总数，按钮随之消失）
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
              <button className={"sb-icon-btn" + (sortMenuOpen ? " active" : "")} title={t("sidebar.sort_title")} onClick={() => setSortMenuOpen(!sortMenuOpen)}><IconSortDesc size={14} /></button>
              <button className="sb-icon-btn" title={t("sidebar.new_project")} onClick={handleNewProject}><IconFolderPlus size={14} /></button>
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
            {/* plan-20261016-ux（问题 9）：置顶分区——在「项目 / 任务」标题之上；置顶会话从项目列表移出 */}
            {pinnedSessions.length > 0 && (
              <div className="sb-pinned">
                <div className="sb-section-label">{t("sidebar.section_pinned")}</div>
                {pinnedSessions.map(renderSession)}
              </div>
            )}
            {view === "project" ? (
              <>
                <div className="sb-section-label">{t("sidebar.section_projects")}</div>
                {visibleProjects.map((p) => (
                  <div key={p.id} className="sb-project-group">
                    {renderProjectRow(p, false)}
                    {isProjectOpen(p) && renderProjectChildren(p)}
                    {/* 问题3：工作树与父项目**同级**、紧随其后（不再嵌在父项目子层级），
                        独立折叠不随父项目收起；样式与普通项目完全一致，仅多一个「工作树」标识 */}
                    {(worktreesByParent.get(p.id) ?? []).map((wt) => (
                      <div key={`wt-${wt.id}`} className="sb-project-group sb-worktree-entry">
                        {renderProjectRow(wt, true)}
                        {isProjectOpen(wt) && renderProjectChildren(wt, true)}
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
        <button className={`sb-user-settings${active === "settings" ? " active" : ""}`} title={t("cmd.settings")} onClick={() => onChange("settings")}><IxSettings size={16} /><span>{t("cmd.settings")}</span></button>
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
