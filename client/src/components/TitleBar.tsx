/** 自定义标题栏（v7 对齐 ZCode；plan-20261025-shell 收进中间卡片）：
 * 左：折叠侧栏钮（全局固定层 WindowNavCluster 悬浮在此）
 * 中：会话标题 + 项目 chip + 「...」会话操作菜单
 * 右：打开工作区(黄文件夹) + 终端；右面板开关与窗口控制已移到全局固定层 WindowControls，
 *     这里只按 shell.css 的 --wc-w 给它留出位置
 */
import { useRef, useState } from "react";
import { api } from "../api/client";
import { useChatStore, useCurrentSession } from "../store/chat";
import { usePanelStore } from "../store/panel";
import { useI18n } from "../store/i18n";
import { ConfirmDialog } from "./ConfirmDialog";
import { Floating, Input, Tooltip } from "./ui";
import { WorktreeCreateDialog } from "./worktree/WorktreeCreateDialog";
import { IconGitBranch as IconGitBranchTb } from "./icons";
import {
  IconFolder,
  IconGitBranch, IconTerminal,
  IconMoreHorizontal,
  IconChevronDown, IconCheck,
  IconBrandExplorer, IconBrandVSCode, IconBrandIdea, IconBrandWindowsTerminal,
  IconPencil, IconArchive, IconTrash,
} from "./icons";

interface TitleBarProps {
  leftCollapsed: boolean;
  /** 设置页态——不展示项目/会话/外部打开/命令行/侧边栏等信息 */
  settings?: boolean;
  /** plan-41-233：页面态（拓展/定时任务/设置等 nav 页）——不展示会话标题与右侧图标组；
   *  左侧 logo/折叠/前进后退属导航控件，**始终保留**（用户反馈：这组不能被页面态隐藏）。 */
  page?: boolean;
  /** plan-20261025-shell：当前页面有没有右面板（新建任务页没有）——没有时不提供打开终端标签的入口 */
  panelAvailable?: boolean;
  /** 兼容插件槽位契约：左侧导航控件已提到 WindowNavCluster、右面板开关已提到 WindowControls，
   *  标题栏不再使用这些回调与状态 */
  rightCollapsed?: boolean;
  onToggleLeft?: () => void;
  onToggleRight?: () => void;
  onSessionFocus?: () => void;
}

