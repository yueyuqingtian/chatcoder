/** v19: 侧栏共用壳——外部会话侧栏与设置页侧栏共用同一容器/头部/列表样式，
 * 宽度统一由 App 的 leftPanelWidth + ResizeHandle 控制，顶部与主布局连续（不再 overlay 割裂）。
 */
import type { ReactNode } from "react";
import { IconArrowLeft } from "../icons";
import { NAV_GROUPS, SETTINGS_INDEX, type SettingsTab } from "./index";

export function SidebarShell({ collapsed, children, footer }: {
  collapsed: boolean;
  children: ReactNode;
  footer?: ReactNode;
}) {
  return (
    <nav className={`sidebar sb settings-sidebar${collapsed ? " collapsed" : ""}`}>
      {children}
      {footer}
    </nav>
  );
}

/** 设置页左侧导航（复用侧栏壳；顶部首项「返回工作区」） */
export function SettingsSidebar({ tab, onTab, onBack, collapsed }: {
  tab: SettingsTab;
  onTab: (t: SettingsTab) => void;
  onBack: () => void;
  collapsed: boolean;
}) {
  return (
    <SidebarShell collapsed={collapsed}>
      <div className="sb-head title-drag-region">
        <span className="sb-logo title-no-drag" title="chatcoder">C</span>
        {collapsed && (
          <button className="sb-nav-arrow title-no-drag" onClick={onBack} title="返回工作区" type="button">
            <IconArrowLeft size={15} />
          </button>
        )}
      </div>
      {!collapsed && (
        <button className="sb-back-item title-no-drag" onClick={onBack} title="返回工作区" type="button">
          <IconArrowLeft size={14} />
          <span>返回工作区</span>
        </button>
      )}
      {!collapsed && (
        <div className="sb-list">
          {NAV_GROUPS.map((g) => (
            <div key={g.id} className="sb-section-group">
              <div className="sb-section-label">{g.label}</div>
              {SETTINGS_INDEX.filter((it) => it.group === g.id).map((it) => (
                <div
                  key={it.key}
                  className={`sb-nav-item${tab === it.key ? " active" : ""}`}
                  onClick={() => onTab(it.key)}
                  title={it.label}
                >
                  <span className="sb-nav-icon">{it.icon}</span>
                  <span className="sb-nav-label">{it.label}</span>
                </div>
              ))}
            </div>
          ))}
        </div>
      )}
    </SidebarShell>
  );
}
