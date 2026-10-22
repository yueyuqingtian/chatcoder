/** 右侧面板（v5）：多标签页 + 全屏按钮 + 加号菜单修复。
 * v19: 标签溢出治理——tab 不再被 flex 压缩（flex-shrink:0 + 文字省略），
 *      支持滚轮横滚；溢出时头部出现「全部标签」下拉，可直接跳转/关闭。
 * plan-20261009-ui U8：标签条改为「图标 + 文案」胶囊（关闭按钮占固定槽位，悬停才出现，不抖动）；
 *      加号 / 全部标签菜单统一为 Popover（点外部、Esc、进出场）；头部按钮统一 28px 并用 Tooltip；
 *      空态由四条灰色长条改为 2×2 工具启动卡（角色色磁贴，闲置灰度、聚焦彩色）。
 */
import { IxTasks, IxBrowser, IxTerminal, IxFiles, IxBot, IxBug, IxWorkflow } from "../iconkit/StackIcons";
import { lazy, memo, Suspense, useEffect, useLayoutEffect, useRef, useState } from "react";
import { api } from "../../api/client";
import { isBusy, subscribe } from "../../perf/bus";
import { usePanelStore, bucketSessionId } from "../../store/panel";
import { sessionKey, useBrowserStore } from "../../store/browser";
import type { PanelTab, PanelTabId } from "../../store/panel";
import { useI18n } from "../../store/i18n";
import {
  IconArrowToggle, IconCheck, IconChevronDown, IconGitBranch, IconMaximize, IconMessageSquare, IconMinus, IconPlus, IconRotateCcw, IconX,
} from "../icons";
import { Popover, Tooltip } from "../ui";
import { TaskSummaryPanel } from "./TaskSummaryPanel";
import { BrowserPanel } from "./BrowserPanel";
import { SubagentPanel } from "./SubagentPanel";
import { DebugPanel } from "./DebugPanel";
import { WorkflowPanel } from "../workflow/WorkflowPanel";
import { SidePanel } from "./SidePanel";
import { GitPanel } from "./GitPanel";
import { ErrorBoundary } from "../ErrorBoundary";

/** 文件树（内含 Monaco 编辑器）与终端（内含 xterm）体积大，只在对应标签打开时才加载，不进首屏包。
 *  占位层与面板同样撑满剩余空间，加载瞬间不产生布局跳动。 */
const FileTreePanel = lazy(() => import("./FileTreePanel").then((m) => ({ default: m.FileTreePanel })));
const TerminalPanel = lazy(() => import("./TerminalPanel").then((m) => ({ default: m.TerminalPanel })));
const LazyPanelFallback = () => <div style={{ flex: 1, minHeight: 0 }} aria-busy="true" />;

/** plan-75-334 阶段3：面板内容按 tab/sessionId/visible 记忆化。
 *  此前标签列表无关变化（新增/关闭其它标签、全屏切换等）会重建**全部**面板内容，
 *  包括已保活的终端与浏览器；memo 后只有本标签自身的可见性与 tab 变化才重渲染。 */
const PanelContent = memo(function PanelContent({ tab, sessionId, visible }: { tab: PanelTab; sessionId: number | null; visible: boolean }) {
  switch (tab.id) {
    case "task-summary": return <TaskSummaryPanel visible={visible} />;
    case "browser": return <BrowserPanel sessionId={sessionId} visible={visible} />;
    case "terminal": return (
      <Suspense fallback={<LazyPanelFallback />}>
        <TerminalPanel tab={tab} sessionId={sessionId} visible={visible} />
      </Suspense>
    );
    case "files": return (
      <Suspense fallback={<LazyPanelFallback />}>
        <FileTreePanel visible={visible} sessionId={sessionId} />
      </Suspense>
    );
    case "subagent": return <SubagentPanel threadId={tab.meta?.threadId} agentName={tab.meta?.agentName} visible={visible} />;
    case "debug": return <DebugPanel sessionId={sessionId} />;
    case "workflow": return <WorkflowPanel sessionId={sessionId} visible={visible} />;
    case "side": return <SidePanel visible={visible} />;
    case "git": return <GitPanel visible={visible} />;
    default: return null;
  }
});