export function TitleBar({ leftCollapsed, settings = false, page = false, panelAvailable = true }: TitleBarProps) {
  const { t } = useI18n();
  // plan-41-233：裸态 = 设置页 ∪ 页面态——仅约束「会话标题 / 项目 chip / 右侧图标组」；
  // 左侧 logo/折叠/前进后退不在其内（用户要求保留）。
  const bare = settings || page;
  const currentSessionId = useChatStore((s) => s.currentSessionId);
  const projects = useChatStore((s) => s.projects);
  const currentProjectId = useChatStore((s) => s.currentProjectId);
  const deleteSession = useChatStore((s) => s.deleteSession);
  const renameSession = useChatStore((s) => s.renameSession);
  // plan-156-620：子会话不在主会话列表里，统一走 findSession（否则标题栏显示「新任务」）
  const session = useCurrentSession();
  const project = projects.find((p) => p.id === (session?.project_id ?? currentProjectId));
  const winApi = window.chatcoderAPI;

  const [menuOpen, setMenuOpen] = useState(false);
  /** 会话级独立工作树弹窗（plan-20261017-goal12 项 6） */
  const [wtOpen, setWtOpen] = useState(false);
  const [folderMenuOpen, setFolderMenuOpen] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [renaming, setRenaming] = useState<string | null>(null);
  const [openTarget, setOpenTarget] = useState<string>(() => {
    try { return localStorage.getItem("chatcoder.preferred_open_target") || "explorer"; }
    catch { return "explorer"; }
  });
  /** plan-20261025-popclip：两个菜单改走 ui/Floating（挂到 body + 视口碰撞）——标题栏在中间卡片里，
   *  卡片的 overflow:hidden 会把越过卡片右缘的菜单裁掉（窄窗口下会话「⋯」菜单只剩半截）。
   *  点外部 / Esc 关闭由 Floating 统一处理，触发按钮自身不算外部。 */
  const moreBtnRef = useRef<HTMLButtonElement>(null);
  const folderBtnRef = useRef<HTMLButtonElement>(null);

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
      {/* plan-128-518：logo / 折叠 / 前进后退已提到 App 层的 WindowNavCluster（全局固定，折叠不再换 DOM）；
          折叠态下这里只给它留一段固定宽度，会话标题随中间面板一起左移到它右侧。 */}
      <div className="titlebar-left" aria-hidden />

      <div className="titlebar-workspace">
        {bare ? (
          null
        ) : (
          <>
        {renaming !== null && session ? (
          <Input
            className="titlebar-rename"
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
          <Tooltip content={project?.path}>
            <span className="titlebar-workspace-project">
              <IconFolder size={13} /> {projectName}
            </span>
          </Tooltip>
        )}
        {session?.worktree_path && (
          <Tooltip content={session.worktree_path}>
            <span className="titlebar-workspace-project">
              <IconGitBranch size={13} /> {t("titlebar.worktree")}
            </span>
          </Tooltip>
        )}
        {/* plan-156-621：子会话不提供工作树入口——它跟随主会话的工作区，
            以子会话 id 调 createWorktree 语义未定义（侧栏也只对主会话提供该入口）。 */}
        {session && !session.worktree_path && session.side_parent_id == null && currentSessionId != null && (
          <>
            <button type="button" className="tb-wt-btn" onClick={() => setWtOpen(true)}
              title="在独立工作树中继续本会话，改动只落在该工作树分支上">
              <IconGitBranchTb size={13} />
              <span>工作树</span>
            </button>
            <WorktreeCreateDialog
              open={wtOpen}
              subtitle={`为「${session.title || "当前会话"}」切换到独立工作树`}
              explain="本会话之后的改动只落在该工作树的分支上，主工作区保持不变；需要时可在侧栏合并回主分支。"
              onClose={() => setWtOpen(false)}
              onCreate={async (opts) => {
                await api.createWorktree(currentSessionId, opts.branch);
                await useChatStore.getState().loadBootstrap();
              }}
            />
          </>
        )}
        {session && (
          <div className="titlebar-more">
            <button
              ref={moreBtnRef}
              className="titlebar-btn titlebar-more-btn"
              title={t("titlebar.session_actions")}
              aria-haspopup="menu"
              aria-expanded={menuOpen}
              onClick={(e) => {
                e.stopPropagation();
                setMenuOpen((v) => !v);
              }}
              type="button"
            >
              <IconMoreHorizontal size={16} />
            </button>
            <Floating
              open={menuOpen}
              anchorRef={moreBtnRef}
              onClose={() => setMenuOpen(false)}
              side="bottom"
              align="start"
              gap={4}
              className="titlebar-pop"
              role="menu"
              aria-label={t("titlebar.session_actions")}
            >
              <div onClick={() => setMenuOpen(false)}>
                <div className="context-menu-item" onClick={() => setRenaming(session.title || "")}><IconPencil size={13} /> {t("sidebar.ctx_rename")}</div>
                {/* 归档限制：正在执行任务的会话不允许归档（与侧栏一致）——
                    执行中归档会让运行中的 turn 与它所属的会话被拆散。
                    会话未执行过任务（has_running 为假）时照常可用。 */}
                <div
                  className={"context-menu-item" + (session.has_running ? " disabled" : "")}
                  title={session.has_running ? t("sidebar.archive_disabled") : ""}
                  onClick={() => {
                    setMenuOpen(false);
                    if (session.has_running) return;
                    // plan-20261025-shell（需求 8）：走统一归档动作——归档当前会话后自动回退到上一个会话或新建任务页
                    void useChatStore.getState().archiveSession(session.id);
                  }}
                ><IconArchive size={13} /> {t("sidebar.ctx_archive")}</div>
                <div className="context-menu-divider" />
                <div className="context-menu-item danger" onClick={() => setConfirmDelete(true)}><IconTrash size={13} /> {t("sidebar.ctx_delete")}</div>
              </div>
            </Floating>
          </div>
        )}
          </>
        )}
      </div>

      <div className="titlebar-mid" />

      <div className="titlebar-right">
        {/* 打开工作区下拉菜单 */}
        {!bare && (
        <>
        <div className="titlebar-folder-dropdown-wrap">
          <button
            ref={folderBtnRef}
            className="titlebar-btn titlebar-folder-combo"
            aria-haspopup="menu"
            aria-expanded={folderMenuOpen}
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
          <Floating
            open={folderMenuOpen}
            anchorRef={folderBtnRef}
            onClose={() => setFolderMenuOpen(false)}
            side="bottom"
            align="end"
            gap={4}
            className="titlebar-pop is-folder"
            role="menu"
            aria-label={t("titlebar.open_external_tip")}
          >
            <div onClick={() => setFolderMenuOpen(false)}>
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
          </Floating>
        </div>
        {/* 终端入口移至顶栏右上；新建任务页没有右面板，入口一并隐藏（plan-20261025-shell 需求 7） */}
        {panelAvailable && (
          <button className="titlebar-btn" onClick={() => usePanelStore.getState().openNewTab("terminal")} title={t("titlebar.terminal_btn")}>
            <IconTerminal size={16} />
          </button>
        )}
        </>
        )}
        {/* plan-20261025-shell：右面板开关与「最小化 / 最大化 / 关闭」已移到全局固定层 WindowControls——
            它钉在窗口右上角，右面板收起时压在本标题栏右端（这里由 shell.css 预留 --wc-w），
            展开后压在右面板标签栏右端，面板开合时按钮位置不变。 */}
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
