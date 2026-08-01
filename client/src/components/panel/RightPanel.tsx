/** 右侧面板（v5）：多标签页 + 全屏按钮 + 加号菜单修复。 */
import { useState } from "react";
import { usePanelStore } from "../../store/panel";
import type { PanelTabId } from "../../store/panel";
import { IconArrowToggle, IconFolder, IconGlobe, IconTerminal, IconX, IconPlus, IconMaximize, IconMinus } from "../icons";
import { TaskSummaryPanel } from "./TaskSummaryPanel";
import { BrowserPanel } from "./BrowserPanel";
import { FileTreePanel } from "./FileTreePanel";
import { TerminalPanel } from "./TerminalPanel";

const TAB_META: Record<PanelTabId, { label: string; icon: React.ReactNode }> = {
  "task-summary": { label: "任务摘要", icon: <span className="rp-tab-dot" /> },
  browser: { label: "浏览器", icon: <IconGlobe size={13} /> },
  terminal: { label: "终端", icon: <IconTerminal size={13} /> },
  files: { label: "文件", icon: <IconFolder size={13} /> },
};

function PanelContent({ id }: { id: PanelTabId }) {
  switch (id) {
    case "task-summary": return <TaskSummaryPanel />;
    case "browser": return <BrowserPanel />;
    case "terminal": return <TerminalPanel />;
    case "files": return <FileTreePanel />;
    default: return null;
  }
}

export function RightPanel() {
  const { tabs, activeKey, closePanel, openTab, closeTab, setActiveTab, fullscreen, toggleFullscreen } = usePanelStore();
  const [showAddMenu, setShowAddMenu] = useState(false);

  const handleAdd = (id: PanelTabId) => { openTab(id); setShowAddMenu(false); };

  return (
    <div className="right-panel">
      <div className="rp-head">
        <div className="rp-tabs">
          {tabs.map((t) => {
            const key = `${t.id}-${t.instance}`;
            const meta = TAB_META[t.id];
            return (
              <div key={key} className={`rp-tab${activeKey === key ? " active" : ""}`} onClick={() => setActiveTab(key)}>
                {meta.icon}
                <span>{t.instance > 1 ? `${meta.label} ${t.instance}` : meta.label}</span>
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
              <button onClick={() => handleAdd("terminal")}>终端</button>
              <button onClick={() => handleAdd("files")}>文件管理</button>
            </div>
          )}
          <button className={`rp-fullscreen-btn${fullscreen ? " active" : ""}`} onClick={toggleFullscreen} title={fullscreen ? "缩放" : "全屏"}>
            {fullscreen ? <IconMinus size={14} /> : <IconMaximize size={14} />}
          </button>
          <button className="rp-collapse-btn" onClick={closePanel} title="折叠"><IconArrowToggle open={false} size={14} /></button>
        </div>
      </div>
      <div className="rp-content">
        {activeKey ? (() => {
          const active = tabs.find((t) => `${t.id}-${t.instance}` === activeKey);
          return active ? <PanelContent id={active.id} /> : null;
        })() : (
          <div className="rp-quick">
            <button onClick={() => openTab("task-summary")}>任务摘要</button>
            <button onClick={() => openTab("browser")}>浏览器</button>
            <button onClick={() => openTab("terminal")}>终端</button>
            <button onClick={() => openTab("files")}>文件管理</button>
          </div>
        )}
      </div>
    </div>
  );
}
