/** 应用根（v5）：三栏骨架 + 右面板最大宽度限制 + 全屏模式。 */
import { useCallback, useEffect, useRef, useState } from "react";
import type { NavKey } from "./components/Sidebar";
import { Workspace } from "./components/Workspace";
import { RollbackConfirmModal } from "./components/chat/RollbackConfirmModal";
import { ImageGallery } from "./components/chat/ImageGallery";
import { WhatsNewModal } from "./components/WhatsNewModal";
import { ResizeHandle } from "./components/ResizeHandle";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { Toast } from "./components/Toast";
// plan-24-106 M5：原 `@tomagranate/liquid-glass` 折射叠加层已整体移除（该包不可用，
// 且其 SVG feDisplacementMap 只对元素背后的背景副本做位移，无法采样窗口外桌面）。
// 真正的桌面折射改由主进程的原生面板承担（electron/liquid-glass.cjs）。
import { SettingsContent, type SettingsTab } from "./components/settings";
import { CommandCenter } from "./components/CommandCenter";
import { ArchivedProjectPrompt } from "./components/ArchivedProjectPrompt";
import { PluginSlot } from "./plugins/registry";
import { NativeTitleTooltip, PageTransition, TooltipProvider } from "./components/ui";
import { useUiStore, initUi } from "./store/ui";
import { usePanelStore } from "./store/panel";
import { useChatStore } from "./store/chat";
import { useUpdaterStore } from "./store/updater";
import { initTheme } from "./store/theme";
import { installFocusGuard } from "./utils/focusGuard";
import { setWindowMotion } from "./perf/bus";
import { applyPaneUpdate } from "./perf/paneTransition";

/** S7（plan-329-1647）：面板折叠/展开的过渡已整体改为 View Transition。
 *
 *  原实现在这里（beginPaneAnimation）：给面板 180ms 的 CSS width 过渡，同时把内容根宽度
 *  钉死以省掉逐帧折行——代价是过渡期内容被裁切、结束瞬间一次集中重排。
 *  现在缩放交给合成器快照动画（见 client/src/perf/paneTransition.ts），布局一次性到位；
 *  过渡期与收尾的“运动中”状态由 PerfBus 的 panel-anim 统一表达。
 *  下方 applyPaneUpdate 的调用点：本文件的左栏折叠、以及 store/panel.ts 的右栏展开/折叠/全屏。 */

