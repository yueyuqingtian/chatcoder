/** 窗口右上角控件组（plan-20261025-shell）：[全屏][右面板开关] | [最小化][最大化][关闭]。
 *
 *  与左上角 WindowNavCluster 对称：全局固定层，钉在窗口右上角，不随卡片换 DOM。
 *  右侧面板收起时它压在中间卡片标题栏的右端，展开后压在右面板卡片标签栏的右端——
 *  按钮的屏幕位置始终不变，面板展开 / 收起时不会出现「按钮从一张卡片跳到另一张」的闪动；
 *  「全屏」只在面板展开时出现，「面板开关」只在会话页出现（新建任务页、设置等页面态没有右面板），
 *  两者都是宽度可收拢的固定槽位，显隐走过渡，不推挤窗口按钮。
 *  卡片为它预留的宽度见 shell.css 的 --wc-w / --wc-full-w，改按钮数量或尺寸时两处同步。 */
import type { CSSProperties } from "react";
import { useChatStore } from "../store/chat";
import { usePanelStore } from "../store/panel";
import { sessionKey, useBrowserStore } from "../store/browser";
import { useI18n } from "../store/i18n";
import { Tooltip } from "./ui";
import { IconMaximize, IconMinimize, IconMinus, IconPanelRight, IconSquare, IconX } from "./icons";

/** 按钮级 no-drag（plan-31-152 S5-2 同款加固）：不依赖选择器继承，避免点击被系统拖拽命中区吞掉 */
const NO_DRAG = { WebkitAppRegion: "no-drag" } as CSSProperties;

export function WindowControls({ panelAvailable, panelShown, fullscreen, onTogglePanel }: {
  /** 当前页面是否有右面板（会话页才有） */
  panelAvailable: boolean;
  /** 右面板是否展开在屏幕上 */
  panelShown: boolean;
  fullscreen: boolean;
  onTogglePanel: () => void;
}) {
  const { t } = useI18n();
  const currentSessionId = useChatStore((s) => s.currentSessionId);
  // 面板收起时 AI 仍可能在后台操作浏览器——开关上给出状态点（与此前标题栏开关同源）
  const browserAgentBusy = useBrowserStore((s) => (s.sessions[sessionKey(currentSessionId)]?.agent.phase ?? "idle") === "working");
  const toggleFullscreen = usePanelStore((s) => s.toggleFullscreen);
  const winApi = window.chatcoderAPI;
  const fsLabel = fullscreen ? t("rp.scale") : t("rp.fullscreen");
  const panelLabel = !panelShown && browserAgentBusy
    ? "AI 正在操作浏览器，展开面板查看"
    : panelShown ? t("titlebar.panel_collapse") : t("titlebar.panel_expand");

  return (
    <div className={"win-cluster" + (panelShown ? " is-panel" : "") + (panelAvailable ? " has-panel" : "")}>
      <span className="wc-slot wc-slot-fs" aria-hidden={!panelShown || undefined}>
        <Tooltip content={fsLabel} side="bottom" disabled={!panelShown}>
          <button
            type="button"
            className={"wc-btn" + (fullscreen ? " is-on" : "")}
            style={NO_DRAG}
            tabIndex={panelShown ? undefined : -1}
            aria-label={fsLabel}
            aria-pressed={fullscreen}
            onClick={toggleFullscreen}
          >
            {fullscreen ? <IconMinimize size={16} /> : <IconMaximize size={16} />}
          </button>
        </Tooltip>
      </span>
      <span className="wc-slot wc-slot-panel" aria-hidden={!panelAvailable || undefined}>
        <Tooltip content={panelLabel} side="bottom" disabled={!panelAvailable}>
          <button
            type="button"
            className="wc-btn"
            style={NO_DRAG}
            tabIndex={panelAvailable ? undefined : -1}
            aria-label={panelLabel}
            aria-expanded={panelShown}
            onClick={onTogglePanel}
          >
            <IconPanelRight size={16} open={panelShown} />
            {!panelShown && browserAgentBusy && <span className="tb-ai-dot" aria-hidden />}
          </button>
        </Tooltip>
        <span className="wc-sep" aria-hidden />
      </span>
      <Tooltip content={t("titlebar.min")} side="bottom">
        <button type="button" className="wc-btn" style={NO_DRAG} aria-label={t("titlebar.min")} disabled={!winApi}
          onClick={() => winApi?.minimizeWindow?.()}>
          <IconMinus size={16} />
        </button>
      </Tooltip>
      <Tooltip content={t("titlebar.max")} side="bottom">
        <button type="button" className="wc-btn" style={NO_DRAG} aria-label={t("titlebar.max")} disabled={!winApi}
          onClick={() => winApi?.toggleMaximize?.()}>
          <IconSquare size={14} />
        </button>
      </Tooltip>
      <Tooltip content={t("titlebar.close")} side="bottom">
        <button type="button" className="wc-btn wc-close" style={NO_DRAG} aria-label={t("titlebar.close")} disabled={!winApi}
          onClick={() => winApi?.closeWindow?.()}>
          <IconX size={16} />
        </button>
      </Tooltip>
    </div>
  );
}
