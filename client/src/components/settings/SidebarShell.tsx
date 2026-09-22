/** v19: 侧栏共用壳——外部会话侧栏与设置页侧栏共用同一容器/头部/列表样式，
 * 宽度统一由 App 的 leftPanelWidth + ResizeHandle 控制，顶部与主布局连续（不再 overlay 割裂）。
 */
import type { ReactNode } from "react";
import { IconArrowLeft } from "../icons";
import { AppLogo } from "../AppLogo";
import { NAV_GROUPS, SETTINGS_INDEX, type SettingsTab } from "./index";
import { useI18n } from "../../store/i18n";
import { useWindowDrag } from "../../hooks/useWindowDrag";

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
  const { t } = useI18n();
  // plan-26-126 P2：设置侧栏头部同样是拖拽/双击伪全屏区
  const { onPointerDown, onPointerMove, onPointerUp, onDoubleClick: onTitleDoubleClick } = useWindowDrag();
  return (
    <SidebarShell collapsed={collapsed}>
      <div className="sb-head" onPointerDown={onPointerDown} onPointerMove={onPointerMove} onPointerUp={onPointerUp} onDoubleClick={onTitleDoubleClick}>
        <AppLogo size={20} className="sb-logo-img" />
        {collapsed && (
          <button className="sb-nav-arrow" onClick={onBack} title={t("titlebar.back")} type="button">
            <IconArrowLeft size={15} />
          </button>
        )}
      </div>
      {!collapsed && (
        <button className="sb-back-item" onClick={onBack} title={t("titlebar.back")} type="button">
          <IconArrowLeft size={14} />
          <span>{t("titlebar.back")}</span>
        </button>
      )}
      {!collapsed && (
        <div className="sb-list">
          {NAV_GROUPS.map((g) => (
            <div key={g.id} className="sb-section-group">
              <div className="sb-section-label">{t(`settingsGroup.${g.id}`)}</div>
              {SETTINGS_INDEX.filter((it) => it.group === g.id).map((it) => (
                <div
                  key={it.key}
                  className={`sb-nav-item${tab === it.key ? " active" : ""}`}
                  onClick={() => onTab(it.key)}
                  title={t(`settings.tab.${it.key}`)}
                >
                  <span className="sb-nav-icon">{it.icon}</span>
                  <span className="sb-nav-label">{t(`settings.tab.${it.key}`)}</span>
                </div>
              ))}
            </div>
          ))}
        </div>
      )}
    </SidebarShell>
  );
}
