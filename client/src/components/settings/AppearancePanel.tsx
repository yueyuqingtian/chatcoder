/** 设置中心：外观（v2.2 对齐 zcode 3.18）。
 * 主题模式、毛玻璃效果、布局宽度、字号、左侧面板外观。 */
import { useThemeStore, type Theme } from "../../store/theme";
import { useUiStore, type MotionLevel } from "../../store/ui";
import { Slider } from "../ui";
import { Row, Sw } from "./shared";

const THEMES: Record<Theme, string> = { light: "浅色", dark: "深色" };

/** plan-248-1258 M5: 动画效果三档（低配机可减弱以提升流畅度） */
const MOTION_LEVELS: Record<MotionLevel, { label: string; desc: string }> = {
  full: { label: "标准", desc: "完整动效" },
  reduced: { label: "减弱", desc: "缩短时长、禁用循环动画" },
  off: { label: "关闭", desc: "瞬时呈现" },
};

export function AppearancePanel() {
  const { theme, setTheme } = useThemeStore();
  const ui = useUiStore();
  return (
    <div className="settings-card-stack">
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
        <Row title="动画效果" desc="低配置设备可选择「减弱」或「关闭」以提升流畅度">
          <div style={{ display: "flex", gap: 6 }}>
            {(Object.keys(MOTION_LEVELS) as MotionLevel[]).map((m) => (
              <button
                key={m}
                className={"settings-pill" + ((ui.motionLevel || "full") === m ? " active" : "")}
                title={MOTION_LEVELS[m].desc}
                onClick={() => ui.setPrefs({ motionLevel: m })}
              >
                {MOTION_LEVELS[m].label}
              </button>
            ))}
          </div>
        </Row>
      </div>

      <div className="settings-card">
        <Row title="左侧面板宽度" desc="可在主界面直接拖拽分隔条调整">
          <Slider min={200} max={480} step={4} value={ui.leftPanelWidth}
            onChange={(v) => ui.setPrefs({ leftPanelWidth: v })} format={(v) => `${v}px`} aria-label="左侧面板宽度" />
        </Row>
        <Row title="右侧面板宽度" desc="可在主界面直接拖拽分隔条调整">
          <Slider min={200} max={1200} step={10} value={ui.rightPanelWidth}
            onChange={(v) => ui.setPrefs({ rightPanelWidth: v })} format={(v) => `${v}px`} aria-label="右侧面板宽度" />
        </Row>
        <Row title="对话字号" desc="控制对话消息的文字大小（立即生效）">
          <Slider min={11} max={18} step={1} value={ui.chatFontSize}
            onChange={(v) => ui.setPrefs({ chatFontSize: v })} format={(v) => `${v}px`} aria-label="对话字号" />
        </Row>
        <Row title="消息行距" desc="控制对话消息的行间间距倍率（立即生效）">
          <Slider min={1.2} max={2.2} step={0.05} value={ui.chatLineHeight}
            onChange={(v) => ui.setPrefs({ chatLineHeight: v })} format={(v) => v.toFixed(2)} aria-label="消息行距" />
        </Row>
        <Row title="内容展示宽度" desc="0 表示不限制，填满可视区域">
          <Slider min={0} max={1200} step={50} value={ui.contentMaxWidth}
            onChange={(v) => ui.setPrefs({ contentMaxWidth: v })}
            format={(v) => (v === 0 ? "不限" : `${v}px`)} aria-label="内容展示宽度" />
        </Row>
      </div>

      <div className="settings-card">
        <div className="settings-card-title">左侧面板外观</div>
        <Row title="文字大小" desc="左侧面板会话与导航文字大小">
          <Slider min={11} max={16} step={1} value={ui.sidebarFontSize}
            onChange={(v) => ui.setPrefs({ sidebarFontSize: v })} format={(v) => `${v}px`} aria-label="侧栏文字大小" />
        </Row>
        <Row title="图标大小" desc="左侧面板图标尺寸">
          <Slider min={12} max={20} step={1} value={ui.sidebarIconSize}
            onChange={(v) => ui.setPrefs({ sidebarIconSize: v })} format={(v) => `${v}px`} aria-label="侧栏图标大小" />
        </Row>
      </div>
    </div>
  );
}
