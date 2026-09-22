/** 自定义标题栏（v7 对齐 ZCode）：
 * 左：折叠侧栏钮
 * 中：会话标题 + 项目 chip + 「...」会话操作菜单
 * 右：打开工作区(黄文件夹) + 任务卡开关 + 右面板开关 + 窗口控制
 */
import { useEffect, useRef, useState } from "react";
import { useWindowDrag } from "../hooks/useWindowDrag";
import { api } from "../api/client";
import { useChatStore } from "../store/chat";
import { usePanelStore } from "../store/panel";
import { useI18n } from "../store/i18n";
import { ConfirmDialog } from "./ConfirmDialog";
import { AppLogo } from "./AppLogo";
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
  /** 设置页态——不展示项目/会话/外部打开/命令行/侧边栏等信息 */
  settings?: boolean;
  onToggleLeft: () => void;
  onToggleRight: () => void;
}

export function TitleBar({ leftCollapsed, rightCollapsed, settings = false, onToggleLeft, onToggleRight }: TitleBarProps) {
  const { t } = useI18n();
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

  const handleOpenInApp = async (target: string) => {
    if (!project?.path) return;
    setFolderMenuOpen(false);

    // plan-308-1542 需求5：主进程现返回 { ok, launcher?, error? }——
    // 失败必须可见（此前 spawn 失败静默，用户只看到"点了没反应"）。
    if (winApi?.openInApp) {
      try {
        const res = await winApi.openInApp(target, project.path);
        if (res && res.ok === false) {
          useChatStore.setState({
            error: `${t("titlebar.open_failed")}${res.error ? `：${res.error}` : ""}`
              + `（可在菜单中「${t("titlebar.choose_app")}」指定可执行文件）`,
          });
          return;
        }
        // 仅在成功时记忆目标，避免记住一个不可用的目标却无反馈
        setOpenTarget(target);
        try { localStorage.setItem("chatcoder.preferred_open_target", target); } catch {}
      } catch (e) {
        useChatStore.setState({ error: `${t("titlebar.open_failed")}：${String(e)}` });
      }
      return;
    }

    // Web 模式兜底
    setOpenTarget(target);
    try { localStorage.setItem("chatcoder.preferred_open_target", target); } catch {}
    if (target === "vscode") {
      window.open(`vscode://file/${project.path.replace(/\\/g, "/")}`);
    } else if (target === "idea") {
      window.open(`idea://open?file=${project.path.replace(/\\/g, "/")}`);
    } else if (winApi?.openPath) {
      void winApi.openPath(project.path);
    }
  };

  /** plan-308-1542 需求5：手动指定外部应用可执行文件，成功后立即重试打开。 */
  const handleChooseApp = async (target: string) => {
    if (!winApi?.selectApp) return;
    try {
      const res = await winApi.selectApp(target);
      if (res?.ok) {
        await handleOpenInApp(target);
      } else if (res && res.canceled !== true) {
        useChatStore.setState({ error: t("titlebar.choose_app_failed") });
      }
    } catch (e) {
      useChatStore.setState({ error: String(e) });
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

  /* ── plan-26-126 P2：自研拖拽 + 双击伪全屏（详见 hooks/useWindowDrag.ts）──
     不再用 `-webkit-app-region: drag`：该区域由系统处理拖拽/双击，DOM 拿不到 dblclick，
     双击会直接触发系统原生最大化（重建 DWM 图层 ⇒ 玻璃丢失且不保证恢复）。
     本轮修复：双击判定改由 hook 内部按 pointerup 自研——原生 dblclick 的 target 会被
     提升到两次点击的共同祖先，导致「点折叠按钮 + 点旁边空白」误触发伪最大化（小窗变全屏）。 */
  const { onPointerDown, onPointerMove, onPointerUp } = useWindowDrag();

  return (
    <div
      className={`titlebar${leftCollapsed ? " left-collapsed" : ""}`}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
    >
      <div className="titlebar-left">
        {/* 侧栏折叠时：logo 与前进/后退 + 展开按钮移到标题栏左侧（展开态折叠入口在侧栏头部） */}
        {leftCollapsed && (
          <>
            <AppLogo size={20} className="sb-logo-img" />
            {/* plan-234-1171 R6: 传 open 让图标随状态变形态（展开=实心块 / 折叠=虚线空框），
                此前未传导致图标在面板开合后外观毫无变化。 */}
            <button className="titlebar-btn collapsed" onClick={onToggleLeft} title={t("sidebar.expand_tip")}>
              <IconPanelLeft size={15} open={false} />
            </button>
            <button className="sb-nav-arrow" disabled={!canBack} onClick={() => histGo(-1)} title={t("sidebar.history_back")}><IconChevronLeft size={15} /></button>
            <button className="sb-nav-arrow" disabled={!canForward} onClick={() => histGo(1)} title={t("sidebar.history_forward")}><IconChevronRight size={15} /></button>
          </>
        )}
      </div>

      <div className="titlebar-workspace">
        {settings ? (
          null
        ) : (
          <>
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
          <span className="titlebar-workspace-title">{currentSessionId ? (session?.title || t("titlebar.new_task")) : t("titlebar.new_task")}</span>
        )}
        {projectName && (
          <span className="titlebar-workspace-project" title={project?.path}>
            <IconFolder size={13} /> {projectName}
          </span>
        )}
        {session?.worktree_path && (
          <span className="titlebar-workspace-project" title={session.worktree_path}>
            <IconGitBranch size={13} /> {t("titlebar.worktree")}
          </span>
        )}
        {session && (
          <div className="titlebar-more" ref={menuRef}>
            <button
              className="titlebar-btn titlebar-more-btn"
              title={t("titlebar.session_actions")}
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
                <div className="context-menu-item" onClick={() => setRenaming(session.title || "")}>{t("sidebar.ctx_rename")}</div>
                {/* 归档限制：正在执行任务的会话不允许归档（与侧栏一致）——
                    执行中归档会让运行中的 turn 与它所属的会话被拆散。
                    会话未执行过任务（has_running 为假）时照常可用。 */}
                <div
                  className={"context-menu-item" + (session.has_running ? " disabled" : "")}
                  title={session.has_running ? t("sidebar.archive_disabled") : ""}
                  onClick={() => {
                    setMenuOpen(false);
                    if (session.has_running) return;
                    void api.updateSession(session.id, { status: "archived" }).then(() => loadBootstrap());
                  }}
                >{t("sidebar.ctx_archive")}</div>
                <div className="context-menu-divider" />
                <div className="context-menu-item danger" onClick={() => setConfirmDelete(true)}>{t("sidebar.ctx_delete")}</div>
              </div>
            )}
          </div>
        )}
          </>
        )}
      </div>

      <div className="titlebar-mid" />

      <div className="titlebar-right">
        {/* 打开工作区下拉菜单 */}
        {!settings && (
        <>
        <div className="titlebar-folder-dropdown-wrap" ref={folderMenuRef}>
          <button
            className="titlebar-btn titlebar-folder-combo"
            onMouseDown={(e) => e.stopPropagation()}
            onClick={(e) => {
              e.stopPropagation();
              setFolderMenuOpen((v) => !v);
            }}
            title={t("titlebar.open_external_tip")}
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
                  <span>{t("titlebar.open_explorer")}</span>
                </div>
                {openTarget === "explorer" && <IconCheck size={13} className="titlebar-folder-menu-check" />}
              </div>
              <div className={`context-menu-item titlebar-folder-menu-item${openTarget === "idea" ? " active" : ""}`} onClick={() => void handleOpenInApp("idea")}>
                <div className="titlebar-folder-menu-item-left">
                  <IconBrandIdea size={15} />
                  <span>{t("titlebar.open_idea")}</span>
                </div>
                {openTarget === "idea" && <IconCheck size={13} className="titlebar-folder-menu-check" />}
              </div>
              {winApi?.selectApp && (
                <div className="context-menu-item titlebar-folder-menu-item" onClick={() => void handleChooseApp("idea")}>
                  <div className="titlebar-folder-menu-item-left">
                    <IconFolder size={15} />
                    <span>{t("titlebar.choose_idea")}</span>
                  </div>
                </div>
              )}
              <div className={`context-menu-item titlebar-folder-menu-item${openTarget === "terminal" ? " active" : ""}`} onClick={() => handleOpenInApp("terminal")}>
                <div className="titlebar-folder-menu-item-left">
                  <IconBrandWindowsTerminal size={15} />
                  <span>{t("titlebar.open_terminal")}</span>
                </div>
                {openTarget === "terminal" && <IconCheck size={13} className="titlebar-folder-menu-check" />}
              </div>
              <div className={`context-menu-item titlebar-folder-menu-item${openTarget === "vscode" ? " active" : ""}`} onClick={() => void handleOpenInApp("vscode")}>
                <div className="titlebar-folder-menu-item-left">
                  <IconBrandVSCode size={15} />
                  <span>{t("titlebar.open_vscode")}</span>
                </div>
                {openTarget === "vscode" && <IconCheck size={13} className="titlebar-folder-menu-check" />}
              </div>
              {winApi?.selectApp && (
                <div className="context-menu-item titlebar-folder-menu-item" onClick={() => void handleChooseApp("vscode")}>
                  <div className="titlebar-folder-menu-item-left">
                    <IconFolder size={15} />
                    <span>{t("titlebar.choose_vscode")}</span>
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
        {/* 终端入口移至顶栏右上 */}
        <button className="titlebar-btn" onClick={() => usePanelStore.getState().openNewTab("terminal")} title={t("titlebar.terminal_btn")}>
          <IconTerminal size={14} />
        </button>
        </>
        )}
        {!settings && (
        <button
          className={`app-pane-toggle titlebar-btn${rightCollapsed ? " collapsed" : ""}`}
          onClick={onToggleRight}
          title={rightCollapsed ? t("titlebar.panel_expand") : t("titlebar.panel_collapse")}
        >
          {/* plan-234-1171 R6: 传 open 让图标随右面板开合变形态 */}
          <IconPanelRight size={14} open={!rightCollapsed} />
        </button>
        )}
        <span className="titlebar-sep" />
        <button className="titlebar-btn" onClick={() => winApi?.minimizeWindow?.()} title={t("titlebar.min")} disabled={!winApi}>
          <IconMinus size={14} />
        </button>
        <button className="titlebar-btn" onClick={() => winApi?.toggleMaximize?.()} title={t("titlebar.max")} disabled={!winApi}>
          <IconSquare size={12} />
        </button>
        <button className="titlebar-btn titlebar-close" onClick={() => winApi?.closeWindow?.()} title={t("titlebar.close")} disabled={!winApi}>
          <IconX size={14} />
        </button>
      </div>

      <ConfirmDialog
        open={confirmDelete}
        title={t("common.delete_session_title")}
        message={t("common.delete_session_msg", { title: session?.title || t("titlebar.new_task") })}
        confirmLabel={t("common.delete")}
        cancelLabel={t("common.cancel")}
        danger
        onConfirm={async () => { if (session) await deleteSession(session.id); setConfirmDelete(false); }}
        onCancel={() => setConfirmDelete(false)}
      />
    </div>
  );
}
