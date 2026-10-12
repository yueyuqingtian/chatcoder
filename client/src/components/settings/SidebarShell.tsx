/** v19: 侧栏共用壳——外部会话侧栏与设置页侧栏共用同一容器/头部/列表样式，
 * 宽度统一由 App 的 leftPanelWidth + ResizeHandle 控制，顶部与主布局连续（不再 overlay 割裂）。
 */
import type { ReactNode } from "react";
import { IconArrowLeft } from "../icons";
import { NAV_GROUPS, SETTINGS_INDEX, type SettingsTab } from "./index";
import { useI18n } from "../../store/i18n";

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
export function SettingsSidebar({ tab, onTab, onBack, collapsed = false }: {
  tab: SettingsTab;
  onTab: (t: SettingsTab) => void;
  onBack: () => void;
  collapsed?: boolean;
}) {
  const { t } = useI18n();
  // plan-31-151 S3：设置侧栏头部拖拽/双击改由系统 -webkit-app-region:drag 处理（见 global.css）。
  return (
    <SidebarShell collapsed={collapsed}>
      {/* plan-128-518：头部只是占位——logo / 折叠 / 前进后退由全局的 WindowNavCluster 悬浮在此；折叠不再卸载内容，由面板收窄裁切 */}
      <div className="sb-head" aria-hidden />
      {(
        <button className="sb-back-item" onClick={onBack} title={t("titlebar.back")} type="button">
          <IconArrowLeft size={14} />
          <span>{t("titlebar.back")}</span>
        </button>
      )}
      {(
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
