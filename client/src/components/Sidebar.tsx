/** 左侧栏（v7 完全对齐 ZCode）：
 * 顶部 logo + 前进/后退
 * 主导航（新建任务 Ctrl+N / 搜索 Ctrl+K / 自动化 / 技能）
 * 分组|项目胶囊切换 + 操作图标（展开/排序/新建项目）
 * 项目→会话两级列表（会话行带相对时间）
 * 底部用户条（头像+用户名 / 打开工作区 / 设置）
 */
import { useEffect, useMemo, useState } from "react";
import type { ProjectOut, SessionOut } from "../api/client";
import { api } from "../api/client";
import { useChatStore } from "../store/chat";
import { usePanelStore } from "../store/panel";
import { formatRelativeTime, parseUtc } from "../utils/time";
import { ConfirmDialog } from "./ConfirmDialog";
import {
  IconCalendar, IconChevronLeft, IconChevronRight, IconExpandDiagonal,
  IconFolder, IconHash, IconListFilter,
  IconMoreHorizontal, IconPin, IconPlus, IconSearch, IconSettings,
  IconSquarePlus, IconTerminal, IconZap,
} from "./icons";

export type NavKey = "chat" | "scheduled" | "skills" | "mcp" | "settings";

interface SidebarProps {
  active: NavKey | null;
  onChange: (key: NavKey) => void;
  onSessionFocus: () => void;
  collapsed: boolean;
  onToggleCollapse: () => void;
}

const NAV_ITEMS: { key: NavKey | "search"; label: string; icon: React.ReactNode; shortcut?: string }[] = [
  { key: "chat", label: "新建任务", icon: <IconPlus size={16} />, shortcut: "Ctrl+N" },
  { key: "search", label: "搜索", icon: <IconSearch size={16} />, shortcut: "Ctrl+K" },
  { key: "scheduled", label: "自动化", icon: <IconCalendar size={16} /> },
  { key: "skills", label: "技能", icon: <IconZap size={16} /> },
];

function shortPath(path: string): string {
  const clean = path.replace(/\\/g, "/").replace(/\/$/, "");
  const parts = clean.split("/");
  return parts[parts.length - 1] || clean;
}

