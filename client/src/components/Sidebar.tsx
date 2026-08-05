/** 左侧栏（v4 r3）：五项统一导航 + 任务列表 + 底部设置。
 * 修复：三点菜单溢出、排序图标无法区分、操作图标无动画。
 */
import { useEffect, useMemo, useState } from "react";
import type { ProjectOut, SessionOut } from "../api/client";
import { api } from "../api/client";
import { useChatStore } from "../store/chat";
import { ConfirmDialog } from "./ConfirmDialog";
import {
 IconCalendar, IconFolder,
  IconClock, IconPin, IconSortAlpha, IconArrowToggle,
 IconGitBranch, IconMessageSquare, IconMoreHorizontal, IconPlus, IconSearch,
  IconSettings, IconX, IconZap, IconPlug,
} from "./icons";

export type NavKey = "chat" | "scheduled" | "skills" | "mcp" | "settings";

interface SidebarProps {
  active: NavKey | null;
  onChange: (key: NavKey) => void;
  onSessionFocus: () => void;
  collapsed: boolean;
  onToggleCollapse: () => void;
}

const NAV_ITEMS: { key: NavKey; label: string; icon: React.ReactNode }[] = [
  { key: "chat", label: "新任务", icon: <IconMessageSquare size={15} /> },
  { key: "scheduled", label: "定时任务", icon: <IconCalendar size={15} /> },
  { key: "skills", label: "技能", icon: <IconZap size={15} /> },
  { key: "mcp", label: "MCP", icon: <IconPlug size={15} /> },
];

function shortPath(path: string): string {
  const clean = path.replace(/\\/g, "/").replace(/\/$/, "");
  const parts = clean.split("/");
  if (parts.length <= 2) return clean;
  return parts[parts.length - 1] || clean;
}

