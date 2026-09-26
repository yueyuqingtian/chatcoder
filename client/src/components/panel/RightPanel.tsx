/** 右侧面板（v5）：多标签页 + 全屏按钮 + 加号菜单修复。
 * v19: 标签溢出治理——tab 不再被 flex 压缩（flex-shrink:0 + 文字省略），
 *      支持滚轮横滚；溢出时头部出现「全部标签」下拉，可直接跳转/关闭。
 */
import { memo, useEffect, useRef, useState } from "react";
import { api } from "../../api/client";
import { isBusy, subscribe } from "../../perf/bus";
import { usePanelStore, bucketSessionId } from "../../store/panel";
import type { PanelTab, PanelTabId } from "../../store/panel";
import { useI18n } from "../../store/i18n";
import { IconArrowToggle, IconBug, IconChevronDown, IconFolder, IconGlobe, IconTerminal, IconX, IconPlus, IconMaximize, IconMinus } from "../icons";
import { TaskSummaryPanel } from "./TaskSummaryPanel";
import { BrowserPanel } from "./BrowserPanel";
import { FileTreePanel } from "./FileTreePanel";
import { TerminalPanel } from "./TerminalPanel";
import { SubagentPanel } from "./SubagentPanel";
import { DebugPanel } from "./DebugPanel";
import { useClickOutside } from "../../hooks/useClickOutside";
import { ErrorBoundary } from "../ErrorBoundary";

/** plan-75-334 阶段3：面板内容按 tab/sessionId/visible 记忆化。
 *  此前标签列表无关变化（新增/关闭其它标签、全屏切换等）会重建**全部**面板内容，
 *  包括已保活的终端与浏览器；memo 后只有本标签自身的可见性与 tab 变化才重渲染。 */
