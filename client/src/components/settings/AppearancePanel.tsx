/** 设置中心：外观（v2.2 对齐 zcode 3.18）。
 * 主题模式、毛玻璃效果、布局宽度、字号、左侧面板外观。
 *
 * plan-26-116：只保留**毛玻璃**一种玻璃模式——液态玻璃、液态玻璃（折射版）、
 * 系统模糊后端提示、玻璃诊断/自检等全部移除，设置页不再出现任何后端/诊断文案。 */
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

/** plan-26-116 M4：玻璃强度三档（0 轻柔 / 1 标准 / 2 深邃）——各层不透明度由 ui.ts 计算 */
const GLASS_STRENGTHS: Array<{ value: number; label: string; desc: string }> = [
  { value: 0, label: "轻柔", desc: "面板更接近实色，文字最清晰" },
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
        {/* plan-26-126 M4：强度三档统一控制左侧面板的不透明度（低透明：0.94/0.90/0.86） */}
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
        {/* plan-26-126 P1：毛玻璃**重启后生效**——开启/切换后提示用户重启（带一键重启）。
            为何改为重启生效：运行期切换会让 DWM 合成与渲染层缓存失步
            （左侧面板色块 / 设置页残影，只有重启才恢复），且在高频偏好写入下卡顿。 */}
        {ui.glassmorphism && ui.glassNeedRestart && (
          <Row title="重启后生效" desc="毛玻璃需要重启应用才能完全生效（设置已保存）">
            <button
              className="btn btn-primary btn-sm"
              onClick={() => { void window.chatcoderAPI?.relaunchApp?.(); }}
            >
              立即重启
            </button>
          </Row>
        )}
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