export default function App() {
  const leftPanelElRef = useRef<HTMLDivElement>(null);
  const rightPanelElRef = useRef<HTMLDivElement>(null);
  const mainElRef = useRef<HTMLDivElement>(null);
  // plan-308-1555 M7：删除了原先的第二个开屏界面（纯 logo 蒙层）。
  // 用户要求只保留主进程 loading.html 那一个带加载指示的启动界面——
  // 两个开屏叠在一起会先看到「加载中」再闪一个「无加载图标的 logo」，观感割裂。
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [nav, setNav] = useState<NavKey | null>(null);
  // plan-41-233：右面板状态按会话分桶，这里跟随会话切换同步「当前桶」。
  const currentSessionId = useChatStore((s) => s.currentSessionId);
  // 进入设置前的位置：首页用 null + 空会话 ID 与消息页区分。
  const [returnLocation, setReturnLocation] = useState<{ nav: NavKey | null; sessionId: number | null }>({ nav: null, sessionId: null });
  const [settingsTab, setSettingsTab] = useState<string | undefined>(undefined);
  // v19: 设置页当前 tab（左侧 SettingsSidebar 与内容区共享）。
  const [settingsActiveTab, setSettingsActiveTab] = useState<SettingsTab>("general");
  // 问题1: 进入设置前记录右面板展开状态，离开时恢复（设置页内折叠右面板）
  const settingsPanelExpandedRef = useRef(false);
  const leftPanelWidth = useUiStore((s) => s.leftPanelWidth);
  const setLeftPanelWidth = useUiStore((s) => s.setLeftPanelWidth);
  const rightExpanded = usePanelStore((s) => s.expanded);
  const rightPanelWidth = usePanelStore((s) => s.width);
  const rightFullscreen = usePanelStore((s) => s.fullscreen);
  const setRightPanelWidth = usePanelStore((s) => s.setWidth);

  /** 左栏折叠/展开（S7）：与右栏同走 applyPaneUpdate——View Transition 快照动画，
   *  布局一次性到位、主线程逐帧零布局；能力缺失或用户要求减少动效时自动退化。 */
  const toggleSidebar = useCallback(() => {
    applyPaneUpdate(() => setSidebarCollapsed((v) => !v));
  }, []);
  const toggleRightPanel = useCallback(() => {
    if (usePanelStore.getState().fullscreen) return;
    usePanelStore.getState().togglePanel();
  }, []);

  useEffect(() => {
    initTheme(); initUi();
    // plan-31-151 S3：data-window-drag 分流已移除——标题栏统一走系统 -webkit-app-region:drag。
    // plan-31-151 S2：监听主进程 maximize/unmaximize 事件，修复 frame:true +
    //   titleBarStyle:"hidden" 最大化时客户区外扩 8px 非客户区导致的边缘内容裁切。
    const offMaximizeChange = window.chatcoderAPI?.onMaximizeChange?.((isMax) => {
      document.body.classList.toggle("maximized", isMax);
    });
    // 更新状态通道：订阅主进程推送（侧栏/关于页共享）
    useUpdaterStore.getState().init();
    // 启动全局状态通道：跨会话运行态/活动时间（侧栏实时化，不随会话切换重建）
    useChatStore.getState().connectGlobalEvents();
    // 启动时立即触发 Bootstrap（加载项目、会话、模型与提供商列表）
    void useChatStore.getState().loadBootstrap();

    // 全局焦点保护：覆盖"切换页面后输入框无法聚焦/IME 卡死"的兜底逻辑
    const guard = installFocusGuard();
    // 主进程在改窗口几何前同步发来运动态（IPC window:motion）。
    // S7 起面板折叠/展开的过渡由 applyPaneUpdate 接管（View Transition），
    // 不再需要 chatcoder:panel-before-* 事件。
    const offMotion = window.chatcoderAPI?.onWindowMotion?.((payload) => {
      const active = payload?.active === true;
      (window as unknown as { __chatcoderWindowMotion?: boolean }).__chatcoderWindowMotion = active;
      document.documentElement.setAttribute("data-window-motion", active ? "1" : "0");
      setWindowMotion(active); // PerfBus：窗口几何运动状态（S3）
      window.dispatchEvent(new CustomEvent("chatcoder:window-motion", { detail: { active } }));
    });
    return () => {
      guard.dispose();
      offMotion?.();
      offMaximizeChange?.();
      useChatStore.getState().disconnectGlobalEvents();
    };
  }, []);

  // Ctrl+B 切换侧栏（对齐 zcode）
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "b") {
        e.preventDefault();
        toggleSidebar();
      }
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "j") {
        e.preventDefault();
        usePanelStore.getState().openTab("terminal");
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [toggleSidebar]);

  const openSettings = useCallback((tab?: string) => {
    const { currentSessionId } = useChatStore.getState();
    setReturnLocation({ nav, sessionId: currentSessionId });
    if (tab) setSettingsActiveTab(tab as SettingsTab);
    setSettingsTab(tab);
    setNav("settings");
    if (sidebarCollapsed) toggleSidebar();
    // 问题1: 进入设置页自动折叠右面板（记录展开态，离开时恢复）
    settingsPanelExpandedRef.current = usePanelStore.getState().expanded;
    if (settingsPanelExpandedRef.current) toggleRightPanel();
  }, [nav, sidebarCollapsed, toggleSidebar, toggleRightPanel]);

  const leaveSettings = useCallback(() => {
    const target = returnLocation;
    // 问题1: 离开设置页恢复右面板展开态
    if (settingsPanelExpandedRef.current) toggleRightPanel();
    setNav(target.nav);
    if (target.sessionId === null) {
      useChatStore.setState({ currentSessionId: null, ...{
        messages: [], turns: [], tasks: [], runningTurnId: null, isRunning: false,
        interruptedTurnId: null, streamingBuffers: {}, thinkingBuffers: {}, usage: null,
        pendingApproval: null, pendingPlan: null, reviewedFiles: {},
      } });
    }
    // 退出设置页后自动刷新模型列表
    void useChatStore.getState().loadModels();

    // 从设置页返回会话页后主动聚焦输入框（登录/配置期间 textarea 已卸载重挂，
    // 不恢复焦点则用户点击会被"窗口未聚焦"或卸载竞态吞掉）
    window.setTimeout(() => {
      window.dispatchEvent(new CustomEvent("chatcoder:focus-composer"));
    }, 50);
  }, [returnLocation, toggleRightPanel]);

  // plan-41-233：会话切换时把右面板投影切到该会话的桶（旧会话的终端/浏览器实例继续保活，
  // 切回时原样恢复；不再出现「A 会话的子代理标签残留在 B 会话面板」）。
  useEffect(() => {
    usePanelStore.getState().setActiveSession(currentSessionId);
  }, [currentSessionId]);

  // v16: 模型选择器「管理模型」入口 —— 打开设置页并定位到模型 tab
  useEffect(() => {
    const handler = (e: Event) => {
      const tab = (e as CustomEvent<{ tab?: string }>).detail?.tab;
      openSettings(tab);
    };
    window.addEventListener("chatcoder:open-settings", handler);
    return () => window.removeEventListener("chatcoder:open-settings", handler);
  }, [openSettings]);
  // S14（plan-41-197）：反向通道——设置页内容区需要“离开设置页进入刚打开的会话”时
  // （如归档恢复并打开），由内容区派发该事件：退出设置并回到工作区。
  // 与 leaveSettings 的差异：**不按入场快照重置 currentSessionId**——调用方刚刚
  // switchSession 切到目标会话，若在这里重置会话会被清掉（“点了恢复并打开却没进会话”）。
  useEffect(() => {
    const handler = (e: Event) => {
      // plan-41-198：detail.nav 指定离开设置后的落点——设置页「扩展管理 → 前往市场」
      // 需要直接落到左面板「拓展」（nav=skills），而不是回入场快照的会话。
      const targetNav = (e as CustomEvent<{ nav?: string }>).detail?.nav;
      if (settingsPanelExpandedRef.current) toggleRightPanel();
      setNav((targetNav as NavKey | undefined) ?? null);
      void useChatStore.getState().loadModels();
      window.setTimeout(() => {
        window.dispatchEvent(new CustomEvent("chatcoder:focus-composer"));
      }, 50);
    };
    window.addEventListener("chatcoder:leave-settings", handler);
    return () => window.removeEventListener("chatcoder:leave-settings", handler);
  }, [toggleRightPanel]);

  // plan-73-323：宠物面板 → 主窗的两个入口
  //  ①「查看会话」：切到目标会话并退出设置页（复用 switchSession 与既有 leave-settings 通道，
  //    该通道不重置 currentSessionId，所以刚切的会话不会被盖掉）；
  //  ② 面板底部齿轮：打开「设置 → 宠物」。
  useEffect(() => {
    const offFocus = window.chatcoderAPI?.onPetFocusSession?.((sessionId) => {
      void useChatStore.getState().switchSession(sessionId);
      window.dispatchEvent(new CustomEvent("chatcoder:leave-settings"));
    });
    const offSettings = window.chatcoderAPI?.onPetOpenSettings?.(() => {
      openSettings("pets");
    });
    return () => {
      offFocus?.();
      offSettings?.();
    };
  }, [openSettings]);
  // v19: 命令中心跳转设置 tab 同步
  useEffect(() => {
    if (settingsTab && nav === "settings") setSettingsActiveTab(settingsTab as SettingsTab);
  }, [settingsTab, nav]);
  // 设置入口由 openSettings 捕获，避免 nav 变化后覆盖首页的会话 ID。

  /** 中间区容器 ref：分隔条拖拽期间其内容根（PageTransition）被 ResizeHandle 冻结，
   *  消息 markdown/虚拟列表不再随面板宽度每帧重排（性能冻结，见 ResizeHandle Props）。 */

  // plan-31-151 S5：面板宽度由 React inline style 单源写入——effect 二次写回 DOM 是
  //   bcc223d 引入的冗余（与 inline style 重复），且会在 View Transition 快照期间
  //   污染「新状态」采样（同一几何属性三个写入者竞争，快速开合时动画方向/时序错乱）。

  return (
    <ErrorBoundary>
      {/* S0（plan-41-197）：全局 Tooltip Provider —— 所有 ui/Tooltip 消费点
          （侧栏/顶栏/右面板/设置页图标按钮）共享统一展示延迟（200ms）。 */}
      <TooltipProvider>
      {/* S1（plan-41-197）：接管存量原生 title——全站悬停提示统一为圆角卡片浮层，
          与 ui/Tooltip 共享展示延迟（不再出现浏览器自带的方形浮块）。 */}
      <NativeTitleTooltip />
      <div className="app-shell">
        <Toast />
        <RollbackConfirmModal />
        <ImageGallery />
        <WhatsNewModal />
        <CommandCenter />
        <ArchivedProjectPrompt />
        {/* v18 布局重构（对齐 zcode）：左侧栏全高（含 logo/导航箭头），
            右侧 = 顶部标题栏 + 内容行（消息流 + 右侧面板）。 */}
        <div ref={leftPanelElRef} className={`app-pane app-pane-left collapsible${sidebarCollapsed ? " collapsed" : ""}`} style={sidebarCollapsed ? { width: "0px", flexBasis: "0px" } : { width: `${leftPanelWidth}px`, flexBasis: `${leftPanelWidth}px` }}>
          {nav === "settings" ? (
            /* v19: 设置侧栏经插件 slot 渲染（与外部侧栏共用壳与宽度）。 */
            <PluginSlot slot="settings-sidebar" tab={settingsActiveTab} onTab={setSettingsActiveTab} onBack={leaveSettings} collapsed={sidebarCollapsed} />
          ) : (
            <PluginSlot slot="sidebar" active={nav} onChange={(k: NavKey) => { if (k === "settings") { openSettings(); return; } setNav(k); if (k === "chat") { useChatStore.setState({ currentSessionId: null, messages: [], turns: [], tasks: [], runningTurnId: null, isRunning: false, interruptedTurnId: null, streamingBuffers: {}, thinkingBuffers: {}, usage: null, pendingApproval: null, pendingPlan: null, reviewedFiles: {} }); } }} onSessionFocus={() => setNav(null)} collapsed={sidebarCollapsed} onToggleCollapse={toggleSidebar} />
          )}
        </div>
        {/* RFL-1（plan-329-1647 S4）：不再传 freezeRefs——拖左分隔条时中列内容宽度实时跟随，
            消息流文本逐帧折行（用户反馈「拖拽时排版不实时」）。性能改由 PerfBus 零读预算兜。 */}
        {!sidebarCollapsed && <ResizeHandle side="left" baseWidth={leftPanelWidth} minWidth={200} maxWidth={480} reservePx={370} panelEl={leftPanelElRef} onCommit={setLeftPanelWidth} />}
        <div className="app-right">
          {/* v19: 标题栏与右面板经插件 slot 渲染。
              plan-41-233：页面态（拓展/定时任务/设置等 nav 页）传 page——隐藏会话标题、项目
              与右侧图标组；左侧 logo/折叠/前进后退保留，页面态的会话聚焦由 Sidebar 抑制。 */}
          <PluginSlot slot="titlebar" leftCollapsed={sidebarCollapsed} rightCollapsed={!rightExpanded} page={nav !== null && nav !== "chat"} onToggleLeft={toggleSidebar} onToggleRight={toggleRightPanel} onSessionFocus={() => setNav(null)} />
          <div className="app-body">
            <main ref={mainElRef} className={`app-main${!rightExpanded ? " right-panel-collapsed" : ""}`}>
              {/* plan-282-1421（第3项）：设置 ↔ 工作区切换过渡（id 为页面标识）。
                  App.tsx 与 settings/Workspace 内层过渡叠加时也不会位移：两者都是
                  transform/opacity，且内层只在 tab/session 变化时重播。 */}
              <PageTransition id={nav === "settings" ? "settings" : `ws-${nav ?? "chat"}`}>
                {nav === "settings"
                  ? <SettingsContent tab={settingsActiveTab} />
                  : <Workspace nav={nav} onSessionStart={() => setNav(null)} />}
              </PageTransition>
            </main>
            {/* plan-95: reservePx=主区 min-width 480 + 手柄宽 10，动态上限防溢出裁剪。
                RFL-1（S4）：freezeRefs 收窄为仅右面板内容根——中列实时跟随（消息流逐帧折行），
                右面板内的 Monaco/xterm 不逐帧重排，松手后由收敛序列一次解冻。 */}
            {rightExpanded && !rightFullscreen && <ResizeHandle side="right" baseWidth={rightPanelWidth} minWidth={280} maxWidth={1200} reservePx={490} panelEl={rightPanelElRef} onCommit={setRightPanelWidth} freezeRefs={[rightPanelElRef]} />}
            <div ref={rightPanelElRef} className={`app-pane app-pane-right${rightExpanded ? "" : " collapsed"}${rightFullscreen ? " fullscreen" : ""}`} style={{ width: rightExpanded ? (rightFullscreen ? "100%" : `${rightPanelWidth}px`) : "0px", flexBasis: rightExpanded ? (rightFullscreen ? "100%" : `${rightPanelWidth}px`) : "0px" }}>
              {/* plan-41-233：内容常驻挂载——折叠只变几何（width:0 + visibility:hidden），
                  终端 PTY 与浏览器页面不再因折叠被卸载重建（用户诉求：折叠再展开保留状态）。 */}
              <PluginSlot slot="right-panel" />
            </div>
          </div>
        </div>
      </div>
      </TooltipProvider>
    </ErrorBoundary>
  );
}
