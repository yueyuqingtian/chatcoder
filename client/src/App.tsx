/** 应用根（v5）：三栏骨架 + 右面板最大宽度限制 + 全屏模式。 */
import { useCallback, useEffect, useRef, useState } from "react";
import type { NavKey } from "./components/Sidebar";
import { Workspace } from "./components/Workspace";
import { RollbackConfirmModal } from "./components/chat/RollbackConfirmModal";
import { ResizeHandle } from "./components/ResizeHandle";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { Toast } from "./components/Toast";
import { Splash } from "./components/Splash";
import { SettingsContent, type SettingsTab } from "./components/settings";
import { CommandCenter } from "./components/CommandCenter";
import { PluginSlot } from "./plugins/registry";
import { useUiStore, initUi } from "./store/ui";
import { usePanelStore } from "./store/panel";
import { useChatStore } from "./store/chat";
import { useUpdaterStore } from "./store/updater";
import { initTheme } from "./store/theme";
import { installFocusGuard } from "./utils/focusGuard";

export default function App() {
  const [showSplash, setShowSplash] = useState(true);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [nav, setNav] = useState<NavKey | null>(null);
  // 进入设置前的位置：首页用 null + 空会话 ID 与消息页区分。
  const [returnLocation, setReturnLocation] = useState<{ nav: NavKey | null; sessionId: number | null }>({ nav: null, sessionId: null });
  const [settingsTab, setSettingsTab] = useState<string | undefined>(undefined);
  // v19: 设置页当前 tab（左侧 SettingsSidebar 与内容区共享）。
  const [settingsActiveTab, setSettingsActiveTab] = useState<SettingsTab>("general");
  const leftPanelWidth = useUiStore((s) => s.leftPanelWidth);
  const setLeftPanelWidth = useUiStore((s) => s.setLeftPanelWidth);
  const rightExpanded = usePanelStore((s) => s.expanded);
  const rightPanelWidth = usePanelStore((s) => s.width);
  const rightFullscreen = usePanelStore((s) => s.fullscreen);
  const setRightPanelWidth = usePanelStore((s) => s.setWidth);

  useEffect(() => {
    initTheme(); initUi();
    // 更新状态通道：订阅主进程推送（侧栏/关于页共享）
    useUpdaterStore.getState().init();
    // 启动全局状态通道：跨会话运行态/活动时间（侧栏实时化，不随会话切换重建）
    useChatStore.getState().connectGlobalEvents();
    // 启动时立即触发 Bootstrap（加载项目、会话、模型与提供商列表）
    void useChatStore.getState().loadBootstrap();

    // 全局焦点保护：覆盖"切换页面后输入框无法聚焦/IME 卡死"的兜底逻辑
    const guard = installFocusGuard();
    return () => {
      guard.dispose();
      useChatStore.getState().disconnectGlobalEvents();
    };
  }, []);

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

  const openSettings = useCallback((tab?: string) => {
    const { currentSessionId } = useChatStore.getState();
    setReturnLocation({ nav, sessionId: currentSessionId });
    if (tab) setSettingsActiveTab(tab as SettingsTab);
    setSettingsTab(tab);
    setNav("settings");
    setSidebarCollapsed(false);
  }, [nav]);

  const leaveSettings = useCallback(() => {
    const target = returnLocation;
    setNav(target.nav);
    if (target.sessionId === null) {
      useChatStore.setState({ currentSessionId: null, ...{
        messages: [], turns: [], tasks: [], runningTurnId: null, isRunning: false,
        interruptedTurnId: null, streamingBuffers: {}, thinkingBuffers: {}, usage: null,
        pendingApproval: null, pendingPlan: null, reviewedFiles: {}, injectMarks: [],
      } });
    }
    // 退出设置页后自动刷新模型列表
    void useChatStore.getState().loadModels();

    // 从设置页返回会话页后主动聚焦输入框（登录/配置期间 textarea 已卸载重挂，
    // 不恢复焦点则用户点击会被"窗口未聚焦"或卸载竞态吞掉）
    window.setTimeout(() => {
      window.dispatchEvent(new CustomEvent("chatcoder:focus-composer"));
    }, 50);
  }, [returnLocation]);

  // v16: 模型选择器「管理模型」入口 —— 打开设置页并定位到模型 tab
  useEffect(() => {
    const handler = (e: Event) => {
      const tab = (e as CustomEvent<{ tab?: string }>).detail?.tab;
      openSettings(tab);
    };
    window.addEventListener("chatcoder:open-settings", handler);
    return () => window.removeEventListener("chatcoder:open-settings", handler);
  }, [openSettings]);
  // v19: 命令中心跳转设置 tab 同步
  useEffect(() => {
    if (settingsTab && nav === "settings") setSettingsActiveTab(settingsTab as SettingsTab);
  }, [settingsTab, nav]);
  // 设置入口由 openSettings 捕获，避免 nav 变化后覆盖首页的会话 ID。

  const leftPanelElRef = useRef<HTMLDivElement>(null);
  const rightPanelElRef = useRef<HTMLDivElement>(null);

  // 左栏折叠 = 0px 隐藏（展开按钮移到标题栏左侧，和 logo/导航箭头一起）
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
            右侧 = 顶部标题栏 + 内容行（消息流 + 右侧面板）。 */}
        <div ref={leftPanelElRef} className={`app-pane app-pane-left collapsible${sidebarCollapsed ? " collapsed" : ""}`} style={sidebarCollapsed ? { width: "0px", flexBasis: "0px" } : { width: `${leftPanelWidth}px`, flexBasis: `${leftPanelWidth}px` }}>
          {nav === "settings" ? (
            /* v19: 设置侧栏经插件 slot 渲染（与外部侧栏共用壳与宽度）。 */
            <PluginSlot slot="settings-sidebar" tab={settingsActiveTab} onTab={setSettingsActiveTab} onBack={leaveSettings} collapsed={sidebarCollapsed} />
          ) : (
            <PluginSlot slot="sidebar" active={nav} onChange={(k: NavKey) => { if (k === "settings") { openSettings(); return; } setNav(k); if (k === "chat") { useChatStore.setState({ currentSessionId: null, messages: [], turns: [], tasks: [], runningTurnId: null, isRunning: false, interruptedTurnId: null, streamingBuffers: {}, thinkingBuffers: {}, usage: null, pendingApproval: null, pendingPlan: null, reviewedFiles: {} }); } }} onSessionFocus={() => setNav(null)} collapsed={sidebarCollapsed} onToggleCollapse={() => setSidebarCollapsed((v) => !v)} />
          )}
        </div>
        {!sidebarCollapsed && <ResizeHandle side="left" baseWidth={leftPanelWidth} minWidth={200} maxWidth={480} reservePx={370} panelEl={leftPanelElRef} onCommit={setLeftPanelWidth} />}
        <div className="app-right">
          {/* v19: 标题栏与右面板经插件 slot 渲染 */}
          <PluginSlot slot="titlebar" leftCollapsed={sidebarCollapsed} rightCollapsed={!rightExpanded} onToggleLeft={() => setSidebarCollapsed((v) => !v)} onToggleRight={() => usePanelStore.getState().togglePanel()} />
          <div className="app-body">
            <main className={`app-main${!rightExpanded ? " right-panel-collapsed" : ""}`}>
              {nav === "settings"
                ? <SettingsContent tab={settingsActiveTab} />
                : <Workspace nav={nav} onSessionStart={() => setNav(null)} />}
            </main>
            {/* plan-95: reservePx=主区 min-width 480 + 手柄宽 10，动态上限防溢出裁剪 */}
            {rightExpanded && !rightFullscreen && <ResizeHandle side="right" baseWidth={rightPanelWidth} minWidth={280} maxWidth={1200} reservePx={490} panelEl={rightPanelElRef} onCommit={setRightPanelWidth} />}
            <div ref={rightPanelElRef} className={`app-pane app-pane-right${rightExpanded ? "" : " collapsed"}${rightFullscreen ? " fullscreen" : ""}`} style={{ width: rightExpanded ? (rightFullscreen ? "100%" : `${rightPanelWidth}px`) : "0px", flexBasis: rightExpanded ? (rightFullscreen ? "100%" : `${rightPanelWidth}px`) : "0px" }}>
              {rightExpanded && <PluginSlot slot="right-panel" />}
            </div>
          </div>
        </div>
      </div>
    </ErrorBoundary>
  );
}
