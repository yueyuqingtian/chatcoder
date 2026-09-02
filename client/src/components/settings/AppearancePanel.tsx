/** 设置中心：外观（v2.2 对齐 zcode 3.18）。
 * 主题模式、毛玻璃效果、布局宽度、字号、左侧面板外观。 */
import { useThemeStore, type Theme } from "../../store/theme";
import { useUiStore } from "../../store/ui";
import { Row, Sw } from "./shared";

const THEMES: Record<Theme, string> = { light: "浅色", dark: "深色" };

export function AppearancePanel() {
  const { theme, setTheme } = useThemeStore();
  const ui = useUiStore();
  return (
    <div>
      <div className="settings-card">
        <Row title="主题模式" desc="浅色 / 深色">
          <div style={{ display: "flex", gap: 6 }}>
            {(Object.keys(THEMES) as Theme[]).map((t) => (
              <button
                key={t}
                className={"settings-pill" + (theme === t ? " active" : "")}
                onClick={() => setTheme(t)}
              >
                {THEMES[t]}
              </button>
            ))}
          </div>
        </Row>
        <Row title="毛玻璃效果" desc="启用窗口与侧边栏半透明磨砂背景">
          <Sw checked={ui.glassmorphism} onChange={(v) => ui.setPrefs({ glassmorphism: v })} />
        </Row>
      </div>

      <div className="settings-card">
        <Row title="左侧面板宽度" desc="可在主界面直接拖拽分隔条调整">
          <div className="settings-slider-wrap">
            <input
              type="range"
              className="settings-slider"
              min={200}
              max={480}
              step={4}
              value={ui.leftPanelWidth}
              onChange={(e) => ui.setPrefs({ leftPanelWidth: Number(e.target.value) })}
            />
            <span className="settings-slider-value">{ui.leftPanelWidth}px</span>
          </div>
        </Row>
        <Row title="右侧面板宽度" desc="可在主界面直接拖拽分隔条调整">
          <div className="settings-slider-wrap">
            <input
              type="range"
              className="settings-slider"
              min={200}
              max={1200}
              step={10}
              value={ui.rightPanelWidth}
              onChange={(e) => ui.setPrefs({ rightPanelWidth: Number(e.target.value) })}
            />
            <span className="settings-slider-value">{ui.rightPanelWidth}px</span>
          </div>
        </Row>
        <Row title="对话字号" desc="控制对话消息的文字大小（立即生效）">
          <div className="settings-slider-wrap">
            <input
              type="range"
              className="settings-slider"
              min={11}
              max={18}
              step={1}
              value={ui.chatFontSize}
              onChange={(e) => ui.setPrefs({ chatFontSize: Number(e.target.value) })}
            />
            <span className="settings-slider-value">{ui.chatFontSize}px</span>
          </div>
        </Row>
        <Row title="消息行距" desc="控制对话消息的行间间距倍率（立即生效）">
          <div className="settings-slider-wrap">
            <input
              type="range"
              className="settings-slider"
              min={1.2}
              max={2.2}
              step={0.05}
              value={ui.chatLineHeight}
              onChange={(e) => ui.setPrefs({ chatLineHeight: Number(e.target.value) })}
            />
            <span className="settings-slider-value">{ui.chatLineHeight.toFixed(2)}</span>
          </div>
        </Row>
        <Row title="内容展示宽度" desc="0 表示不限制，填满可视区域">
          <div className="settings-slider-wrap">
            <input
              type="range"
              className="settings-slider"
              min={0}
              max={1200}
              step={50}
              value={ui.contentMaxWidth}
              onChange={(e) => ui.setPrefs({ contentMaxWidth: Number(e.target.value) })}
            />
            <span className="settings-slider-value">{ui.contentMaxWidth === 0 ? "不限" : ui.contentMaxWidth + "px"}</span>
          </div>
        </Row>
      </div>

      <div className="settings-card">
        <div className="settings-card-title">左侧面板外观</div>
        <Row title="文字大小" desc="左侧面板会话与导航文字大小">
          <div className="settings-slider-wrap">
            <input
              type="range"
              className="settings-slider"
              min={11}
              max={16}
              step={1}
              value={ui.sidebarFontSize}
              onChange={(e) => ui.setPrefs({ sidebarFontSize: Number(e.target.value) })}
            />
            <span className="settings-slider-value">{ui.sidebarFontSize}px</span>
          </div>
        </Row>
        <Row title="图标大小" desc="左侧面板图标尺寸">
          <div className="settings-slider-wrap">
            <input
              type="range"
              className="settings-slider"
              min={12}
              max={20}
              step={1}
              value={ui.sidebarIconSize}
              onChange={(e) => ui.setPrefs({ sidebarIconSize: Number(e.target.value) })}
            />
            <span className="settings-slider-value">{ui.sidebarIconSize}px</span>
          </div>
        </Row>
      </div>
    </div>
  );
}