export function Sidebar({ active, onChange, onSessionFocus, collapsed, onToggleCollapse: _onToggleCollapse }: SidebarProps) {
  void _onToggleCollapse;
  const projects = useChatStore((s) => s.projects);
  const sessions = useChatStore((s) => s.sessions);
  const currentSessionId = useChatStore((s) => s.currentSessionId);
  const loadBootstrap = useChatStore((s) => s.loadBootstrap);
  const switchSession = useChatStore((s) => s.switchSession);
  const deleteSession = useChatStore((s) => s.deleteSession);
  const renameSession = useChatStore((s) => s.renameSession);
  const forkSession = useChatStore((s) => s.forkSession);

  const [query, setQuery] = useState("");
  const [searchOpen, setSearchOpen] = useState(false);
  const [view, setView] = useState<"project" | "flat">("project");
 const [sort, setSort] = useState<"pinned" | "recent" | "name">("pinned");
 const [sortMenuOpen, setSortMenuOpen] = useState(false);

/** 排序方式图标 */
function SortIcon({ type }: { type: "pinned" | "recent" | "name" }) {
  if (type === "pinned") return <IconPin size={13} />;
  if (type === "recent") return <IconClock size={13} />;
  return <IconSortAlpha size={13} />;
}
 const [menuFor, setMenuFor] = useState<number | null>(null);
  const [projectMenuFor, setProjectMenuFor] = useState<number | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<SessionOut | null>(null);
  const [renaming, setRenaming] = useState<{ id: number; value: string } | null>(null);
  const [collapsedProjects, setCollapsedProjects] = useState<Record<number, boolean>>({});

  useEffect(() => { loadBootstrap(); }, [loadBootstrap]);

  // 全局弹窗点击外部关闭：排序菜单/会话菜单/项目菜单
  useEffect(() => {
    if (!sortMenuOpen && menuFor === null && projectMenuFor === null) return;
    const handler = (e: MouseEvent) => {
      const target = e.target as Element;
      const withinMenu = target.closest?.(".context-menu") || target.closest?.(".sidebar-sort-btn") || target.closest?.(".sidebar-session-actions") || target.closest?.(".sidebar-project-actions");
      if (!withinMenu) {
        setSortMenuOpen(false);
        setMenuFor(null);
        setProjectMenuFor(null);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [sortMenuOpen, menuFor, projectMenuFor]);

  const visibleProjects = useMemo(() => projects.filter((p) => !p.archived), [projects]);

  const filteredSessions = useMemo(() => {
    let list = sessions.filter((s) => s.status !== "archived");
    if (query.trim()) {
      const q = query.toLowerCase();
      list = list.filter((s) => (s.title || "").toLowerCase().includes(q) || String(s.id).includes(q));
    }
    return [...list].sort((a, b) => {
      if (sort === "name") return (a.title || "").localeCompare(b.title || "") || a.id - b.id;
      if (sort === "recent") return b.id - a.id;
      return Number(b.pinned) - Number(a.pinned) || b.id - a.id;
    });
  }, [sessions, query, sort]);

  const grouped: [ProjectOut | null, SessionOut[]][] = useMemo(() => {
    if (view === "flat") return [[null, filteredSessions]];
    return visibleProjects.map((p) => [p, filteredSessions.filter((s) => s.project_id === p.id)] as [ProjectOut, SessionOut[]]);
  }, [visibleProjects, filteredSessions, view]);

  const toggleProject = (id: number) => setCollapsedProjects((prev) => ({ ...prev, [id]: !prev[id] }));

  const handleOpenInFolder = (project: ProjectOut) => {
    if (window.chatcoderAPI?.showItemInFolder) window.chatcoderAPI.showItemInFolder(project.path);
    else window.chatcoderAPI?.openPath?.(project.path);
  };

  return (
    <nav className={`sidebar${collapsed ? " collapsed" : ""}`}>
      <div className="sidebar-nav">
        {NAV_ITEMS.map((item) => (
          <div key={item.key} className={`sidebar-nav-item${item.key === active ? " active" : ""}`} onClick={() => onChange(item.key)}>
            {item.icon}
            {!collapsed && <span>{item.label}</span>}
          </div>
        ))}
      </div>

      <div className="sidebar-divider" />

      {!collapsed && (
        <>
          <div className="sidebar-tasklist-header">
            <span>任务列表</span>
            <div className="sidebar-tasklist-actions" style={{ position: "relative" }}>
              <button title="搜索" onClick={() => setSearchOpen(!searchOpen)} className={"sidebar-action-btn" + (searchOpen ? " active" : "")}>
                <IconSearch size={13} />
              </button>
              <button title={view === "project" ? "项目分组" : "平铺列表"} onClick={() => setView(view === "project" ? "flat" : "project")} className="sidebar-action-btn">
                {view === "project" ? <IconFolder size={13} /> : <IconMessageSquare size={13} />}
              </button>
              <button title="排序方式" onClick={() => setSortMenuOpen(!sortMenuOpen)} className={"sidebar-action-btn sidebar-sort-btn" + (sortMenuOpen ? " active" : "")}>
                <SortIcon type={sort} />
             </button>
              {sortMenuOpen && (
                <div className="context-menu sidebar-context-menu sidebar-sort-menu" onClick={() => setSortMenuOpen(false)}>
                  <div className={"context-menu-item" + (sort === "pinned" ? " active" : "")} onClick={() => { setSort("pinned"); setSortMenuOpen(false); }}>
                    <SortIcon type="pinned" /> <span>按置顶</span>
                  </div>
                  <div className={"context-menu-item" + (sort === "recent" ? " active" : "")} onClick={() => { setSort("recent"); setSortMenuOpen(false); }}>
                    <SortIcon type="recent" /> <span>按最近</span>
                  </div>
                  <div className={"context-menu-item" + (sort === "name" ? " active" : "")} onClick={() => { setSort("name"); setSortMenuOpen(false); }}>
                    <SortIcon type="name" /> <span>按名称</span>
                  </div>
                </div>
              )}
           </div>
          </div>

          {searchOpen && (
            <div className="sidebar-search">
              <IconSearch size={12} />
              <input placeholder="搜索会话…" value={query} onChange={(e) => setQuery(e.target.value)} autoFocus />
              {query && <button onClick={() => setQuery("")} style={{ opacity: 0.5 }}><IconX size={11} /></button>}
            </div>
          )}

          <div className="sidebar-tasklist">
            {grouped.map(([project, sessionList]) => {
              const isCollapsed = project ? collapsedProjects[project.id] : false;
              const items = isCollapsed ? [] : sessionList;
              return (
                <div key={project?.id ?? "flat"} className="sidebar-project-group">
                  {project && (
                    <div className={`sidebar-project-header${isCollapsed ? " collapsed" : ""}`} onClick={() => toggleProject(project.id)}>
                      <span className="chevron"><IconArrowToggle open={!isCollapsed} size={12} /></span>
                      <IconFolder size={13} />
                      <span title={project.path}>{shortPath(project.path)}</span>
                      <span className="sidebar-project-actions" onClick={(e) => { e.stopPropagation(); setProjectMenuFor(projectMenuFor === project.id ? null : project.id); }}>
                        <IconMoreHorizontal size={13} />
                      </span>
                      {projectMenuFor === project.id && (
                        <div className="context-menu sidebar-context-menu" onClick={() => setProjectMenuFor(null)}>
                          <div className="context-menu-item" onClick={() => handleOpenInFolder(project)}><IconFolder size={12} /> 在文件管理器打开</div>
                          <div className="context-menu-divider" />
                          <div className="context-menu-item danger" onClick={() => { api.updateProject(project.id, { archived: true }).then(() => loadBootstrap()); }}>归档项目</div>
                        </div>
                      )}
                    </div>
                  )}

                  {items.map((s) => {
                    const isCurrent = s.id === currentSessionId;
                    return (
                      <div key={s.id} className={`sidebar-session-item${isCurrent ? " active" : ""}`} onClick={() => { switchSession(s.id); onSessionFocus(); }}>
                        {s.pinned && <span title="置顶" className="sidebar-pin-icon"><IconPin size={11} /></span>}
                        {renaming?.id === s.id ? (
                          <input className="input" style={{ padding: "2px 4px", fontSize: 12 }} autoFocus value={renaming.value} onFocus={(e) => e.target.select()} onChange={(e) => setRenaming({ id: s.id, value: e.target.value })} onClick={(e) => e.stopPropagation()} onBlur={async () => { const title = renaming.value.trim(); setRenaming(null); if (title && title !== s.title) await renameSession(s.id, title); }} onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); if (e.key === "Escape") setRenaming(null); }} />
                        ) : (
                          <span className="title" title={s.title || `会话 ${s.id}`}>{s.title || "新任务"}</span>
                        )}
                        {s.worktree_path && <span title={`工作树: ${s.worktree_path}`}><IconGitBranch size={10} /></span>}
                        {s.has_running && <span className="sidebar-session-pulse" />}
                        <span className="sidebar-session-actions" onClick={(e) => { e.stopPropagation(); setMenuFor(menuFor === s.id ? null : s.id); }}>
                          <IconMoreHorizontal size={13} />
                        </span>
                        {menuFor === s.id && (
                          <div className="context-menu sidebar-context-menu" onClick={() => setMenuFor(null)}>
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
                  })}
                </div>
              );
            })}

            {filteredSessions.length === 0 && (
              <div className="empty-state">
                <p>暂无会话</p>
                <button className="btn btn-ghost btn-sm" onClick={() => onChange("chat")}><IconPlus size={13} /> 创建第一个任务</button>
              </div>
            )}
          </div>
        </>
      )}

      <div className="sidebar-footer">
        <div className={`sidebar-nav-item${active === "settings" ? " active" : ""}`} onClick={() => onChange("settings")}>
          <IconSettings size={15} />
          {!collapsed && <span>设置</span>}
        </div>
      </div>

      <ConfirmDialog open={confirmDelete !== null} title="删除会话" message={`确定删除「${confirmDelete?.title || "会话"}」吗？删除后将归档，不可恢复。`} confirmLabel="删除" danger onConfirm={async () => { if (confirmDelete) await deleteSession(confirmDelete.id); setConfirmDelete(null); }} onCancel={() => setConfirmDelete(null)} />
    </nav>
  );
}
