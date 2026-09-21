/** 左侧栏（v7 完全对齐 ZCode）：
 * 顶部 logo + 前进/后退
 * 主导航（新建任务 Ctrl+N / 搜索 Ctrl+K / 自动化 / 技能）
 * 分组|项目胶囊切换 + 操作图标（展开/排序/新建项目）
 * 项目→会话两级列表（会话行带相对时间）
 * 底部用户条（设置 + 更新徽标）
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { ProjectOut, SessionOut } from "../api/client";
import { api } from "../api/client";
import { useChatStore } from "../store/chat";
import { useDraftsStore } from "../store/drafts";
import { useUpdaterStore } from "../store/updater";
import { useI18n } from "../store/i18n";
import { formatRelativeTime, parseUtc } from "../utils/time";
import { ConfirmDialog } from "./ConfirmDialog";
import { MergeDialog } from "./worktree/MergeDialog";
import { AppLogo } from "./AppLogo";
import { MarkdownContent } from "./MarkdownContent";
import {
  IconCalendar, IconChevronDown, IconChevronLeft, IconChevronRight, IconPanelLeft,
  IconFolder, IconFolderDynamic, IconLayers, IconSortDesc,
  IconMoreHorizontal, IconPin, IconPlus, IconRefresh, IconSearch, IconSettings,
  IconFolderPlus, IconDownload, IconArchive, IconGitBranch, IconTrash, IconBox,
} from "./icons";

export type NavKey = "chat" | "scheduled" | "skills" | "mcp" | "settings";

interface SidebarProps {
  active: NavKey | null;
  onChange: (key: NavKey) => void;
  onSessionFocus: () => void;
  collapsed: boolean;
  onToggleCollapse: () => void;
}

const STORAGE_KEY_COLLAPSED_PROJECTS = "chatcoder:collapsed-projects";

/** 更新徽标：主进程发现新版本时在设置按钮右侧出现。
 * available 点击开始下载 → downloading 显示环形进度 → downloaded 显示「更新」按钮；
 * hover 弹出该版本 changelog（plan-246-1236 S4）。
 *
 * changelog 弹窗用 Portal + fixed 定位：此前是侧栏内绝对定位，
 * 被 .sidebar 的 overflow:hidden 裁剪、且绘制层级低于右侧面板，
 * 表现为弹窗被中间面板遮挡/只露出一条。
 *
 * plan-283-1428：浮窗可接收鼠标（移入后停留并滚动查看完整说明）。
 * 显隐由 React 态驱动而非 CSS hover，故关闭延迟加长、且浮窗自身的
 * mouseenter 会取消关闭；浮窗外层 .sb-update-notes-hover 的左侧 padding
 * 覆盖浮窗与按钮间的空隙，避免鼠标跨越空隙时误关闭。 */
