/** 设置中心：外观（v2.2 对齐 zcode 3.18）。
 * 主题模式、毛玻璃效果、布局宽度、字号、特殊文字颜色、左侧面板外观。 */
import { useThemeStore, type Theme } from "../../store/theme";
import { useUiStore, type UiPrefs } from "../../store/ui";
import { Row, Sw } from "./shared";

const THEMES: Record<Theme, string> = { light: "浅色", dark: "深色" };

export function AppearancePanel() {
  const { theme, setTheme } = useThemeStore();
  const ui = useUiStore();
  return (
    <div>
      <div className="settings-card">
        <Row title="主题模式" desc="浅色 / 深色">
          <div style={{ display: "flex", gap: 6 }}>{(Object.keys(THEMES) as Theme[]).map((t) => <button key={t} className={"settings-pill" + (theme === t ? " active" : "")} onClick={() => setTheme(t)}>{THEMES[t]}</button>)}</div>
        </Row>
        <Row title="毛玻璃效果" desc="启用半透明背景模糊">
          <Sw checked={ui.glassmorphism} onChange={(v) => ui.setPrefs({ glassmorphism: v })} />
        </Row>
        <Row title="玻璃强度" desc="左侧面板/主区磨砂模糊度">
          <div style={{ display: "flex", gap: 4 }}>{([["0", "轻柔"], ["1", "标准"], ["2", "深邃"]] as const).map(([v, label]) => <button key={v} className={"settings-pill" + (String(ui.glassStrength) === v ? " active" : "")} onClick={() => ui.setPrefs({ glassStrength: Number(v) })}>{label}</button>)}</div>
        </Row>
        <Row title="玻璃渐变颜色" desc="毛玻璃背后的环境光渐变双色">
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <span style={{ display: "inline-flex", alignItems: "center", gap: 4, fontSize: 11, color: "var(--text-3)" }}>起始<input type="color" className="settings-color-input" value={ui.glassGradientC1 || "#F5F5F5"} onChange={(e) => ui.setPrefs({ glassGradientC1: e.target.value })} /></span>
            <span style={{ display: "inline-flex", alignItems: "center", gap: 4, fontSize: 11, color: "var(--text-3)" }}>结束<input type="color" className="settings-color-input" value={ui.glassGradientC2 || "#F5F5F5"} onChange={(e) => ui.setPrefs({ glassGradientC2: e.target.value })} /></span>
            <button className="btn btn-ghost btn-xs" onClick={() => ui.setPrefs({ glassGradientC1: "", glassGradientC2: "" })}>重置</button>
          </div>
        </Row>
        <Row title="输入框聚焦光晕" desc="输入框聚焦时的呼吸光影颜色（毛玻璃模式）">
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <input type="color" className="settings-color-input" value={ui.composerGlowColor || "#8A4DFF"} onChange={(e) => ui.setPrefs({ composerGlowColor: e.target.value })} />
            <button className="btn btn-ghost btn-xs" onClick={() => ui.setPrefs({ composerGlowColor: "" })}>重置</button>
          </div>
        </Row>
        <Row title="阴影强度" desc="无阴影 / 轻柔 / 标准 / 深邃 / 戏剧">
          <div style={{ display: "flex", gap: 4 }}>{([["0", "无"], ["0.5", "轻柔"], ["1", "标准"], ["1.5", "深邃"], ["2", "戏剧"]] as const).map(([v, label]) => <button key={v} className={"settings-pill" + (String(ui.shadowStrength) === v ? " active" : "")} onClick={() => ui.setPrefs({ shadowStrength: Number(v) })}>{label}</button>)}</div>
        </Row>
      </div>

      <div className="settings-card">
        <Row title="左侧面板宽度" desc="可在主界面直接拖拽分隔条调整">
          <div className="settings-slider-wrap"><input type="range" className="settings-slider" min={200} max={480} step={4} value={ui.leftPanelWidth} onChange={(e) => ui.setPrefs({ leftPanelWidth: Number(e.target.value) })} /><span className="settings-slider-value">{ui.leftPanelWidth}px</span></div>
        </Row>
        <Row title="右侧面板宽度" desc="可在主界面直接拖拽分隔条调整">
          <div className="settings-slider-wrap"><input type="range" className="settings-slider" min={200} max={1200} step={10} value={ui.rightPanelWidth} onChange={(e) => ui.setPrefs({ rightPanelWidth: Number(e.target.value) })} /><span className="settings-slider-value">{ui.rightPanelWidth}px</span></div>
        </Row>
        <Row title="对话字号" desc="控制对话消息的文字大小">
          <div className="settings-slider-wrap"><input type="range" className="settings-slider" min={11} max={18} step={1} value={ui.chatFontSize} onChange={(e) => ui.setPrefs({ chatFontSize: Number(e.target.value) })} /><span className="settings-slider-value">{ui.chatFontSize}px</span></div>
        </Row>
        <Row title="消息行距" desc="控制对话消息的行间间距（倍率）">
          <div className="settings-slider-wrap"><input type="range" className="settings-slider" min={1.2} max={2.2} step={0.05} value={ui.chatLineHeight} onChange={(e) => ui.setPrefs({ chatLineHeight: Number(e.target.value) })} /><span className="settings-slider-value">{ui.chatLineHeight.toFixed(2)}</span></div>
        </Row>
        <Row title="内容展示宽度" desc="0 表示不限制">
          <div className="settings-slider-wrap"><input type="range" className="settings-slider" min={0} max={1200} step={50} value={ui.contentMaxWidth} onChange={(e) => ui.setPrefs({ contentMaxWidth: Number(e.target.value) })} /><span className="settings-slider-value">{ui.contentMaxWidth === 0 ? "不限" : ui.contentMaxWidth + "px"}</span></div>
        </Row>
      </div>

      <div className="settings-card">
        <div className="settings-card-title">特殊文字颜色</div>
        <div className="settings-color-grid">
          {([["chatCodeColor", "代码块", ui.chatCodeColor], ["chatHeadingColor", "标题", ui.chatHeadingColor], ["chatLinkColor", "链接", ui.chatLinkColor], ["chatQuoteColor", "引用", ui.chatQuoteColor]] as const).map(([key, label, val]) => (
            <div key={key} className="settings-color-item"><span>{label}</span><input type="color" className="settings-color-input" value={val} onChange={(e) => ui.setPrefs({ [key]: e.target.value } as Partial<UiPrefs>)} /></div>
          ))}
        </div>
      </div>

      <div className="settings-card">
        <div className="settings-card-title">左侧面板外观</div>
        <Row title="文字大小" desc="左侧面板会话与导航文字">
          <div className="settings-slider-wrap"><input type="range" className="settings-slider" min={10} max={16} step={1} value={ui.sidebarFontSize} onChange={(e) => ui.setPrefs({ sidebarFontSize: Number(e.target.value) })} /><span className="settings-slider-value">{ui.sidebarFontSize}px</span></div>
        </Row>
        <Row title="图标大小" desc="左侧面板图标尺寸">
          <div className="settings-slider-wrap"><input type="range" className="settings-slider" min={10} max={20} step={1} value={ui.sidebarIconSize} onChange={(e) => ui.setPrefs({ sidebarIconSize: Number(e.target.value) })} /><span className="settings-slider-value">{ui.sidebarIconSize}px</span></div>
        </Row>
        <Row title="聚焦颜色" desc="选中会话/导航的强调色（留空使用默认）">
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <input type="color" className="settings-color-input" value={ui.sidebarFocusColor || "#1A1A1E"} onChange={(e) => ui.setPrefs({ sidebarFocusColor: e.target.value })} />
            <button className="btn btn-ghost btn-xs" onClick={() => ui.setPrefs({ sidebarFocusColor: "" })}>重置</button>
          </div>
        </Row>
      </div>
    </div>
  );
}