/**
 * 单个面板内容的错误隔离层：某块面板（浏览器 / 终端 / 文件树）抛错时只让该面板
 * 显示局部兜底，不再冒泡到 App 顶层 ErrorBoundary 把整个应用打成白屏。
 * resetKey 用「桶 + tab key」：切 tab 或重开同名 tab 时自动复位并重试渲染。
 */
const GuardedPanelContent = memo(function GuardedPanelContent({ tabKey, tab, sessionId, visible }: {
  tabKey: string;
  tab: PanelTab;
  sessionId: number | null;
  visible: boolean;
}) {
  return (
    <ErrorBoundary variant="panel" resetKey={tabKey}>
      <PanelContent tab={tab} sessionId={sessionId} visible={visible} />
    </ErrorBoundary>
  );
});

/** 标签图标（与加号菜单、启动卡共用同一套） */
function tabIcon(id: PanelTabId, size: number) {
  switch (id) {
    case "task-summary": return <IxTasks size={size} />;
    case "browser": return <IxBrowser size={size} />;
    case "terminal": return <IxTerminal size={size} />;
    case "files": return <IxFiles size={size} />;
    case "subagent": return <IxBot size={size} />;
    case "debug": return <IxBug size={size} />;
    case "workflow": return <IxWorkflow size={size} />;
    case "side": return <IconMessageSquare size={size} />;
    case "git": return <IconGitBranch size={size} />;
  }
}