function UpdateBadge({ collapsed = false }: { collapsed?: boolean }) {
  const { t } = useI18n();
  const status = useUpdaterStore((s) => s.status);
  const downloadUpdate = useUpdaterStore((s) => s.downloadUpdate);
  const installUpdate = useUpdaterStore((s) => s.installUpdate);
  const wrapRef = useRef<HTMLDivElement>(null);
  const closeTimer = useRef<number | null>(null);
  const [anchor, setAnchor] = useState<{ left: number; bottom: number } | null>(null);
  const visible = status.state === "available" || status.state === "downloading" || status.state === "downloaded";

  useEffect(() => () => {
    if (closeTimer.current) window.clearTimeout(closeTimer.current);
  }, []);

  if (!visible) return null;

  const version = "version" in status ? status.version : "";
  const notes = (status.state === "available" || status.state === "downloaded") ? (status.notes || "") : "";
  const showNotes = status.state === "available" || status.state === "downloaded";
  const percent = status.state === "downloading" ? (status.percent ?? 0) : 0;
  const ring = 2 * Math.PI * 7;

  /** 浮窗宽度：与 .sb-update-notes 的 CSS 宽度保持一致；
   * HOVER_PAD 与 .sb-update-notes-hover 的 padding-left 一致，用于覆盖
   * 浮窗与按钮之间的水平空隙（容器左缘贴按钮右缘，避免遮挡按钮热区）。 */
  const NOTES_W = 420;
  const HOVER_PAD = 12;

  const cancelClose = () => {
    if (closeTimer.current) { window.clearTimeout(closeTimer.current); closeTimer.current = null; }
  };

  const openNotes = () => {
    if (!showNotes) return;
    cancelClose();
    const rect = wrapRef.current?.getBoundingClientRect();
    if (!rect) return;
    // 从按钮右缘向右弹出、底部与按钮对齐（向上生长）；右侧空间不足时向左收
    const total = NOTES_W + HOVER_PAD;
    const left = Math.max(8, Math.min(rect.right, window.innerWidth - total - 12));
    setAnchor({ left, bottom: Math.max(8, window.innerHeight - rect.bottom) });
  };
  const scheduleClose = () => {
    cancelClose();
    // 留出从按钮移到浮窗的缓冲时间（浮窗 mouseenter 会取消该定时器）
    closeTimer.current = window.setTimeout(() => { setAnchor(null); closeTimer.current = null; }, 320);
  };

  const popover = showNotes && anchor ? createPortal(
    <div
      className="sb-update-notes-hover"
      style={{ left: anchor.left, bottom: anchor.bottom }}
      onMouseEnter={cancelClose}
      onMouseLeave={scheduleClose}
    >
      <div className="sb-update-notes" role="tooltip">
        <div className="sb-update-notes-head">
          <span className="sb-update-notes-badge">NEW</span>
          <span className="sb-update-notes-ver">{version ? `v${version}` : ""}</span>
          <span className="sb-update-notes-label">{t("sidebar.update_notes")}</span>
        </div>
        {notes
          ? <div className="sb-update-notes-body release-notes"><MarkdownContent>{notes}</MarkdownContent></div>
          : <div className="sb-update-notes-empty">{t("sidebar.update_notes_empty")}</div>}
      </div>
    </div>,
    document.body,
  ) : null;

  const notesHoverProps = { ref: wrapRef, onMouseEnter: openNotes, onMouseLeave: scheduleClose };

  if (status.state === "downloading") {
    return (
      <div className="sb-update-wrap">
        <button
          className="sb-update-btn sb-update-btn-progress"
          title={t("sidebar.downloading_update", { percent })}
          disabled
        >
          <svg className="sb-update-ring" width="18" height="18" viewBox="0 0 18 18" aria-hidden>
            <circle cx="9" cy="9" r="7" fill="none" stroke="rgba(255,255,255,0.28)" strokeWidth="2" />
            <circle
              cx="9" cy="9" r="7" fill="none" stroke="#fff" strokeWidth="2"
              strokeLinecap="round"
              strokeDasharray={ring}
              strokeDashoffset={ring * (1 - percent / 100)}
              transform="rotate(-90 9 9)"
            />
          </svg>
          <span className="sb-update-progress">{percent}%</span>
        </button>
      </div>
    );
  }
  if (status.state === "downloaded") {
    return (
      <div className="sb-update-wrap" {...notesHoverProps}>
        <button
          className="sb-update-restart"
          title={t("sidebar.restart_update_tip", { version: version ?? "" })}
          onClick={() => { void installUpdate(); }}
        >
          {!collapsed && <span>{t("sidebar.restart")}</span>}
          {collapsed && <IconRefresh size={14} color="#fff" />}
        </button>
        {popover}
      </div>
    );
  }
  return (
    <div className="sb-update-wrap" {...notesHoverProps}>
      <button
        className="sb-update-btn"
        title={t("sidebar.download_update_tip", { version: version ?? "" })}
        onClick={() => { void downloadUpdate(); }}
      >
        <IconDownload size={16} />
      </button>
      {popover}
    </div>
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

function shortPath(path: string): string {
  const clean = path.replace(/\\/g, "/").replace(/\/$/, "");
  const parts = clean.split("/");
  return parts[parts.length - 1] || clean;
}

export function Sidebar({ active, onChange, onSessionFocus, collapsed, onToggleCollapse }: SidebarProps) {
  const { t, language } = useI18n();
  const projects = useChatStore((s) => s.projects);
  const sessions = useChatStore((s) => s.sessions);
  const currentSessionId = useChatStore((s) => s.currentSessionId);
  const currentProjectId = useChatStore((s) => s.currentProjectId);
  const loadBootstrap = useChatStore((s) => s.loadBootstrap);
  const switchSession = useChatStore((s) => s.switchSession);
  const createProject = useChatStore((s) => s.createProject);
  const deleteSession = useChatStore((s) => s.deleteSession);
  const renameSession = useChatStore((s) => s.renameSession);
  const forkSession = useChatStore((s) => s.forkSession);
  // 会话前进/后退（与折叠态标题栏共用 store 历史栈）
  const sessionHist = useChatStore((s) => s.sessionHist);
  const sessionHistIdx = useChatStore((s) => s.sessionHistIdx);
  const histGo = useChatStore((s) => s.histGo);
  const canBack = sessionHistIdx > 0;
  const canForward = sessionHistIdx >= 0 && sessionHistIdx < sessionHist.length - 1;

  const [view, setView] = useState<"group" | "project">(() => {
    return (localStorage.getItem("chatcoder:sidebar-view") as "group" | "project") || "project";
  });
  const [sort, setSort] = useState<"recent" | "pinned" | "name">(() => {
    return (localStorage.getItem("chatcoder:sidebar-sort") as "recent" | "pinned" | "name") || "recent";
  });
  const [sortMenuOpen, setSortMenuOpen] = useState(false);
  const [collapsedProjects, setCollapsedProjects] = useState<Record<number, boolean>>(loadCollapsedProjects);
  const [projectLimits, setProjectLimits] = useState<Record<number, number>>({});
  const [renaming, setRenaming] = useState<{ id: number; value: string } | null>(null);
  const [menuFor, setMenuFor] = useState<number | null>(null);
  const [projectMenuFor, setProjectMenuFor] = useState<number | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<SessionOut | null>(null);

  const navItems = useMemo(() => [
    { key: "chat", label: t("sidebar.new_task"), icon: <IconPlus size={16} />, shortcut: "Ctrl+N" },
    { key: "search", label: t("sidebar.search"), icon: <IconSearch size={16} />, shortcut: "Ctrl+K" },
    { key: "scheduled", label: t("sidebar.automation"), icon: <IconCalendar size={16} /> },
    // plan-282-1441（#6）：原「技能」入口改为「拓展」（插件/技能/连接器三合一）
    { key: "skills", label: t("sidebar.extensions"), icon: <IconBox size={16} /> },
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

  const filteredSessions = useMemo(() => {
    const list = sessions.filter((s) => s.status !== "archived");
    // v7: 置顶会话恒在顶部（与所选排序无关）；置顶组内按 pinned_at 倒序（后置顶在上）
    const pinnable = (s: SessionOut) => parseUtc(s.pinned_at) || 0;
    // 运行中任务的排序键：用「开始执行时间」而非 last_activity_at——后者随每条流式消息
    // 刷新，同一项目内多个并发任务会互相超车导致上下跳动。started_at 运行期间恒定。
    // 缺字段（如极短的乐观窗口）不倒回活动时间，否则又会跳；统一用 id 兜底保持确定性。
    const runKey = (s: SessionOut) => parseUtc(s.running_started_at);
    return list.sort((a, b) => {
      if (a.pinned !== b.pinned) return a.pinned ? -1 : 1;
      if (a.pinned && b.pinned) {
        // 后置顶在上；pinned_at 缺失时回退 updated/last_activity
        const pa = pinnable(a) || parseUtc(a.last_activity_at);
        const pb = pinnable(b) || parseUtc(b.last_activity_at);
        if (pb !== pa) return pb - pa;
      }
      // 执行中的任务单独成区：置顶之下、其余会话之上（组内仍按时序）
      if (!!a.has_running !== !!b.has_running) return a.has_running ? -1 : 1;
      if (a.has_running && b.has_running) {
        // 「最新开始执行的在上面」；同刻/缺字段时用 id 兜底，保证顺序确定不抖动
        const ra = runKey(a);
        const rb = runKey(b);
        if (rb !== ra) return rb - ra;
        return b.id - a.id;
      }
      if (sort === "name") {
        return (a.title || "").localeCompare(b.title || "");
      }
      const ta = parseUtc(a.last_activity_at);
      const tb = parseUtc(b.last_activity_at);
      if (tb !== ta) return tb - ta;
      return b.id - a.id;
    });
  }, [sessions, sort]);

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

  const handleNewProject = async () => {
    const dir = await window.chatcoderAPI?.selectDirectory?.();
    if (dir) { await createProject(dir); }
  };

  const handleNewTaskAt = (projectId: number) => {
    useDraftsStore.getState().patchDraft("home", { projectId });
    useChatStore.setState({ currentProjectId: projectId });
    onChange("chat");
  };

  /** plan-282-1441（#5）：一键为项目创建工作树（默认命名），完成后刷新侧栏。 */
  const handleCreateWorktree = async (p: ProjectOut) => {
    try {
      const res = await api.createWorktreeForProject(p.id, {});
      // 展开父项目，让新工作树立即可见
      setCollapsedProjects((prev) => ({ ...prev, [p.id]: false }));
      await loadBootstrap();
      useChatStore.setState({ error: `已创建工作树「${res.name}」（分支 ${res.branch}）` });
    } catch (e) {
      useChatStore.setState({ error: `创建工作树失败：${String(e)}` });
    }
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
    const isCurrent = s.id === currentSessionId;
    return (
      <div key={s.id} className={`sb-session${isCurrent ? " active" : ""}`} onClick={() => { switchSession(s.id); onSessionFocus(); }}>
        {s.pinned && <span title={t("sidebar.ctx_pin")} className="sb-pin"><IconPin size={11} /></span>}
        {renaming?.id === s.id ? (
          <input className="input sb-rename-input" autoFocus value={renaming.value} onFocus={(e) => e.target.select()}
            onChange={(e) => setRenaming({ id: s.id, value: e.target.value })}
            onClick={(e) => e.stopPropagation()}
            onBlur={async () => { const title = renaming.value.trim(); setRenaming(null); if (title && title !== s.title) await renameSession(s.id, title); }}
            onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); if (e.key === "Escape") setRenaming(null); }} />
        ) : (
          <span className="sb-session-title" title={s.title || `会话 ${s.id}`}>{s.title || t("sidebar.new_task")}</span>
        )}
        {s.has_running && <span className="sb-session-pulse" />}
        <span className="sb-session-time">{formatRelativeTime(s.last_activity_at, language)}</span>
        {/* v7: 一键归档（三点菜单图标左侧）。运行中的会话禁止归档 */}
        <span
          className={"sb-session-actions sb-session-archive" + (s.has_running ? " disabled" : "")}
          title={s.has_running ? t("sidebar.archive_disabled") : t("sidebar.ctx_archive")}
          onClick={(e) => {
            e.stopPropagation();
            if (s.has_running) return;
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
            <div className="context-menu-item" onClick={() => { setRenaming({ id: s.id, value: s.title || "" }); setMenuFor(null); }}>{t("sidebar.ctx_rename")}</div>
            <div className="context-menu-item" onClick={() => { forkSession(s.id); onSessionFocus(); setMenuFor(null); }}>{t("sidebar.ctx_fork")}</div>
            <div className="context-menu-item" onClick={() => { api.updateSession(s.id, { pinned: !s.pinned }).then(() => loadBootstrap()); setMenuFor(null); }}>{s.pinned ? t("sidebar.ctx_unpin") : t("sidebar.ctx_pin")}</div>
            <div className="context-menu-item" onClick={() => { api.createWorktree(s.id); setMenuFor(null); }}>{t("sidebar.ctx_worktree")}</div>
            <div
              className={"context-menu-item" + (s.has_running ? " disabled" : "")}
              title={s.has_running ? t("sidebar.archive_disabled") : ""}
              onClick={() => {
                setMenuFor(null);
                if (s.has_running) return;
                api.updateSession(s.id, { status: "archived" }).then(() => loadBootstrap());
              }}
            >{t("sidebar.ctx_archive")}</div>
            <div className="context-menu-divider" />
            <div className="context-menu-item danger" onClick={() => { setConfirmDelete(s); setMenuFor(null); }}>{t("sidebar.ctx_delete")}</div>
          </div>
        )}
      </div>
    );
  };

  /** 项目行：普通项目与工作树共用同一套结构与样式（问题3——
   * 工作树此前被塞在父项目子层级、额外缩进且名称变灰，与普通项目割裂）。
   * 工作树仅额外带一个「工作树」标识，其余（图标位、+、更多菜单）完全一致。 */
  const renderProjectRow = (p: ProjectOut, isWorktree: boolean) => {
    const open = isProjectOpen(p);
    const isCurrent = p.id === currentProjectId;
    return (
      <div className={`sb-project${isCurrent ? " current" : ""}`} onClick={() => toggleProject(p.id)}>
        <span className={`sb-project-chevron${open ? " open" : ""}`} aria-hidden="true"><IconChevronRight size={13} /></span>
        {isWorktree ? <IconGitBranch size={14} /> : <IconFolderDynamic open={open} size={14} />}
        <span className="sb-project-name" title={isWorktree ? `${p.worktree_branch || ""} · ${p.path}` : p.path}>
          {p.name || shortPath(p.path)}
        </span>
        {isWorktree && <span className="sb-worktree-tag">{t("titlebar.worktree")}</span>}
        <span
          className="sb-project-actions sb-project-new"
          title={t("sidebar.new_task_at_project")}
          onClick={(e) => { e.stopPropagation(); handleNewTaskAt(p.id); }}
        >
          <IconPlus size={12} />
        </span>
        <span className="sb-project-actions" onClick={(e) => { e.stopPropagation(); setProjectMenuFor(projectMenuFor === p.id ? null : p.id); }}>
          <IconMoreHorizontal size={13} />
        </span>
        {projectMenuFor === p.id && (
          <div className="context-menu sb-context-menu" onClick={() => setProjectMenuFor(null)}>
            <div className="context-menu-item" onClick={() => window.chatcoderAPI?.openPath?.(p.path)}>{t("sidebar.ctx_open_in_folder")}</div>
            {isWorktree ? (
              <>
                <div className="context-menu-item" onClick={() => { setMergeDirection("to_main"); setMergeWorktree(p); }}>
                  <IconGitBranch size={12} /> 合并到主工作区
                </div>
                <div className="context-menu-item" onClick={() => { setMergeDirection("from_main"); setMergeWorktree(p); }}>
                  <IconGitBranch size={12} /> 从主工作区更新
                </div>
                <div className="context-menu-divider" />
                <div className="context-menu-item danger" onClick={() => setDropWorktree(p)}>
                  <IconTrash size={12} /> 删除工作树
                </div>
              </>
            ) : (
              <>
                <div className="context-menu-item" onClick={() => { void handleCreateWorktree(p); }}>
                  <IconGitBranch size={12} /> {t("sidebar.ctx_worktree")}
                </div>
                <div className="context-menu-divider" />
                <div className="context-menu-item danger" onClick={() => { api.updateProject(p.id, { archived: true }).then(() => loadBootstrap()); }}>{t("sidebar.ctx_archive_project")}</div>
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
    const limit = projectLimits[p.id] || 5;
    const shown = projSessions.slice(0, limit);
    const remaining = projSessions.length - limit;
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
                setProjectLimits((prev) => ({
                  ...prev,
                  [p.id]: (prev[p.id] || 5) + 5,
                }));
              }}
            >
              <IconChevronDown size={12} />
              <span>{t("sidebar.show_more", { count: remaining })}</span>
            </button>
          </div>
        )}
      </div>
    );
  };

  return (
    <nav className={`sidebar sb${collapsed ? " collapsed" : ""}`}>
      {/* 头部：logo + 折叠按钮 + 前进/后退 */}
      <div className="sb-head title-drag-region">
        <AppLogo size={20} className="sb-logo-img" />
        <button className="sb-nav-arrow title-no-drag" onClick={onToggleCollapse} title={collapsed ? t("sidebar.expand_tip") : t("sidebar.collapse_tip")}><IconPanelLeft size={15} open={!collapsed} /></button>
        <button className="sb-nav-arrow title-no-drag" disabled={!canBack} onClick={() => histGo(-1)} title={t("sidebar.history_back")}><IconChevronLeft size={15} /></button>
        <button className="sb-nav-arrow title-no-drag" disabled={!canForward} onClick={() => histGo(1)} title={t("sidebar.history_forward")}><IconChevronRight size={15} /></button>
      </div>

      {/* 主导航 */}
      <div className="sb-nav">
        {navItems.map((item) => (
          <div key={item.key}
            className={`sb-nav-item${item.key === active ? " active" : ""}`}
            onClick={() => (item.key === "search" ? openCommandCenter() : onChange(item.key as NavKey))}
            title={item.label}>
            <span className="sb-nav-icon">{item.icon}</span>
            {!collapsed && <span className="sb-nav-label">{item.label}</span>}
            {!collapsed && item.shortcut && <kbd className="sb-kbd">{item.shortcut}</kbd>}
          </div>
        ))}
      </div>

      {!collapsed && (
        <>
          {/* 分组|项目切换 + 操作图标 */}
          <div className="sb-viewbar">
            <div className="sb-view-switch">
              <button className={view === "group" ? "active" : ""} onClick={() => setView("group")}><IconLayers size={12} /> {t("sidebar.view_group")}</button>
              <button className={view === "project" ? "active" : ""} onClick={() => setView("project")}><IconFolder size={12} /> {t("sidebar.view_project")}</button>
            </div>
            <div className="sb-view-actions">
              <button className={"sb-icon-btn" + (sortMenuOpen ? " active" : "")} title={t("sidebar.sort_title")} onClick={() => setSortMenuOpen(!sortMenuOpen)}><IconSortDesc size={14} /></button>
              <button className="sb-icon-btn" title={t("sidebar.new_project")} onClick={handleNewProject}><IconFolderPlus size={14} /></button>
            </div>
            {sortMenuOpen && (
              <div className="context-menu sb-context-menu sb-sort-menu" onClick={() => setSortMenuOpen(false)}>
                <div className={"context-menu-item" + (sort === "recent" ? " active" : "")} onClick={() => setSort("recent")}>{t("sidebar.sort_recent")}</div>
                <div className={"context-menu-item" + (sort === "pinned" ? " active" : "")} onClick={() => setSort("pinned")}>{t("sidebar.sort_pinned")}</div>
                <div className={"context-menu-item" + (sort === "name" ? " active" : "")} onClick={() => setSort("name")}>{t("sidebar.sort_name")}</div>
              </div>
            )}
          </div>

          {/* 列表区 */}
          <div className="sb-list">
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
                  <div className="sb-empty">
                    <p>{t("sidebar.empty_projects")}</p>
                    <button className="btn btn-ghost btn-sm" onClick={handleNewProject}><IconPlus size={13} /> {t("sidebar.new_project")}</button>
                  </div>
                )}
              </>
            ) : (
              <>
                <div className="sb-section-label">{t("sidebar.section_tasks")}</div>
                {filteredSessions.map(renderSession)}
                {filteredSessions.length === 0 && (
                  <div className="sb-empty">
                    <p>{t("sidebar.empty_sessions")}</p>
                    <button className="btn btn-ghost btn-sm" onClick={() => onChange("chat")}><IconPlus size={13} /> {t("sidebar.create_first_task")}</button>
                  </div>
                )}
              </>
            )}
          </div>
        </>
      )}

      {/* 底部设置入口 */}
      <div className="sb-userbar">
        <button className={`sb-user-settings${active === "settings" ? " active" : ""}`} title={t("cmd.settings")} onClick={() => onChange("settings")}><IconSettings size={16} />{!collapsed && <span>{t("cmd.settings")}</span>}</button>
        <UpdateBadge collapsed={collapsed} />
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
