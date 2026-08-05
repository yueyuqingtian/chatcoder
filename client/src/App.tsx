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
import { SettingsPage } from "./components/SettingsPage";
import { RightPanel } from "./components/panel/RightPanel";
import { useUiStore, initUi } from "./store/ui";
import { usePanelStore } from "./store/panel";
import { useChatStore } from "./store/chat";
import { initTheme } from "./store/theme";

export default function App() {
  const [showSplash, setShowSplash] = useState(true);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [nav, setNav] = useState<NavKey | null>(null);
  const leftPanelWidth = useUiStore((s) => s.leftPanelWidth);
  const setLeftPanelWidth = useUiStore((s) => s.setLeftPanelWidth);
  const rightExpanded = usePanelStore((s) => s.expanded);
  const rightPanelWidth = usePanelStore((s) => s.width);
  const rightFullscreen = usePanelStore((s) => s.fullscreen);
  const setRightPanelWidth = usePanelStore((s) => s.setWidth);

  useEffect(() => { initTheme(); initUi(); }, []);

  const leftPanelElRef = useRef<HTMLDivElement>(null);
  const rightPanelElRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = leftPanelElRef.current;
    if (!el) return;
    if (sidebarCollapsed) { el.style.width = ""; el.style.flexBasis = ""; }
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
        <TitleBar leftCollapsed={sidebarCollapsed} rightCollapsed={!rightExpanded} onToggleLeft={() => setSidebarCollapsed((v) => !v)} onToggleRight={() => usePanelStore.getState().togglePanel()} />
        <div className="app-body">
          <div ref={leftPanelElRef} className={`app-pane app-pane-left collapsible${sidebarCollapsed ? " collapsed" : ""}`} style={sidebarCollapsed ? undefined : { width: `${leftPanelWidth}px`, flexBasis: `${leftPanelWidth}px` }}>
            <Sidebar active={nav} onChange={(k) => { setNav(k); if (k === "settings") setSidebarCollapsed(false); if (k === "chat") { useChatStore.setState({ currentSessionId: null, messages: [], turns: [], tasks: [], runningTurnId: null, isRunning: false, interruptedTurnId: null, streamingBuffers: {}, thinkingBuffers: {}, usage: null, pendingApproval: null, pendingPlan: null, reviewedFiles: {} }); } }} onSessionFocus={() => setNav(null)} collapsed={sidebarCollapsed} onToggleCollapse={() => setSidebarCollapsed((v) => !v)} />
          </div>
          {!sidebarCollapsed && <ResizeHandle side="left" baseWidth={leftPanelWidth} minWidth={200} maxWidth={480} panelEl={leftPanelElRef} onCommit={setLeftPanelWidth} />}
          <main className="app-main"><Workspace nav={nav} onSessionStart={() => setNav(null)} /></main>
          {rightExpanded && !rightFullscreen && <ResizeHandle side="right" baseWidth={rightPanelWidth} minWidth={200} maxWidth={1200} panelEl={rightPanelElRef} onCommit={setRightPanelWidth} />}
          <div ref={rightPanelElRef} className={`app-pane app-pane-right${rightExpanded ? "" : " collapsed"}${rightFullscreen ? " fullscreen" : ""}`} style={{ width: rightExpanded ? (rightFullscreen ? "100%" : `${rightPanelWidth}px`) : "0px", flexBasis: rightExpanded ? (rightFullscreen ? "100%" : `${rightPanelWidth}px`) : "0px" }}>
            {rightExpanded && <RightPanel />}
          </div>
        </div>
      </div>
      {nav === "settings" && <SettingsPage onBack={() => setNav(null)} />}
    </ErrorBoundary>
  );
}
