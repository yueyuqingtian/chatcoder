/** 自定义标题栏（v7 对齐 ZCode）：
 * 左：折叠侧栏钮
 * 中：会话标题 + 项目 chip + 「...」会话操作菜单
 * 右：打开工作区(黄文件夹) + 任务卡开关 + 右面板开关 + 窗口控制
 */
import { useEffect, useRef, useState, type CSSProperties } from "react";
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

/** plan-31-152 S5-2：窗口按钮级 no-drag 加固。
 *  用户反馈"只有缩放按钮点击无反应，最小化/关闭正常"——同容器按钮行为不一致，
 *  说明点击在该按钮位置被系统 caption 命中区吞掉（而非 CSS 继承链问题）。
 *  这里给三个按钮各自挂 inline app-region，不依赖任何选择器/继承。 */
const NO_DRAG = { WebkitAppRegion: "no-drag" } as CSSProperties;

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

  /* ── plan-31-151 S3：标题栏改回 -webkit-app-region:drag（系统原生拖拽/双击/右键菜单）──
     自研拖拽（useWindowDrag）已随 frame:false 伪最大化架构一并移除；
     frame:true + titleBarStyle:"hidden" 下系统接管拖拽与双击最大化，玻璃由 DWM 全程合成不丢。 */
  return (
    <div
      className={`titlebar${leftCollapsed ? " left-collapsed" : ""}`}
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
        {/* plan-31-152 S5-2（用户反馈"只有缩放按钮点击无反应，最小化/关闭正常"）：
            三个按钮结构相同，唯独缩放失效 ⇒ 排除 drag 区吞点击（同容器会全吞），
            问题在该按钮自身：要么被系统 caption 命中区吞掉，要么 maximize() 未生效。
            双重加固：① 按钮级 inline no-drag（不依赖 CSS 继承链）；
            ② 点击日志便于核对 IPC 是否到达（主进程 console-message 会写进 main.log）。 */}
        <button
          className="titlebar-btn"
          style={NO_DRAG}
          onClick={() => { console.log("[chatcoder] titlebar: minimize clicked"); winApi?.minimizeWindow?.(); }}
          title={t("titlebar.min")}
          disabled={!winApi}
        >
          <IconMinus size={14} />
        </button>
        <button
          className="titlebar-btn"
          style={NO_DRAG}
          onClick={() => { console.log("[chatcoder] titlebar: maximize toggle clicked"); winApi?.toggleMaximize?.(); }}
          title={t("titlebar.max")}
          disabled={!winApi}
        >
          <IconSquare size={14} />
        </button>
        <button
          className="titlebar-btn titlebar-close"
          style={NO_DRAG}
          onClick={() => { console.log("[chatcoder] titlebar: close clicked"); winApi?.closeWindow?.(); }}
          title={t("titlebar.close")}
          disabled={!winApi}
        >
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
