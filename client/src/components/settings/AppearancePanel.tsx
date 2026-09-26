/** 设置中心：外观（v2.2 对齐 zcode 3.18 / S5 精简 plan-41-197）。
 * 主题模式、毛玻璃效果与强度、对话排版、左侧面板外观。
 *
 * S5（plan-41-197）按用户要求精简：
 *  - 毛玻璃开关不再提示「需重启生效」（开关即时生效，重启提示与真实行为不符）；
 *  - 移除「动画效果」配置（固定标准动画，动效统一由 --dur-* / --motion-scale 控制）；
 *  - 移除「面板拖拽排版 / 拖拽自动降级」（按要求固定"实时、不降级"）；
 *  - 移除左/右面板宽度滑杆（改为主界面直接拖拽分隔条，使用合理默认值）。 */
import { useThemeStore, type Theme } from "../../store/theme";
import { useUiStore } from "../../store/ui";
import { Slider } from "../ui";
import { Row, Sw } from "./shared";

const THEMES: Record<Theme, string> = { light: "浅色", dark: "深色" };

/** S5：玻璃强度三档（0 轻柔 / 1 标准 / 2 深邃）——不透明度由 tokens.css 的
 *  --frost-left-alpha 声明式驱动（深邃透出最多、轻柔最实）。 */
const GLASS_STRENGTHS: Array<{ value: number; label: string; desc: string }> = [
  { value: 0, label: "轻柔", desc: "透出最少、文字最清晰" },
  { value: 1, label: "标准", desc: "默认：可读性与透出感平衡" },
  { value: 2, label: "深邃", desc: "透出桌面最明显（深色壁纸下更通透）" },
];

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
        <Row
          title="毛玻璃效果"
          desc={ui.glassmorphism ? "左侧面板半透明磨砂，轻微透出桌面壁纸" : "启用左侧面板与启动页的半透明磨砂背景"}
        >
          <Sw checked={ui.glassmorphism} onChange={(v) => ui.setPrefs({ glassmorphism: v })} />
        </Row>
        {ui.glassmorphism && (
          <Row title="玻璃强度" desc="左侧面板的不透明度。深色壁纸下若看不出透出，请选「深邃」">
            <div style={{ display: "flex", gap: 6 }}>
              {GLASS_STRENGTHS.map((s) => (
                <button
                  key={s.value}
                  className={"settings-pill" + ((ui.glassStrength ?? 1) === s.value ? " active" : "")}
                  title={s.desc}
                  onClick={() => ui.setPrefs({ glassStrength: s.value })}
                >
                  {s.label}
                </button>
              ))}
            </div>
          </Row>
        )}
      </div>

      <div className="settings-card">
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
          <Slider min={11} max={18} step={1} value={ui.sidebarFontSize}
            onChange={(v) => ui.setPrefs({ sidebarFontSize: v })} format={(v) => `${v}px`} aria-label="侧栏文字大小" />
        </Row>
        <Row title="图标大小" desc="左侧面板图标尺寸">
          <Slider min={12} max={22} step={1} value={ui.sidebarIconSize}
            onChange={(v) => ui.setPrefs({ sidebarIconSize: v })} format={(v) => `${v}px`} aria-label="侧栏图标大小" />
        </Row>
      </div>
    </div>
  );
}
