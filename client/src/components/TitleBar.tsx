/** 自定义标题栏 v4 r2：
 * 左：品牌图标 + 折叠侧栏钮
 * 右：折叠右面板钮 + 主题切换 + 窗口控制
 */
import { useThemeStore } from "../store/theme";
import { usePanelStore } from "../store/panel";
import {
  IconSun, IconMoon, IconMinus, IconSquare, IconX,
  IconMessageSquare, IconPanelLeft, IconPanelRight,
  IconCheckSquare, IconArrowToggle,
} from "./icons";

interface TitleBarProps {
  leftCollapsed: boolean;
  rightCollapsed: boolean;
  onToggleLeft: () => void;
  onToggleRight: () => void;
}

export function TitleBar({
  leftCollapsed,
  rightCollapsed,
  onToggleLeft,
  onToggleRight,
}: TitleBarProps) {
  const { theme, toggle } = useThemeStore();
  const taskCardVisible = usePanelStore((s) => s.taskCardVisible);
  const toggleTaskCard = usePanelStore((s) => s.toggleTaskCard);
  const api = (window as Window & { chatcoderAPI?: WindowAPI }).chatcoderAPI;

  return (
    <div className="titlebar title-drag-region">
      <div className="titlebar-left title-no-drag">
        <span className="titlebar-brand-icon"><IconMessageSquare size={14} /></span>
        <span className="titlebar-brand">chatcoder</span>
        <button
          className={`app-pane-toggle titlebar-btn${leftCollapsed ? " collapsed" : ""}`}
          onClick={onToggleLeft}
          title={leftCollapsed ? "展开会话栏" : "收起会话栏"}
        >
          <IconPanelLeft size={14} />
          <IconArrowToggle open={!leftCollapsed} size={12} />
        </button>
      </div>

      <div className="titlebar-mid" />

      <div className="titlebar-right title-no-drag">
        {/* v10: 主窗口任务卡显隐开关（位于"收起任务栏"按钮左侧） */}
        <button
          className={`app-pane-toggle titlebar-btn${taskCardVisible ? "" : " collapsed"}`}
          onClick={toggleTaskCard}
          title={taskCardVisible ? "收起任务卡" : "展开任务卡"}
        >
          <IconCheckSquare size={14} />
          <IconArrowToggle open={taskCardVisible} size={12} />
        </button>
        <button
          className={`app-pane-toggle titlebar-btn${rightCollapsed ? " collapsed" : ""}`}
          onClick={onToggleRight}
          title={rightCollapsed ? "展开任务栏" : "收起任务栏"}
        >
          <IconPanelRight size={14} />
          <IconArrowToggle open={!rightCollapsed} size={12} />
        </button>
        <button
          className="titlebar-btn"
          onClick={toggle}
          title="切换主题"
        >
          {theme === "dark" ? <IconSun size={14} /> : <IconMoon size={14} />}
        </button>
        <button
          className="titlebar-btn"
          onClick={() => api?.minimizeWindow()}
          title="最小化"
          disabled={!api}
        >
          <IconMinus size={14} />
        </button>
        <button
          className="titlebar-btn"
          onClick={() => api?.toggleMaximize()}
          title="最大化/还原"
          disabled={!api}
        >
          <IconSquare size={12} />
        </button>
        <button
          className="titlebar-btn titlebar-close"
          onClick={() => api?.closeWindow()}
          title="关闭"
          disabled={!api}
        >
          <IconX size={14} />
        </button>
      </div>
    </div>
  );
}

interface WindowAPI {
  minimizeWindow: () => void;
  toggleMaximize: () => void;
  closeWindow: () => void;
}
