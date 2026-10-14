/** 窗口左上角导航控件组（plan-128-518）：logo + 折叠侧栏 + 前进 / 后退。
 *
 *  为什么提成全局固定层：此前展开态这组控件在左栏的 .sb-head 里，折叠态却换成标题栏里的另一份 DOM——
 *  折叠瞬间是「卸载一套、挂载另一套」，两套尺寸与间距还不一致，所以 logo 与相邻图标会跳变。
 *  现在只有这一份，绝对定位钉在窗口左上角：折叠 / 展开 / 设置页 / 页面态位置恒定、永不卸载，
 *  折叠时只有左栏背景、分隔线与中间面板向左移动。 */
import { useChatStore } from "../store/chat";
import { useI18n } from "../store/i18n";
import { AppLogo } from "./AppLogo";
import { Tooltip } from "./ui";
import { IconChevronLeft, IconChevronRight, IconPanelLeft } from "./icons";

export function WindowNavCluster({ collapsed, onToggle, onSessionFocus }: {
  collapsed: boolean;
  onToggle: () => void;
  onSessionFocus: () => void;
}) {
  const { t } = useI18n();
  const sessionHist = useChatStore((s) => s.sessionHist);
  const sessionHistIdx = useChatStore((s) => s.sessionHistIdx);
  const histGo = useChatStore((s) => s.histGo);
  const canBack = sessionHistIdx > 0;
  const canForward = sessionHistIdx >= 0 && sessionHistIdx < sessionHist.length - 1;
  const toggleLabel = collapsed ? t("sidebar.expand_tip") : t("sidebar.collapse_tip");

  return (
    <div className="nav-cluster">
      <AppLogo size={20} className="sb-logo-img" />
      <Tooltip content={toggleLabel} side="bottom">
        <button type="button" className="sb-nav-arrow" aria-label={toggleLabel} onClick={onToggle}>
          <IconPanelLeft size={16} open={!collapsed} />
        </button>
      </Tooltip>
      <Tooltip content={t("sidebar.history_back")} side="bottom">
        <button type="button" className="sb-nav-arrow" aria-label={t("sidebar.history_back")} disabled={!canBack} onClick={() => { histGo(-1); onSessionFocus(); }}>
          <IconChevronLeft size={16} />
        </button>
      </Tooltip>
      <Tooltip content={t("sidebar.history_forward")} side="bottom">
        <button type="button" className="sb-nav-arrow" aria-label={t("sidebar.history_forward")} disabled={!canForward} onClick={() => { histGo(1); onSessionFocus(); }}>
          <IconChevronRight size={16} />
        </button>
      </Tooltip>
    </div>
  );
}
