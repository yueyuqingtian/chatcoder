/** 右侧面板（v5）：多标签页 + 全屏按钮 + 加号菜单修复。 */
import { useState } from "react";
import { usePanelStore } from "../../store/panel";
import type { PanelTab, PanelTabId } from "../../store/panel";
import { IconArrowToggle, IconFolder, IconGlobe, IconTerminal, IconX, IconPlus, IconMaximize, IconMinus } from "../icons";
import { TaskSummaryPanel } from "./TaskSummaryPanel";
import { BrowserPanel } from "./BrowserPanel";
import { FileTreePanel } from "./FileTreePanel";
import { TerminalPanel } from "./TerminalPanel";
import { SubagentPanel } from "./SubagentPanel";

const TAB_META: Record<PanelTabId, { label: string; icon: React.ReactNode }> = {
  "task-summary": { label: "任务摘要", icon: <span className="rp-tab-dot" /> },
  browser: { label: "浏览器", icon: <IconGlobe size={13} /> },
  terminal: { label: "终端", icon: <IconTerminal size={13} /> },
  files: { label: "文件", icon: <IconFolder size={13} /> },
  subagent: { label: "子代理", icon: <IconTerminal size={13} /> },
};

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

  const handleAdd = (id: PanelTabId) => { openTab(id); setShowAddMenu(false); };
  // v2.2 (对齐 zcode 3.15): 终端每次新建实例（多终端标签并存）
  const handleAddTerminal = () => { openNewTab("terminal"); setShowAddMenu(false); };

  return (
    <div className="right-panel">
      <div className="rp-head">
        <div className="rp-tabs">
          {tabs.map((t) => {
            const key = `${t.id}-${t.instance}`;
            const meta = TAB_META[t.id];
            const label = t.id === "subagent" && t.meta?.agentName
              ? t.meta.agentName.slice(0, 10)
              : t.instance > 1 ? `${meta.label} ${t.instance}` : meta.label;
            return (
              <div key={key} className={`rp-tab${activeKey === key ? " active" : ""}`} onClick={() => setActiveTab(key)}>
                {meta.icon}
                <span>{label}</span>
                <button className="rp-tab-close" onClick={(e) => { e.stopPropagation(); closeTab(key); }}><IconX size={10} /></button>
              </div>
            );
          })}
          {tabs.length === 0 && <div className="rp-tabs-empty">点击 + 打开面板</div>}
        </div>
        <div className="rp-head-actions">
          <button className="rp-add-btn" onClick={() => setShowAddMenu(!showAddMenu)} title="新增标签"><IconPlus size={14} /></button>
          {showAddMenu && (
            <div className="rp-add-menu" onClick={() => setShowAddMenu(false)}>
              <button onClick={() => handleAdd("task-summary")}>任务摘要</button>
              <button onClick={() => handleAdd("browser")}>浏览器</button>
              <button onClick={() => handleAddTerminal()}>终端</button>
              <button onClick={() => handleAdd("files")}>文件管理</button>
              {/* v2.2 (对齐 zcode 3.3.3): 最近关闭标签恢复 */}
              {closedStack.length > 0 && (
                <>
                  <div className="rp-add-menu-divider" />
                  {closedStack.slice(0, 5).map((t, i) => {
                    const meta = TAB_META[t.id];
                    const label = t.id === "subagent" && t.meta?.agentName
                      ? t.meta.agentName.slice(0, 10)
                      : t.instance > 1 ? `${meta.label} ${t.instance}` : meta.label;
                    return (
                      <button key={`${t.id}-${t.instance}`} onClick={() => reopenClosedTab(i)} title="恢复最近关闭">
                        ↺ {label}
                      </button>
                    );
                  })}
                </>
              )}
            </div>
          )}
          <button className={`rp-fullscreen-btn${fullscreen ? " active" : ""}`} onClick={toggleFullscreen} title={fullscreen ? "缩放" : "全屏"}>
            {fullscreen ? <IconMinus size={14} /> : <IconMaximize size={14} />}
          </button>
          <button className="rp-collapse-btn" onClick={closePanel} title="折叠"><IconArrowToggle open={false} size={14} /></button>
        </div>
      </div>
      <div className="rp-content">
        {/* v2.2 (对齐 zcode 3.15): 保活渲染所有标签（终端切换不丢 PTY 会话），非激活 display:none */}
        {tabs.length > 0 ? tabs.map((t) => {
          const key = `${t.id}-${t.instance}`;
          const isActive = key === activeKey;
          return (
            <div key={key} className={isActive ? "view-enter" : ""} style={{ display: isActive ? "block" : "none", height: "100%" }}>
              <PanelContent tab={t} />
            </div>
          );
        }) : (
          <div className="rp-quick">
            <button onClick={() => openTab("task-summary")}>任务摘要</button>
            <button onClick={() => openTab("browser")}>浏览器</button>
            <button onClick={() => openNewTab("terminal")}>终端</button>
            <button onClick={() => openTab("files")}>文件管理</button>
          </div>
        )}
      </div>
    </div>
  );
}
