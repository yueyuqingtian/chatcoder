/** 应用根（v5）：三栏骨架 + 右面板最大宽度限制 + 全屏模式。 */
import { useEffect, useRef, useState } from "react";
import { TitleBar } from "./components/TitleBar";
import { Sidebar, type NavKey } from "./components/Sidebar";
import { Workspace } from "./components/Workspace";
import { RollbackConfirmModal } from "./components/chat/RollbackConfirmModal";
import { ResizeHandle } from "./components/ResizeHandle";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { Toast } from "./components/Toast";
import { Splash } from "./components/Splash";
import { SettingsPage } from "./components/settings";
import { CommandCenter } from "./components/CommandCenter";
import { RightPanel } from "./components/panel/RightPanel";
import { useUiStore, initUi } from "./store/ui";
import { usePanelStore } from "./store/panel";
import { useChatStore } from "./store/chat";
import { initTheme } from "./store/theme";

export default function App() {
  const [showSplash, setShowSplash] = useState(true);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [nav, setNav] = useState<NavKey | null>(null);
  const [settingsTab, setSettingsTab] = useState<string | undefined>(undefined);
  const leftPanelWidth = useUiStore((s) => s.leftPanelWidth);
  const setLeftPanelWidth = useUiStore((s) => s.setLeftPanelWidth);
  const rightExpanded = usePanelStore((s) => s.expanded);
  const rightPanelWidth = usePanelStore((s) => s.width);
  const rightFullscreen = usePanelStore((s) => s.fullscreen);
  const taskCardVisible = usePanelStore((s) => s.taskCardVisible);
  const hasTasks = useChatStore((s) => s.tasks.length > 0);
  const setRightPanelWidth = usePanelStore((s) => s.setWidth);

  useEffect(() => { initTheme(); initUi(); }, []);

  // Ctrl+B 切换侧栏（对齐 zcode）
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "b") {
        e.preventDefault();
        setSidebarCollapsed((v) => !v);
      }
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "j") {
        e.preventDefault();
        usePanelStore.getState().openTab("terminal");
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, []);

  // v16: 模型选择器「管理模型」入口 —— 打开设置页并定位到模型 tab
  useEffect(() => {
    const handler = (e: Event) => {
      const tab = (e as CustomEvent<{ tab?: string }>).detail?.tab;
      setSettingsTab(tab);
      setNav("settings");
      setSidebarCollapsed(false);
    };
    window.addEventListener("chatcoder:open-settings", handler);
    return () => window.removeEventListener("chatcoder:open-settings", handler);
  }, []);

  const leftPanelElRef = useRef<HTMLDivElement>(null);
  const rightPanelElRef = useRef<HTMLDivElement>(null);

  // 左栏折叠 = 0px 隐藏（展开按钮移到标题栏左侧，与 logo/导航箭头一起）
  useEffect(() => {
    const el = leftPanelElRef.current;
    if (!el) return;
    if (sidebarCollapsed) { el.style.width = "0px"; el.style.flexBasis = "0px"; }
    else { el.style.width = leftPanelWidth + "px"; el.style.flexBasis = leftPanelWidth + "px"; }
  }, [sidebarCollapsed, leftPanelWidth]);

  useEffect(() => {
    const el = rightPanelElRef.current;
    if (!el) return;
    if (rightExpanded && !rightFullscreen) { el.style.width = rightPanelWidth + "px"; el.style.flexBasis = rightPanelWidth + "px"; }
    else if (rightFullscreen) { el.style.width = ""; el.style.flexBasis = ""; }
    else { el.style.width = "0px"; el.style.flexBasis = "0px"; }
  }, [rightExpanded, rightPanelWidth, rightFullscreen]);

  return (
    <ErrorBoundary>
      {showSplash && <Splash onDone={() => setShowSplash(false)} />}
      <div className="app-shell">
        <Toast />
        <RollbackConfirmModal />
        <CommandCenter />
        {/* v18 布局重构（对齐 zcode）：左侧栏全高（含 logo/导航箭头），
            右侧列 = 顶部标题栏 + 内容行（消息流 + 右侧面板） */}
        <div ref={leftPanelElRef} className={`app-pane app-pane-left collapsible${sidebarCollapsed ? " collapsed" : ""}`} style={sidebarCollapsed ? { width: "0px", flexBasis: "0px" } : { width: `${leftPanelWidth}px`, flexBasis: `${leftPanelWidth}px` }}>
          <Sidebar active={nav} onChange={(k) => { setNav(k); if (k === "settings") setSidebarCollapsed(false); if (k === "chat") { useChatStore.setState({ currentSessionId: null, messages: [], turns: [], tasks: [], runningTurnId: null, isRunning: false, interruptedTurnId: null, streamingBuffers: {}, thinkingBuffers: {}, usage: null, pendingApproval: null, pendingPlan: null, reviewedFiles: {} }); } }} onSessionFocus={() => setNav(null)} collapsed={sidebarCollapsed} onToggleCollapse={() => setSidebarCollapsed((v) => !v)} />
        </div>
        {!sidebarCollapsed && <ResizeHandle side="left" baseWidth={leftPanelWidth} minWidth={200} maxWidth={480} panelEl={leftPanelElRef} onCommit={setLeftPanelWidth} />}
        <div className="app-right">
          <TitleBar leftCollapsed={sidebarCollapsed} rightCollapsed={!rightExpanded} onToggleLeft={() => setSidebarCollapsed((v) => !v)} onToggleRight={() => usePanelStore.getState().togglePanel()} />
          <div className="app-body">
            <main className={`app-main${taskCardVisible && hasTasks ? " todo-card-active" : ""}${!rightExpanded ? " right-panel-collapsed" : ""}`}><Workspace nav={nav} onSessionStart={() => setNav(null)} /></main>
            {rightExpanded && !rightFullscreen && <ResizeHandle side="right" baseWidth={rightPanelWidth} minWidth={200} maxWidth={1200} panelEl={rightPanelElRef} onCommit={setRightPanelWidth} />}
            <div ref={rightPanelElRef} className={`app-pane app-pane-right${rightExpanded ? "" : " collapsed"}${rightFullscreen ? " fullscreen" : ""}`} style={{ width: rightExpanded ? (rightFullscreen ? "100%" : `${rightPanelWidth}px`) : "0px", flexBasis: rightExpanded ? (rightFullscreen ? "100%" : `${rightPanelWidth}px`) : "0px" }}>
              {rightExpanded && <RightPanel />}
            </div>
          </div>
        </div>
      </div>
      {nav === "settings" && <SettingsPage initialTab={settingsTab} onBack={() => setNav(null)} />}
    </ErrorBoundary>
  );
}