export function RightPanel() {
  const { t } = useI18n();
  // tabs/activeKey 为「当前会话桶」的投影（见 store/panel.ts）；buckets 用于跨会话保活渲染
  const tabs = usePanelStore((s) => s.tabs);
  const activeKey = usePanelStore((s) => s.activeKey);
  const buckets = usePanelStore((s) => s.buckets);
  const activeBucket = usePanelStore((s) => s.activeBucket);
  const closePanel = usePanelStore((s) => s.closePanel);
  const openTab = usePanelStore((s) => s.openTab);
  const openNewTab = usePanelStore((s) => s.openNewTab);
  const closeTab = usePanelStore((s) => s.closeTab);
  const closedStack = usePanelStore((s) => s.closedStack);
  const reopenClosedTab = usePanelStore((s) => s.reopenClosedTab);
  const setActiveTab = usePanelStore((s) => s.setActiveTab);
  const fullscreen = usePanelStore((s) => s.fullscreen);
  const toggleFullscreen = usePanelStore((s) => s.toggleFullscreen);
  const [showAddMenu, setShowAddMenu] = useState(false);
  const [showTabsMenu, setShowTabsMenu] = useState(false);
  // plan-20261015-browser-agent：当前会话的 AI 正在操作浏览器——「浏览器」标签上给出状态点
  const browserAgentBusy = useBrowserStore((s) => (s.sessions[sessionKey(bucketSessionId(activeBucket))]?.agent.phase ?? "idle") === "working");
  /** 连接器「开发调试」是否已启用——启用后才提供调试面板入口（需求：如果 mcp 开启了）。
   *  面板入口是低频操作，在打开「+」菜单时重查即可，无需全局订阅。 */
  const [debugEnabled, setDebugEnabled] = useState(false);
  const refreshDebugEnabled = () => {
    void api.listMcpServers()
      .then((list) => setDebugEnabled(list.some((m) => m.name === "debugger" && m.is_active)))
      .catch(() => setDebugEnabled(false));
  };
  useEffect(refreshDebugEnabled, []);
  const tabsRef = useRef<HTMLDivElement>(null);
  const addBtnRef = useRef<HTMLButtonElement>(null);
  const tabsBtnRef = useRef<HTMLButtonElement>(null);
  const [tabsOverflow, setTabsOverflow] = useState(false);

  const tabLabel = (tTab: PanelTab): string => {
    const base: Record<PanelTabId, string> = {
      "task-summary": t("rp.tab_task_summary"),
      browser: t("rp.tab_browser"),
      terminal: t("rp.tab_terminal"),
      files: t("rp.tab_files"),
      subagent: t("rp.tab_subagent"),
      debug: t("rp.tab_debug"),
      workflow: t("rp.tab_workflow"),
      side: "侧边对话",
      git: "Git",
    };
    if (tTab.id === "subagent" && tTab.meta?.agentName) return tTab.meta.agentName.slice(0, 10);
    return tTab.instance > 1 ? `${base[tTab.id]} ${tTab.instance}` : base[tTab.id];
  };

  // 标签条溢出检测（scrollWidth > clientWidth 时展示「全部标签」入口）。
  // plan-329-1647 S5：两个布局读（scrollWidth / clientWidth）在运动期跳过——
  // 拖分隔条/折叠面板时标签条宽度每帧都变，逐帧读会升级为强制同步布局。
  // 运动中只记账，运动结束由 PerfBus 订阅补一次。
  useEffect(() => {
    const el = tabsRef.current;
    if (!el) return;
    let missed = false;
    const check = () => {
      if (isBusy()) { missed = true; return; }
      missed = false;
      const overflow = el.scrollWidth > el.clientWidth + 2;
      setTabsOverflow(overflow);
      // 面板被拖窄后激活的标签可能被挤出可视区：溢出时顺手把它滚回来
      if (overflow) el.querySelector<HTMLElement>(".rp-tab.is-active")?.scrollIntoView({ block: "nearest", inline: "nearest" });
    };
    check();
    const ro = new ResizeObserver(check);
    ro.observe(el);
    const off = subscribe((busy) => { if (!busy && missed) check(); });
    return () => { ro.disconnect(); off(); };
  }, [tabs.length]);

  // 激活的标签滚入可视区（标签很多、从菜单跳转时尤其需要）
  useEffect(() => {
    const el = tabsRef.current?.querySelector<HTMLElement>(".rp-tab.is-active");
    el?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }, [activeKey]);

  // plan-91-393：激活标签的底色滑块——量出激活标签位置，滑块用弹簧曲线跟随移动
  const [ind, setInd] = useState<{ x: number; w: number } | null>(null);
  useLayoutEffect(() => {
    const el = tabsRef.current?.querySelector<HTMLElement>(".rp-tab.is-active");
    setInd(el ? { x: el.offsetLeft, w: el.offsetWidth } : null);
  }, [activeKey, tabs.length]);

  // 滚轮纵转横：标签过多时直接滚轮浏览
  const onTabsWheel = (e: React.WheelEvent) => {
    const el = tabsRef.current;
    if (!el || el.scrollWidth <= el.clientWidth) return;
    e.preventDefault();
    el.scrollLeft += e.deltaY;
  };

  const handleAdd = (id: PanelTabId) => { openTab(id); setShowAddMenu(false); };
  const handleAddTerminal = () => { openNewTab("terminal"); setShowAddMenu(false); };

  /** 启动卡 / 加号菜单共用的入口清单（调试按条件出现） */
  const entries: Array<{ id: PanelTabId; title: string; desc: string; onPick: () => void }> = [
    { id: "task-summary", title: t("rp.tab_task_summary"), desc: t("rp.desc_task"), onPick: () => handleAdd("task-summary") },
    { id: "browser", title: t("rp.tab_browser"), desc: t("rp.desc_browser"), onPick: () => handleAdd("browser") },
    { id: "terminal", title: t("rp.tab_terminal"), desc: t("rp.desc_terminal"), onPick: handleAddTerminal },
    { id: "files", title: t("rp.tab_files_full"), desc: t("rp.desc_files"), onPick: () => handleAdd("files") },
    { id: "workflow", title: t("rp.tab_workflow"), desc: t("rp.desc_workflow"), onPick: () => handleAdd("workflow") },
    { id: "side", title: "侧边对话", desc: "单独提问，可按需挑选主对话的上下文", onPick: () => handleAdd("side") },
    { id: "git", title: "Git", desc: "查看变更、暂存、提交、同步与切换分支", onPick: () => handleAdd("git") },
    ...(debugEnabled ? [{ id: "debug" as const, title: t("rp.tab_debug"), desc: t("rp.desc_debug"), onPick: () => handleAdd("debug") }] : []),
  ];

  return (
    <div className="right-panel">
      <div className="rp-head">
        <div className={"rp-tabs" + (tabsOverflow ? " is-overflow" : "")} ref={tabsRef} onWheel={onTabsWheel} role="tablist">
          {ind && <span className="rp-tab-ind" aria-hidden style={{ transform: `translateX(${ind.x}px)`, width: ind.w }} />}
          {tabs.map((tTab) => {
            const key = `${tTab.id}-${tTab.instance}`;
            const label = tabLabel(tTab);
            const active = activeKey === key;
            return (
              <div
                key={key}
                className={`rp-tab${active ? " is-active" : ""}`}
                role="tab"
                aria-selected={active}
                tabIndex={0}
                onClick={() => setActiveTab(key)}
                onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setActiveTab(key); } }}
              >
                <span className="rp-tab-ic">{tabIcon(tTab.id, 14)}</span>
                <span className="rp-tab-label">{label}</span>
                {tTab.id === "browser" && browserAgentBusy && <span className="rp-tab-ai-dot" aria-label="AI 正在操作浏览器" />}
                <Tooltip content={t("rp.close_tab")} side="bottom">
                  <button type="button" className="rp-tab-close" aria-label={`${t("rp.close_tab")}：${label}`} onClick={(e) => { e.stopPropagation(); closeTab(key); }}>
                    <IconX size={11} />
                  </button>
                </Tooltip>
              </div>
            );
          })}
          {tabs.length === 0 && <div className="rp-tabs-empty">{t("rp.tabs_empty")}</div>}
        </div>
        <div className="rp-head-actions">
          {tabsOverflow && (
            <div className="rp-anchor">
              <Tooltip content={t("rp.all_tabs")} side="bottom" disabled={showTabsMenu}>
                <button
                  ref={tabsBtnRef}
                  type="button"
                  className={"rp-hbtn" + (showTabsMenu ? " is-open" : "")}
                  aria-label={t("rp.all_tabs")}
                  aria-expanded={showTabsMenu}
                  onClick={() => { setShowTabsMenu((v) => !v); setShowAddMenu(false); }}
                >
                  <IconChevronDown size={14} />
                </button>
              </Tooltip>
              <Popover open={showTabsMenu} onClose={() => setShowTabsMenu(false)} placement="bottom-end" className="rp-menu" ignoreRefs={[tabsBtnRef]} role="menu" aria-label={t("rp.all_tabs")}>
                {tabs.map((tTab) => {
                  const key = `${tTab.id}-${tTab.instance}`;
                  const active = activeKey === key;
                  return (
                    <button key={key} type="button" role="menuitem" className={"pop-item" + (active ? " is-selected" : "")} onClick={() => { setActiveTab(key); setShowTabsMenu(false); }}>
                      <span className="pop-item-icon">{tabIcon(tTab.id, 15)}</span>
                      <span className="pop-item-title">{tabLabel(tTab)}</span>
                      {active && <span className="pop-item-end"><span className="pop-item-check"><IconCheck size={14} /></span></span>}
                    </button>
                  );
                })}
              </Popover>
            </div>
          )}
          <div className="rp-anchor">
            <Tooltip content={t("rp.new_tab")} side="bottom" disabled={showAddMenu}>
              <button
                ref={addBtnRef}
                type="button"
                className={"rp-hbtn" + (showAddMenu ? " is-open" : "")}
                aria-label={t("rp.new_tab")}
                aria-expanded={showAddMenu}
                onClick={() => { const next = !showAddMenu; setShowAddMenu(next); setShowTabsMenu(false); if (next) refreshDebugEnabled(); }}
              >
                <IconPlus size={15} />
              </button>
            </Tooltip>
            <Popover open={showAddMenu} onClose={() => setShowAddMenu(false)} placement="bottom-end" className="rp-menu rp-menu-add" ignoreRefs={[addBtnRef]} role="menu" aria-label={t("rp.new_tab")}>
              {entries.map((en) => (
                <button key={en.id} type="button" role="menuitem" className="pop-item" onClick={en.onPick}>
                  <span className="pop-item-icon">{tabIcon(en.id, 16)}</span>
                  <span className="pop-item-main">
                    <span className="pop-item-title">{en.title}</span>
                    <span className="pop-item-desc">{en.desc}</span>
                  </span>
                </button>
              ))}
              {closedStack.length > 0 && (
                <>
                  <div className="pop-sep" />
                  <div className="pop-label">{t("rp.recent_closed")}</div>
                  {closedStack.slice(0, 5).map((tTab, i) => (
                    <button key={`${tTab.id}-${tTab.instance}`} type="button" role="menuitem" className="pop-item" onClick={() => { reopenClosedTab(i); setShowAddMenu(false); }}>
                      <span className="pop-item-icon"><IconRotateCcw size={14} /></span>
                      <span className="pop-item-title">{tabLabel(tTab)}</span>
                    </button>
                  ))}
                </>
              )}
            </Popover>
          </div>
          <Tooltip content={fullscreen ? t("rp.scale") : t("rp.fullscreen")} side="bottom">
            <button type="button" className={`rp-hbtn${fullscreen ? " is-on" : ""}`} aria-label={fullscreen ? t("rp.scale") : t("rp.fullscreen")} aria-pressed={fullscreen} onClick={toggleFullscreen}>
              {fullscreen ? <IconMinus size={14} /> : <IconMaximize size={14} />}
            </button>
          </Tooltip>
          <Tooltip content={t("rp.collapse")} side="bottom">
            <button type="button" className="rp-hbtn" aria-label={t("rp.collapse")} onClick={closePanel}>
              <IconArrowToggle open={false} size={14} />
            </button>
          </Tooltip>
        </div>
      </div>
      <div className="rp-content">
        {/* plan-41-233：跨会话保活渲染——所有会话桶内的标签实例都保持挂载（display 切换可见性），
            切换会话 / 折叠面板都不会卸载终端与浏览器（PTY 不关闭、网页不刷新）。
            可见性 = 「属于当前会话桶」且「是该桶的激活标签」。
            plan-282-1421（第3项）：激活 tab 播放统一过渡（类名 "" → "ui-tab-enter" 触发动画）。 */}
        {Object.entries(buckets).flatMap(([bKey, bucket]) =>
          bucket.tabs.map((tTab) => {
            const key = `${tTab.id}-${tTab.instance}`;
            const isActive = bKey === activeBucket && key === activeKey;
            // plan-20261015-browser-agent：浏览器标签不能 display:none（网页视口会变 0×0，AI 无法在后台操作），
            // 改为不可见但保留布局；其中的 webview 由 BrowserPanel 停泊在视口内继续渲染
            const keepAlive = !isActive && tTab.id === "browser";
            return (
              <div
                key={`${bKey}:${key}`}
                className={isActive ? "ui-tab-enter" : keepAlive ? "rp-keepalive" : ""}
                style={{ display: isActive || keepAlive ? "block" : "none", height: "100%" }}
                aria-hidden={!isActive || undefined}
              >
                <GuardedPanelContent tabKey={`${bKey}:${key}`} tab={tTab} sessionId={bucketSessionId(bKey)} visible={isActive} />
              </div>
            );
          })
        )}
        {tabs.length === 0 && (
          <div className="rp-launch">
            <div className="rp-launch-inner">
              <div className="rp-launch-head">
                <div className="rp-launch-title">{t("rp.launch_title")}</div>
                <div className="rp-launch-sub">{t("rp.launch_sub")}</div>
              </div>
              <div className="rp-launch-list" role="list">
                {entries.map((en) => (
                  <Tooltip key={en.id} content={en.desc} side="bottom">
                    <button type="button" role="listitem" className="rp-launch-tile" onClick={en.onPick}>
                      <span className="rp-launch-ic">{tabIcon(en.id, 18)}</span>
                      <span className="rp-launch-name">{en.title}</span>
                    </button>
                  </Tooltip>
                ))}
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