const PanelContent = memo(function PanelContent({ tab, sessionId, visible }: { tab: PanelTab; sessionId: number | null; visible: boolean }) {
  switch (tab.id) {
    case "task-summary": return <TaskSummaryPanel visible={visible} />;
    case "browser": return <BrowserPanel sessionId={sessionId} visible={visible} />;
    case "terminal": return <TerminalPanel tab={tab} sessionId={sessionId} visible={visible} />;
    case "files": return <FileTreePanel visible={visible} />;
    case "subagent": return <SubagentPanel threadId={tab.meta?.threadId} agentName={tab.meta?.agentName} visible={visible} />;
    case "debug": return <DebugPanel sessionId={sessionId} />;
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
  const headActionsRef = useRef<HTMLDivElement>(null);
  const [tabsOverflow, setTabsOverflow] = useState(false);
  useClickOutside(headActionsRef, showAddMenu || showTabsMenu, () => {
    setShowAddMenu(false);
    setShowTabsMenu(false);
  });

  const tabMeta: Record<PanelTabId, { label: string; icon: React.ReactNode }> = {
    "task-summary": { label: t("rp.tab_task_summary"), icon: <span className="rp-tab-dot" /> },
    browser: { label: t("rp.tab_browser"), icon: <IconGlobe size={13} /> },
    terminal: { label: t("rp.tab_terminal"), icon: <IconTerminal size={13} /> },
    files: { label: t("rp.tab_files"), icon: <IconFolder size={13} /> },
    subagent: { label: t("rp.tab_subagent"), icon: <IconTerminal size={13} /> },
    debug: { label: t("rp.tab_debug"), icon: <IconBug size={13} /> },
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
      setTabsOverflow(el.scrollWidth > el.clientWidth + 2);
    };
    check();
    const ro = new ResizeObserver(check);
    ro.observe(el);
    const off = subscribe((busy) => { if (!busy && missed) check(); });
    return () => { ro.disconnect(); off(); };
  }, [tabs.length]);

  // 滚轮纵转横：标签过多时直接滚轮浏览
  const onTabsWheel = (e: React.WheelEvent) => {
    const el = tabsRef.current;
    if (!el || el.scrollWidth <= el.clientWidth) return;
    e.preventDefault();
    el.scrollLeft += e.deltaY;
  };

  const handleAdd = (id: PanelTabId) => { openTab(id); setShowAddMenu(false); };
  const handleAddTerminal = () => { openNewTab("terminal"); setShowAddMenu(false); };

  return (
    <div className="right-panel">
      <div className="rp-head">
        <div className="rp-tabs" ref={tabsRef} onWheel={onTabsWheel}>
          {tabs.map((tTab) => {
            const key = `${tTab.id}-${tTab.instance}`;
            const meta = tabMeta[tTab.id];
            const label = tTab.id === "subagent" && tTab.meta?.agentName
              ? tTab.meta.agentName.slice(0, 10)
              : tTab.instance > 1 ? `${meta.label} ${tTab.instance}` : meta.label;
            return (
              <div key={key} className={`rp-tab${activeKey === key ? " active" : ""}`} title={label} onClick={() => setActiveTab(key)}>
                {meta.icon}
                <span>{label}</span>
                <button className="rp-tab-close" onClick={(e) => { e.stopPropagation(); closeTab(key); }}><IconX size={10} /></button>
              </div>
            );
          })}
          {tabs.length === 0 && <div className="rp-tabs-empty">{t("rp.tabs_empty")}</div>}
        </div>
        <div className="rp-head-actions" ref={headActionsRef}>
          {tabsOverflow && (
            <button className="rp-add-btn" onClick={() => { setShowTabsMenu(!showTabsMenu); setShowAddMenu(false); }} title={t("rp.all_tabs")}>
              <IconChevronDown size={14} />
            </button>
          )}
          {showTabsMenu && (
            <div className="rp-add-menu" onClick={() => setShowTabsMenu(false)}>
              {tabs.map((tTab) => {
                const key = `${tTab.id}-${tTab.instance}`;
                const meta = tabMeta[tTab.id];
                const label = tTab.id === "subagent" && tTab.meta?.agentName
                  ? tTab.meta.agentName.slice(0, 10)
                  : tTab.instance > 1 ? `${meta.label} ${tTab.instance}` : meta.label;
                return (
                  <button key={key} className={activeKey === key ? "active" : ""} onClick={() => setActiveTab(key)}>
                    {meta.icon} <span style={{ flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis" }}>{label}</span>
                  </button>
                );
              })}
            </div>
          )}
          <button className="rp-add-btn" onClick={() => { const next = !showAddMenu; setShowAddMenu(next); setShowTabsMenu(false); if (next) refreshDebugEnabled(); }} title={t("rp.new_tab")}><IconPlus size={14} /></button>
          {showAddMenu && (
            <div className="rp-add-menu" onClick={() => setShowAddMenu(false)}>
              <button onClick={() => handleAdd("task-summary")}>{t("rp.tab_task_summary")}</button>
              <button onClick={() => handleAdd("browser")}>{t("rp.tab_browser")}</button>
              <button onClick={() => handleAddTerminal()}>{t("rp.tab_terminal")}</button>
              <button onClick={() => handleAdd("files")}>{t("rp.tab_files_full")}</button>
              {debugEnabled && (
                <button onClick={() => handleAdd("debug")}>{t("rp.tab_debug")}</button>
              )}
              {closedStack.length > 0 && (
                <>
                  <div className="rp-add-menu-divider" />
                  {closedStack.slice(0, 5).map((tTab, i) => {
                    const meta = tabMeta[tTab.id];
                    const label = tTab.id === "subagent" && tTab.meta?.agentName
                      ? tTab.meta.agentName.slice(0, 10)
                      : tTab.instance > 1 ? `${meta.label} ${tTab.instance}` : meta.label;
                    return (
                      <button key={`${tTab.id}-${tTab.instance}`} onClick={() => reopenClosedTab(i)} title={t("rp.restore_closed")}>
                        ↺ {label}
                      </button>
                    );
                  })}
                </>
              )}
            </div>
          )}
          <button className={`rp-fullscreen-btn${fullscreen ? " active" : ""}`} onClick={toggleFullscreen} title={fullscreen ? t("rp.scale") : t("rp.fullscreen")}>
            {fullscreen ? <IconMinus size={14} /> : <IconMaximize size={14} />}
          </button>
          <button className="rp-collapse-btn" onClick={closePanel} title={t("rp.collapse")}><IconArrowToggle open={false} size={14} /></button>
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
            return (
              <div
                key={`${bKey}:${key}`}
                className={isActive ? "ui-tab-enter" : ""}
                style={{ display: isActive ? "block" : "none", height: "100%" }}
              >
                <GuardedPanelContent tabKey={`${bKey}:${key}`} tab={tTab} sessionId={bucketSessionId(bKey)} visible={isActive} />
              </div>
            );
          })
        )}
        {tabs.length === 0 && (
          <div className="rp-quick">
            <button onClick={() => openTab("task-summary")}>{t("rp.tab_task_summary")}</button>
            <button onClick={() => openTab("browser")}>{t("rp.tab_browser")}</button>
            <button onClick={() => openNewTab("terminal")}>{t("rp.tab_terminal")}</button>
            <button onClick={() => openTab("files")}>{t("rp.tab_files_full")}</button>
            {debugEnabled && <button onClick={() => openTab("debug")}>{t("rp.tab_debug")}</button>}
          </div>
        )}
      </div>
    </div>
  );
}