export function Sidebar({ active, onChange, onSessionFocus, collapsed, onToggleCollapse }: SidebarProps) {
  const projects = useChatStore((s) => s.projects);
  const sessions = useChatStore((s) => s.sessions);
  const currentSessionId = useChatStore((s) => s.currentSessionId);
  const currentProjectId = useChatStore((s) => s.currentProjectId);
  const loadBootstrap = useChatStore((s) => s.loadBootstrap);
  const switchSession = useChatStore((s) => s.switchSession);
  const selectProject = useChatStore((s) => s.selectProject);
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

  const [view, setView] = useState<"group" | "project">("project");
  const [sort, setSort] = useState<"pinned" | "recent" | "name">("recent");
  const [sortMenuOpen, setSortMenuOpen] = useState(false);
  const [menuFor, setMenuFor] = useState<number | null>(null);
  const [projectMenuFor, setProjectMenuFor] = useState<number | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<SessionOut | null>(null);
  const [renaming, setRenaming] = useState<{ id: number; value: string } | null>(null);
  const [collapsedProjects, setCollapsedProjects] = useState<Record<number, boolean>>({});
  const [username, setUsername] = useState("");

  useEffect(() => { loadBootstrap(); }, [loadBootstrap]);
  useEffect(() => { window.chatcoderAPI?.getUsername?.().then((n) => n && setUsername(n)).catch(() => {}); }, []);

  // Ctrl+N 新建任务（对齐 zcode 快捷键）
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

  /** 搜索项：转发为 Ctrl+K 打开命令中心 */
  const openCommandCenter = () => {
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "k", ctrlKey: true, bubbles: true, cancelable: true }));
  };

  // 全局点击外部关闭弹层
  useEffect(() => {
    if (!sortMenuOpen && menuFor === null && projectMenuFor === null) return;
    const handler = (e: MouseEvent) => {
      const target = e.target as Element;
      const within = target.closest?.(".context-menu") || target.closest?.(".sb-icon-btn") || target.closest?.(".sb-session-actions") || target.closest?.(".sb-project-actions");
      if (!within) { setSortMenuOpen(false); setMenuFor(null); setProjectMenuFor(null); }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [sortMenuOpen, menuFor, projectMenuFor]);

  const visibleProjects = useMemo(() => projects.filter((p) => !p.archived), [projects]);

  const filteredSessions = useMemo(() => {
    const list = sessions.filter((s) => s.status !== "archived");
    return [...list].sort((a, b) => {
      if (sort === "name") return (a.title || "").localeCompare(b.title || "") || a.id - b.id;
      if (sort === "recent") return parseUtc(b.last_activity_at || "") - parseUtc(a.last_activity_at || "") || b.id - a.id;
      return Number(b.pinned) - Number(a.pinned) || b.id - a.id;
    });
  }, [sessions, sort]);

  const isProjectOpen = (p: ProjectOut) => collapsedProjects[p.id] ?? p.id === currentProjectId;
  const toggleProject = (id: number) => setCollapsedProjects((prev) => ({ ...prev, [id]: !(prev[id] ?? id === currentProjectId) }));

  const handleNewProject = async () => {
    const dir = await window.chatcoderAPI?.selectDirectory?.();
    if (dir) { await createProject(dir); }
  };

  const renderSession = (s: SessionOut) => {
    const isCurrent = s.id === currentSessionId;
    return (
      <div key={s.id} className={`sb-session${isCurrent ? " active" : ""}`} onClick={() => { switchSession(s.id); onSessionFocus(); }}>
        {s.pinned && <span title="置顶" className="sb-pin"><IconPin size={11} /></span>}
        {renaming?.id === s.id ? (
          <input className="input sb-rename-input" autoFocus value={renaming.value} onFocus={(e) => e.target.select()}
            onChange={(e) => setRenaming({ id: s.id, value: e.target.value })}
            onClick={(e) => e.stopPropagation()}
            onBlur={async () => { const title = renaming.value.trim(); setRenaming(null); if (title && title !== s.title) await renameSession(s.id, title); }}
            onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); if (e.key === "Escape") setRenaming(null); }} />
        ) : (
          <span className="sb-session-title" title={s.title || `会话 ${s.id}`}>{s.title || "新任务"}</span>
        )}
        {s.has_running && <span className="sb-session-pulse" />}
        <span className="sb-session-time">{formatRelativeTime(s.last_activity_at)}</span>
        <span className="sb-session-actions" onClick={(e) => { e.stopPropagation(); setMenuFor(menuFor === s.id ? null : s.id); }}>
          <IconMoreHorizontal size={14} />
        </span>
        {menuFor === s.id && (
          <div className="context-menu sb-context-menu" onClick={() => setMenuFor(null)}>
            <div className="context-menu-item" onClick={() => { setRenaming({ id: s.id, value: s.title || "" }); setMenuFor(null); }}>重命名</div>
            <div className="context-menu-item" onClick={() => { forkSession(s.id); onSessionFocus(); setMenuFor(null); }}>Fork 分支</div>
            <div className="context-menu-item" onClick={() => { api.updateSession(s.id, { pinned: !s.pinned }).then(() => loadBootstrap()); setMenuFor(null); }}>{s.pinned ? "取消置顶" : "置顶"}</div>
            <div className="context-menu-item" onClick={() => { api.createWorktree(s.id); setMenuFor(null); }}>创建工作树</div>
            <div className="context-menu-item" onClick={() => { api.updateSession(s.id, { status: "archived" }).then(() => loadBootstrap()); setMenuFor(null); }}>归档</div>
            <div className="context-menu-divider" />
            <div className="context-menu-item danger" onClick={() => { setConfirmDelete(s); setMenuFor(null); }}>删除</div>
          </div>
        )}
      </div>
    );
  };

  return (
    <nav className={`sidebar sb${collapsed ? " collapsed" : ""}`}>
      {/* 头部：logo + 前进/后退（全高侧栏的顶行；拖拽区，按钮不拖拽） */}
      <div className="sb-head title-drag-region">
        <span className="sb-logo title-no-drag" title="chatcoder">C</span>
        <button className="sb-nav-arrow title-no-drag" disabled={!canBack} onClick={() => histGo(-1)} title="后退"><IconChevronLeft size={15} /></button>
        <button className="sb-nav-arrow title-no-drag" disabled={!canForward} onClick={() => histGo(1)} title="前进"><IconChevronRight size={15} /></button>
      </div>

      {/* 主导航 */}
      <div className="sb-nav">
        {NAV_ITEMS.map((item) => (
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
              <button className={view === "group" ? "active" : ""} onClick={() => setView("group")}><IconHash size={12} /> 分组</button>
              <button className={view === "project" ? "active" : ""} onClick={() => setView("project")}><IconFolder size={12} /> 项目</button>
            </div>
            <div className="sb-view-actions">
              <button className="sb-icon-btn" title="折叠侧栏" onClick={onToggleCollapse}><IconExpandDiagonal size={14} /></button>
              <button className={"sb-icon-btn" + (sortMenuOpen ? " active" : "")} title="排序方式" onClick={() => setSortMenuOpen(!sortMenuOpen)}><IconListFilter size={14} /></button>
              <button className="sb-icon-btn" title="新建项目" onClick={handleNewProject}><IconSquarePlus size={14} /></button>
            </div>
            {sortMenuOpen && (
              <div className="context-menu sb-context-menu sb-sort-menu" onClick={() => setSortMenuOpen(false)}>
                <div className={"context-menu-item" + (sort === "recent" ? " active" : "")} onClick={() => setSort("recent")}>按最近</div>
                <div className={"context-menu-item" + (sort === "pinned" ? " active" : "")} onClick={() => setSort("pinned")}>按置顶</div>
                <div className={"context-menu-item" + (sort === "name" ? " active" : "")} onClick={() => setSort("name")}>按名称</div>
              </div>
            )}
          </div>

          {/* 列表区 */}
          <div className="sb-list">
            {view === "project" ? (
              <>
                <div className="sb-section-label">项目</div>
                {visibleProjects.map((p) => {
                  const open = isProjectOpen(p);
                  const projSessions = filteredSessions.filter((s) => s.project_id === p.id);
                  const isCurrent = p.id === currentProjectId;
                  return (
                    <div key={p.id} className="sb-project-group">
                      <div className={`sb-project${isCurrent ? " current" : ""}`}
                        onClick={() => { if (!open) toggleProject(p.id); if (!isCurrent) void selectProject(p.id); else toggleProject(p.id); }}>
                        <IconFolder size={14} />
                        <span className="sb-project-name" title={p.path}>{p.name || shortPath(p.path)}</span>
                        <span className="sb-project-actions" onClick={(e) => { e.stopPropagation(); setProjectMenuFor(projectMenuFor === p.id ? null : p.id); }}>
                          <IconMoreHorizontal size={13} />
                        </span>
                        {projectMenuFor === p.id && (
                          <div className="context-menu sb-context-menu" onClick={() => setProjectMenuFor(null)}>
                            <div className="context-menu-item" onClick={() => (window.chatcoderAPI?.showItemInFolder ? window.chatcoderAPI.showItemInFolder(p.path) : window.chatcoderAPI?.openPath?.(p.path))}>在文件管理器打开</div>
                            <div className="context-menu-divider" />
                            <div className="context-menu-item danger" onClick={() => { api.updateProject(p.id, { archived: true }).then(() => loadBootstrap()); }}>归档项目</div>
                          </div>
                        )}
                      </div>
                      {open && projSessions.map(renderSession)}
                    </div>
                  );
                })}
                {visibleProjects.length === 0 && (
                  <div className="sb-empty">
                    <p>暂无项目</p>
                    <button className="btn btn-ghost btn-sm" onClick={handleNewProject}><IconPlus size={13} /> 新建项目</button>
                  </div>
                )}
              </>
            ) : (
              <>
                <div className="sb-section-label">任务</div>
                {filteredSessions.map(renderSession)}
                {filteredSessions.length === 0 && (
                  <div className="sb-empty">
                    <p>暂无会话</p>
                    <button className="btn btn-ghost btn-sm" onClick={() => onChange("chat")}><IconPlus size={13} /> 创建第一个任务</button>
                  </div>
                )}
              </>
            )}
          </div>
        </>
      )}

      {/* 底部用户条 */}
      <div className="sb-userbar">
        <div className="sb-user">
          <span className="sb-avatar">{(username || "U").slice(0, 1).toUpperCase()}</span>
          {!collapsed && <span className="sb-username" title={username}>{username || "用户"}</span>}
        </div>
        {!collapsed && (
          <div className="sb-user-actions">
            <button className="sb-icon-btn" title="打开终端" onClick={() => usePanelStore.getState().openTab("terminal")}><IconTerminal size={14} /></button>
            <button className={`sb-icon-btn${active === "settings" ? " active" : ""}`} title="设置" onClick={() => onChange("settings")}><IconSettings size={14} /></button>
          </div>
        )}
      </div>

      <ConfirmDialog open={confirmDelete !== null} title="删除会话" message={`确定删除「${confirmDelete?.title || "会话"}」吗？删除后将归档，不可恢复。`} confirmLabel="删除" danger
        onConfirm={async () => { if (confirmDelete) await deleteSession(confirmDelete.id); setConfirmDelete(null); }}
        onCancel={() => setConfirmDelete(null)} />
    </nav>
  );
}
