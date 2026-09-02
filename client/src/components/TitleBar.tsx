/** 自定义标题栏（v7 对齐 ZCode）：
 * 左：折叠侧栏钮
 * 中：会话标题 + 项目 chip + 「...」会话操作菜单
 * 右：打开工作区(黄文件夹) + 任务卡开关 + 右面板开关 + 窗口控制
 */
import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { useChatStore } from "../store/chat";
import { usePanelStore } from "../store/panel";
import { ConfirmDialog } from "./ConfirmDialog";
import {
  IconMinus, IconSquare, IconX, IconFolder,
  IconGitBranch, IconTerminal,
  IconMoreHorizontal, IconPanelLeft, IconPanelRight,
  IconChevronLeft, IconChevronRight, IconChevronDown, IconCheck,
  IconBrandExplorer, IconBrandVSCode, IconBrandIdea, IconBrandWindowsTerminal,
} from "./icons";

interface TitleBarProps {
  leftCollapsed: boolean;
  rightCollapsed: boolean;
  onToggleLeft: () => void;
  onToggleRight: () => void;
}

export function TitleBar({ leftCollapsed, rightCollapsed, onToggleLeft, onToggleRight }: TitleBarProps) {
  const currentSessionId = useChatStore((s) => s.currentSessionId);
  const sessions = useChatStore((s) => s.sessions);
  const projects = useChatStore((s) => s.projects);
  const currentProjectId = useChatStore((s) => s.currentProjectId);
  const loadBootstrap = useChatStore((s) => s.loadBootstrap);
  const deleteSession = useChatStore((s) => s.deleteSession);
  const renameSession = useChatStore((s) => s.renameSession);
  const session = sessions.find((s) => s.id === currentSessionId);
  const project = projects.find((p) => p.id === (session?.project_id ?? currentProjectId));
  const winApi = window.chatcoderAPI;

  const [menuOpen, setMenuOpen] = useState(false);
  const [folderMenuOpen, setFolderMenuOpen] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [renaming, setRenaming] = useState<string | null>(null);
  const [openTarget, setOpenTarget] = useState<string>(() => {
    try { return localStorage.getItem("chatcoder.preferred_open_target") || "explorer"; }
    catch { return "explorer"; }
  });
  const menuRef = useRef<HTMLDivElement>(null);
  const folderMenuRef = useRef<HTMLDivElement>(null);

  // 会话前进/后退历史（共享 store 栈；侧栏展开时在侧栏头部展示，折叠时移到这里）
  const sessionHist = useChatStore((s) => s.sessionHist);
  const sessionHistIdx = useChatStore((s) => s.sessionHistIdx);
  const histGo = useChatStore((s) => s.histGo);
  const canBack = sessionHistIdx > 0;
  const canForward = sessionHistIdx >= 0 && sessionHistIdx < sessionHist.length - 1;

  useEffect(() => {
    if (!menuOpen && !folderMenuOpen) return;
    const handler = (e: MouseEvent) => {
      if (menuOpen && !menuRef.current?.contains(e.target as Node)) setMenuOpen(false);
      if (folderMenuOpen && !folderMenuRef.current?.contains(e.target as Node)) setFolderMenuOpen(false);
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [menuOpen, folderMenuOpen]);

  const projectName = project?.path
    ? project.path.replace(/\\/g, "/").replace(/\/$/, "").split("/").pop() || project.path
    : "";

  const handleOpenInApp = (target: string) => {
    if (!project?.path) return;
    setOpenTarget(target);
    try { localStorage.setItem("chatcoder.preferred_open_target", target); } catch {}
    setFolderMenuOpen(false);

    if (winApi?.openInApp) {
      void winApi.openInApp(target, project.path);
      return;
    }

    // Web 模式兜底
    if (target === "vscode") {
      window.open(`vscode://file/${project.path.replace(/\\/g, "/")}`);
    } else if (target === "idea") {
      window.open(`idea://open?file=${project.path.replace(/\\/g, "/")}`);
    } else if (winApi?.openPath) {
      void winApi.openPath(project.path);
    }
  };

  const renderOpenTargetIcon = (target: string, size = 14) => {
    switch (target) {
      case "vscode": return <IconBrandVSCode size={size} />;
      case "idea": return <IconBrandIdea size={size} />;
      case "terminal": return <IconBrandWindowsTerminal size={size} />;
      case "explorer":
      default:
        return <IconBrandExplorer size={size} />;
    }
  };

  return (
    <div className={`titlebar title-drag-region${leftCollapsed ? " left-collapsed" : ""}`}>
      <div className="titlebar-left title-no-drag">
        {/* 侧栏折叠时：logo 与前进/后退 + 展开按钮移到标题栏左侧（展开态折叠入口在侧栏头部） */}
        {leftCollapsed && (
          <>
            <span className="sb-logo" title="chatcoder">C</span>
            <button className="titlebar-btn collapsed" onClick={onToggleLeft} title="展开侧栏 (Ctrl+B)">
              <IconPanelLeft size={15} />
            </button>
            <button className="sb-nav-arrow" disabled={!canBack} onClick={() => histGo(-1)} title="后退"><IconChevronLeft size={15} /></button>
            <button className="sb-nav-arrow" disabled={!canForward} onClick={() => histGo(1)} title="前进"><IconChevronRight size={15} /></button>
          </>
        )}
      </div>

      <div className="titlebar-workspace title-no-drag">
        {renaming !== null && session ? (
          <input
            className="input titlebar-rename"
            autoFocus
            value={renaming}
            onFocus={(e) => e.target.select()}
            onChange={(e) => setRenaming(e.target.value)}
            onBlur={async () => {
              const title = renaming.trim();
              const id = session.id;
              setRenaming(null);
              if (title && title !== session.title) await renameSession(id, title);
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter") (e.target as HTMLInputElement).blur();
              if (e.key === "Escape") setRenaming(null);
            }}
          />
        ) : (
          <span className="titlebar-workspace-title">{currentSessionId ? (session?.title || "新任务") : "新任务"}</span>
        )}
        {projectName && (
          <span className="titlebar-workspace-project" title={project?.path}>
            <IconFolder size={13} /> {projectName}
          </span>
        )}
        {session?.worktree_path && (
          <span className="titlebar-workspace-project" title={session.worktree_path}>
            <IconGitBranch size={13} /> 工作树
          </span>
        )}
        {session && (
          <div className="titlebar-more" ref={menuRef}>
            <button
              className="titlebar-btn titlebar-more-btn"
              title="会话操作"
              onMouseDown={(e) => e.stopPropagation()}
              onClick={(e) => {
                e.stopPropagation();
                setMenuOpen((v) => !v);
              }}
              type="button"
            >
              <IconMoreHorizontal size={15} />
            </button>
            {menuOpen && (
              <div
                className="context-menu titlebar-menu"
                onMouseDown={(e) => e.stopPropagation()}
                onClick={() => setMenuOpen(false)}
              >
                <div className="context-menu-item" onClick={() => setRenaming(session.title || "")}>重命名</div>
                <div className="context-menu-item" onClick={() => { void api.updateSession(session.id, { status: "archived" }).then(() => loadBootstrap()); }}>归档</div>
                <div className="context-menu-divider" />
                <div className="context-menu-item danger" onClick={() => setConfirmDelete(true)}>删除</div>
              </div>
            )}
          </div>
        )}
      </div>

      <div className="titlebar-mid" />

      <div className="titlebar-right title-no-drag">
        {/* 打开工作区下拉菜单（对齐图二，带品牌图标与当前激活联动） */}
        <div className="titlebar-folder-dropdown-wrap" ref={folderMenuRef}>
          <button
            className="titlebar-btn titlebar-folder-combo"
            onMouseDown={(e) => e.stopPropagation()}
            onClick={(e) => {
              e.stopPropagation();
              setFolderMenuOpen((v) => !v);
            }}
            title="在外部打开项目（支持资源管理器、VS Code、IntelliJ IDEA、Windows 终端）"
            disabled={!project?.path}
            type="button"
          >
            {renderOpenTargetIcon(openTarget, 15)}
            <IconChevronDown size={10} className="titlebar-folder-caret" />
          </button>
          {folderMenuOpen && (
            <div
              className="context-menu titlebar-folder-menu"
              onMouseDown={(e) => e.stopPropagation()}
              onClick={() => setFolderMenuOpen(false)}
            >
              <div className={`context-menu-item titlebar-folder-menu-item${openTarget === "explorer" ? " active" : ""}`} onClick={() => handleOpenInApp("explorer")}>
                <div className="titlebar-folder-menu-item-left">
                  <IconBrandExplorer size={15} />
                  <span>在文件资源管理器中打开</span>
                </div>
                {openTarget === "explorer" && <IconCheck size={13} className="titlebar-folder-menu-check" />}
              </div>
              <div className={`context-menu-item titlebar-folder-menu-item${openTarget === "idea" ? " active" : ""}`} onClick={() => handleOpenInApp("idea")}>
                <div className="titlebar-folder-menu-item-left">
                  <IconBrandIdea size={15} />
                  <span>在 IntelliJ IDEA 中打开</span>
                </div>
                {openTarget === "idea" && <IconCheck size={13} className="titlebar-folder-menu-check" />}
              </div>
              <div className={`context-menu-item titlebar-folder-menu-item${openTarget === "terminal" ? " active" : ""}`} onClick={() => handleOpenInApp("terminal")}>
                <div className="titlebar-folder-menu-item-left">
                  <IconBrandWindowsTerminal size={15} />
                  <span>在 Windows 终端中打开</span>
                </div>
                {openTarget === "terminal" && <IconCheck size={13} className="titlebar-folder-menu-check" />}
              </div>
              <div className={`context-menu-item titlebar-folder-menu-item${openTarget === "vscode" ? " active" : ""}`} onClick={() => handleOpenInApp("vscode")}>
                <div className="titlebar-folder-menu-item-left">
                  <IconBrandVSCode size={15} />
                  <span>在 Visual Studio Code 中打开</span>
                </div>
                {openTarget === "vscode" && <IconCheck size={13} className="titlebar-folder-menu-check" />}
              </div>
            </div>
          )}
        </div>
        {/* v19: 终端入口移至顶栏右上 */}
        <button className="titlebar-btn" onClick={() => usePanelStore.getState().openNewTab("terminal")} title="打开终端 (Ctrl+J)">
          <IconTerminal size={14} />
        </button>
        <button
          className={`app-pane-toggle titlebar-btn${rightCollapsed ? " collapsed" : ""}`}
          onClick={onToggleRight}
          title={rightCollapsed ? "展开任务栏" : "收起任务栏"}
        >
          <IconPanelRight size={14} />
        </button>
        <span className="titlebar-sep" />
        <button className="titlebar-btn" onClick={() => winApi?.minimizeWindow?.()} title="最小化" disabled={!winApi}>
          <IconMinus size={14} />
        </button>
        <button className="titlebar-btn" onClick={() => winApi?.toggleMaximize?.()} title="最大化/还原" disabled={!winApi}>
          <IconSquare size={12} />
        </button>
        <button className="titlebar-btn titlebar-close" onClick={() => winApi?.closeWindow?.()} title="关闭" disabled={!winApi}>
          <IconX size={14} />
        </button>
      </div>

      <ConfirmDialog
        open={confirmDelete}
        title="删除会话"
        message={`确定删除「${session?.title || "会话"}」吗？删除后将归档，不可恢复。`}
        confirmLabel="删除"
        danger
        onConfirm={async () => { if (session) await deleteSession(session.id); setConfirmDelete(false); }}
        onCancel={() => setConfirmDelete(false)}
      />
    </div>
  );
}
