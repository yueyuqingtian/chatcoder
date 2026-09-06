/** 左侧栏（v7 完全对齐 ZCode）：
 * 顶部 logo + 前进/后退
 * 主导航（新建任务 Ctrl+N / 搜索 Ctrl+K / 自动化 / 技能）
 * 分组|项目胶囊切换 + 操作图标（展开/排序/新建项目）
 * 项目→会话两级列表（会话行带相对时间）
 * 底部用户条（设置 + 更新徽标）
 */
import { useEffect, useMemo, useState } from "react";
import type { ProjectOut, SessionOut } from "../api/client";
import { api } from "../api/client";
import { useChatStore } from "../store/chat";
import { useDraftsStore } from "../store/drafts";
import { useUpdaterStore } from "../store/updater";
import { useI18n } from "../store/i18n";
import { formatRelativeTime, parseUtc } from "../utils/time";
import { ConfirmDialog } from "./ConfirmDialog";
import {
  IconCalendar, IconChevronDown, IconChevronLeft, IconChevronRight, IconPanelLeft,
  IconFolder, IconFolderDynamic, IconLayers, IconSortDesc,
  IconMoreHorizontal, IconPin, IconPlus, IconRefresh, IconSearch, IconSettings,
  IconFolderPlus, IconZap, IconDownload,
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
 * available 点击开始下载 → downloading 显示进度 → downloaded 显示绿底白字「重启」按钮，点击退出并安装。 */
function UpdateBadge({ collapsed = false }: { collapsed?: boolean }) {
  const { t } = useI18n();
  const status = useUpdaterStore((s) => s.status);
  const downloadUpdate = useUpdaterStore((s) => s.downloadUpdate);
  const installUpdate = useUpdaterStore((s) => s.installUpdate);
  const visible = status.state === "available" || status.state === "downloading" || status.state === "downloaded";
  if (!visible) return null;
  if (status.state === "downloading") {
    return (
      <button className="sb-update-btn" title={t("sidebar.downloading_update", { percent: status.percent ?? 0 })} disabled>
        <span className="sb-update-progress">{status.percent}%</span>
      </button>
    );
  }
  if (status.state === "downloaded") {
    return (
      <button
        className="sb-update-restart"
        title={t("sidebar.restart_update_tip", { version: status.version ?? "" })}
        onClick={() => { void installUpdate(); }}
      >
        {!collapsed && <span>{t("sidebar.restart")}</span>}
        {collapsed && <IconRefresh size={14} color="#fff" />}
      </button>
    );
  }
  return (
    <button
      className="sb-update-btn"
      title={t("sidebar.download_update_tip", { version: status.version ?? "" })}
      onClick={() => { void downloadUpdate(); }}
    >
      <IconDownload size={15} color="#fff" />
    </button>
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
    { key: "skills", label: t("sidebar.skills"), icon: <IconZap size={16} /> },
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

  const visibleProjects = useMemo(() => {
    return projects.filter((p) => !p.archived);
  }, [projects]);

  const filteredSessions = useMemo(() => {
    const list = sessions.filter((s) => s.status !== "archived");
    return list.sort((a, b) => {
      if (sort === "pinned") {
        if (a.pinned !== b.pinned) return a.pinned ? -1 : 1;
      }
      if (sort === "name") {
        return (a.title || "").localeCompare(b.title || "");
      }
      const ta = parseUtc(a.last_activity_at);
      const tb = parseUtc(b.last_activity_at);
      return tb - ta;
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
        <span className="sb-session-actions" onClick={(e) => { e.stopPropagation(); setMenuFor(menuFor === s.id ? null : s.id); }}>
          <IconMoreHorizontal size={14} />
        </span>
        {menuFor === s.id && (
          <div className="context-menu sb-context-menu" onClick={() => setMenuFor(null)}>
            <div className="context-menu-item" onClick={() => { setRenaming({ id: s.id, value: s.title || "" }); setMenuFor(null); }}>{t("sidebar.ctx_rename")}</div>
            <div className="context-menu-item" onClick={() => { forkSession(s.id); onSessionFocus(); setMenuFor(null); }}>{t("sidebar.ctx_fork")}</div>
            <div className="context-menu-item" onClick={() => { api.updateSession(s.id, { pinned: !s.pinned }).then(() => loadBootstrap()); setMenuFor(null); }}>{s.pinned ? t("sidebar.ctx_unpin") : t("sidebar.ctx_pin")}</div>
            <div className="context-menu-item" onClick={() => { api.createWorktree(s.id); setMenuFor(null); }}>{t("sidebar.ctx_worktree")}</div>
            <div className="context-menu-item" onClick={() => { api.updateSession(s.id, { status: "archived" }).then(() => loadBootstrap()); setMenuFor(null); }}>{t("sidebar.ctx_archive")}</div>
            <div className="context-menu-divider" />
            <div className="context-menu-item danger" onClick={() => { setConfirmDelete(s); setMenuFor(null); }}>{t("sidebar.ctx_delete")}</div>
          </div>
        )}
      </div>
    );
  };

  return (
    <nav className={`sidebar sb${collapsed ? " collapsed" : ""}`}>
      {/* 头部：logo + 折叠按钮 + 前进/后退 */}
      <div className="sb-head title-drag-region">
        <span className="sb-logo title-no-drag" title="chatcoder">C</span>
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
                {visibleProjects.map((p) => {
                  const open = isProjectOpen(p);
                  const projSessions = filteredSessions.filter((s) => s.project_id === p.id);
                  const isCurrent = p.id === currentProjectId;
                  return (
                    <div key={p.id} className="sb-project-group">
                      <div className={`sb-project${isCurrent ? " current" : ""}`}
                        onClick={() => toggleProject(p.id)}>
                        <span className={`sb-project-chevron${open ? " open" : ""}`} aria-hidden="true"><IconChevronRight size={13} /></span>
                        <IconFolderDynamic open={open} size={14} />
                        <span className="sb-project-name" title={p.path}>{p.name || shortPath(p.path)}</span>
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
                            <div className="context-menu-divider" />
                            <div className="context-menu-item danger" onClick={() => { api.updateProject(p.id, { archived: true }).then(() => loadBootstrap()); }}>{t("sidebar.ctx_archive_project")}</div>
                          </div>
                        )}
                      </div>
                      {open && (
                        <div className="sb-project-children">
                          {(() => {
                            const limit = projectLimits[p.id] || 5;
                            const shown = projSessions.slice(0, limit);
                            const remaining = projSessions.length - limit;
                            return (
                              <>
                                {shown.map(renderSession)}
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
                              </>
                            );
                          })()}
                        </div>
                      )}
                    </div>
                  );
                })}
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
    </nav>
  );
}
