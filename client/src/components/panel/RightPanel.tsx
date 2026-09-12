/** 右侧面板（v5）：多标签页 + 全屏按钮 + 加号菜单修复。
 * v19: 标签溢出治理——tab 不再被 flex 压缩（flex-shrink:0 + 文字省略），
 *      支持滚轮横滚；溢出时头部出现「全部标签」下拉，可直接跳转/关闭。
 */
import { useEffect, useRef, useState } from "react";
import { usePanelStore } from "../../store/panel";
import type { PanelTab, PanelTabId } from "../../store/panel";
import { useI18n } from "../../store/i18n";
import { IconArrowToggle, IconChevronDown, IconFolder, IconGlobe, IconTerminal, IconX, IconPlus, IconMaximize, IconMinus } from "../icons";
import { TaskSummaryPanel } from "./TaskSummaryPanel";
import { BrowserPanel } from "./BrowserPanel";
import { FileTreePanel } from "./FileTreePanel";
import { TerminalPanel } from "./TerminalPanel";
import { SubagentPanel } from "./SubagentPanel";
import { useClickOutside } from "../../hooks/useClickOutside";

function PanelContent({ tab }: { tab: PanelTab }) {
  switch (tab.id) {
    case "task-summary": return <TaskSummaryPanel />;
    case "browser": return <BrowserPanel />;
    case "terminal": return <TerminalPanel tab={tab} />;
    case "files": return <FileTreePanel />;
    case "subagent": return <SubagentPanel threadId={tab.meta?.threadId} agentName={tab.meta?.agentName} />;
    default: return null;
  }
}

export function RightPanel() {
  const { t } = useI18n();
  const tabs = usePanelStore((s) => s.tabs);
  const activeKey = usePanelStore((s) => s.activeKey);
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
  };

  // 标签条溢出检测（scrollWidth > clientWidth 时展示「全部标签」入口）
  useEffect(() => {
    const el = tabsRef.current;
    if (!el) return;
    const check = () => setTabsOverflow(el.scrollWidth > el.clientWidth + 2);
    check();
    const ro = new ResizeObserver(check);
    ro.observe(el);
    return () => ro.disconnect();
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
          <button className="rp-add-btn" onClick={() => { setShowAddMenu(!showAddMenu); setShowTabsMenu(false); }} title={t("rp.new_tab")}><IconPlus size={14} /></button>
          {showAddMenu && (
            <div className="rp-add-menu" onClick={() => setShowAddMenu(false)}>
              <button onClick={() => handleAdd("task-summary")}>{t("rp.tab_task_summary")}</button>
              <button onClick={() => handleAdd("browser")}>{t("rp.tab_browser")}</button>
              <button onClick={() => handleAddTerminal()}>{t("rp.tab_terminal")}</button>
              <button onClick={() => handleAdd("files")}>{t("rp.tab_files_full")}</button>
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
        {tabs.length > 0 ? tabs.map((tTab) => {
          const key = `${tTab.id}-${tTab.instance}`;
          const isActive = key === activeKey;
          return (
            <div key={key} className={isActive ? "view-enter" : ""} style={{ display: isActive ? "block" : "none", height: "100%" }}>
              <PanelContent tab={tTab} />
            </div>
          );
        }) : (
          <div className="rp-quick">
            <button onClick={() => openTab("task-summary")}>{t("rp.tab_task_summary")}</button>
            <button onClick={() => openTab("browser")}>{t("rp.tab_browser")}</button>
            <button onClick={() => openNewTab("terminal")}>{t("rp.tab_terminal")}</button>
            <button onClick={() => openTab("files")}>{t("rp.tab_files_full")}</button>
          </div>
        )}
      </div>
    </div>
  );
}
