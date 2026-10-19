/** 设置中心：外观（v2.2 对齐 zcode 3.18 / S5 精简 plan-41-197）。
 * 主题模式、毛玻璃效果与强度、对话排版、左侧面板外观。
 *
 * S5（plan-41-197）按用户要求精简：
 *  - 毛玻璃开关不再提示「需重启生效」（开关即时生效，重启提示与真实行为不符）；
 *  - 移除「动画效果」配置（固定标准动画，动效统一由 --dur-* / --motion-scale 控制）；
 *  - 移除「面板拖拽排版 / 拖拽自动降级」（按要求固定"实时、不降级"）；
 *  - 移除左/右面板宽度滑杆（改为主界面直接拖拽分隔条，使用合理默认值）。 */
import { useEffect, useState } from "react";
import { useThemeStore, WORLDS, type Theme, type World } from "../../store/theme";
import {
  SECTION_LABEL, TOOL_CARD_SECTIONS, hydrateToolCardPrefs, saveToolCardForm, saveToolCardSection,
  useToolCardStore, type ToolCardDefaultForm, type ToolCardSection,
} from "../../store/toolCard";
import { useUiStore, UI_FONT_OPTIONS } from "../../store/ui";
import { Select, Slider } from "../ui";
import { Dada } from "../mascot/Dada";
import { Row, Sw } from "./shared";

const THEMES: Record<Theme, string> = { light: "浅色", dark: "深色" };

/** plan-114-447：主题世界——名称 + 一句情绪，卡片里是戴着该世界配件的嗒嗒 */
const WORLD_META: Record<World, { label: string; mood: string }> = {
  graphite: { label: "石墨", mood: "安静专注，一支铅笔就够" },
  forest: { label: "森林", mood: "沉静生长，偶尔落一片叶" },
  campfire: { label: "篝火", mood: "夜里写代码的那点暖" },
  breeze: { label: "煦风", mood: "轻快，发出去就像纸飞机" },
  tide: { label: "潮汐", mood: "冷静，把问题交给漂流瓶" },
  sakura: { label: "樱时", mood: "柔和，花瓣只落一点点" },
};
/** 卡片色标：取各世界强调色（与 themes.css 保持一致） */
const WORLD_DOT: Record<World, string> = {
  graphite: "oklch(30% 0.01 260)",
  forest: "oklch(46% 0.075 150)",
  campfire: "oklch(58% 0.13 45)",
  breeze: "oklch(56% 0.08 220)",
  tide: "oklch(46% 0.11 245)",
  sakura: "oklch(60% 0.07 0)",
};

const TOOL_CARD_FORMS: Record<ToolCardDefaultForm, string> = { capsule: "胶囊", expanded: "展开" };
const TOOL_CARD_SECTION_DESC: Record<ToolCardSection, string> = {
  git: "展开卡片时是否直接显示未提交行数、分支与提交入口",
  plan: "展开卡片时是否直接显示当前计划的步骤",
  process: "展开卡片时是否直接显示正在运行与待执行的进程",
  subagent: "展开卡片时是否直接显示子代理列表",
  workflow: "展开卡片时是否直接显示工作流进度",
};

/** S5：玻璃强度三档（0 轻柔 / 1 标准 / 2 深邃）——不透明度由 tokens.css 的
 *  --frost-left-alpha 声明式驱动（深邃透出最多、轻柔最实）。 */
const GLASS_STRENGTHS: Array<{ value: number; label: string; desc: string }> = [
  { value: 0, label: "轻柔", desc: "透出最少、文字最清晰" },
  { value: 1, label: "标准", desc: "默认：可读性与透出感平衡" },
  { value: 2, label: "深邃", desc: "透出桌面最明显（深色壁纸下更通透）" },
];

export function AppearancePanel() {
  const { theme, setTheme, world, setWorld } = useThemeStore();
  const ui = useUiStore();
  const toolCardForm = useToolCardStore((s) => s.defaultForm);
  const toolCardSections = useToolCardStore((s) => s.sectionDefaults);
  // 设置页可能先于工具卡挂载：补读一次后端全局默认（内部只读一次）
  useEffect(() => { hydrateToolCardPrefs(); }, []);
  // 选中时让该卡片里的嗒嗒眨一下眼；key 递增保证重复点击也会触发
  const [wink, setWink] = useState(0);

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
        <Row title="主题世界" desc="决定强调色、场景与嗒嗒的装扮；底色与侧栏始终保持中性" />
        <div className="world-grid" role="radiogroup" aria-label="主题世界">
          {WORLDS.map((w) => (
            <button
              key={w}
              type="button"
              role="radio"
              aria-checked={world === w}
              className={"world-card" + (world === w ? " active" : "")}
              onClick={() => { setWorld(w); setWink((n) => n + 1); }}
            >
              <span className="world-card-art">
                <Dada
                  key={world === w ? `on-${wink}` : "off"}
                  variant="rich"
                  size={44}
                  blink={false}
                  mood={world === w ? "happy" : "calm"}
                  className={world === w ? "dada-wink" : undefined}
                />
              </span>
              <span className="world-card-text">
                <span className="world-card-name">
                  <i className="world-card-dot" style={{ background: WORLD_DOT[w] }} />
                  {WORLD_META[w].label}
                </span>
                <span className="world-card-mood">{WORLD_META[w].mood}</span>
              </span>
            </button>
          ))}
        </div>
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
        <Row title="界面字体" desc={(UI_FONT_OPTIONS[ui.uiFontFamily] ?? UI_FONT_OPTIONS.inter).desc}>
          <Select
            value={UI_FONT_OPTIONS[ui.uiFontFamily] ? ui.uiFontFamily : "inter"}
            onChange={(v) => ui.setPrefs({ uiFontFamily: v })}
            options={Object.entries(UI_FONT_OPTIONS).map(([value, f]) => ({ value, label: f.label }))}
            aria-label="界面字体"
          />
        </Row>
        <Row title="对话字号" desc="控制对话消息的文字大小（立即生效）">
          <Slider min={11} max={18} step={1} value={ui.chatFontSize}
            onChange={(v) => ui.setPrefs({ chatFontSize: v })} format={(v) => `${v}px`} aria-label="对话字号" />
        </Row>
        <Row title="消息行距" desc="控制对话消息的行间间距倍率（立即生效）">
          <Slider min={1.2} max={2.2} step={0.05} value={ui.chatLineHeight}
            onChange={(v) => ui.setPrefs({ chatLineHeight: v })} format={(v) => v.toFixed(2)} aria-label="消息行距" />
        </Row>
        <Row title="内容展示宽度" desc="消息与输入区的最大宽度，0 表示铺满整个内容区">
          <Slider min={0} max={1600} step={50} value={ui.contentMaxWidth}
            onChange={(v) => ui.setPrefs({ contentMaxWidth: v })}
            format={(v) => (v === 0 ? "不限" : `${v}px`)} aria-label="内容展示宽度" />
        </Row>
      </div>

      <div className="settings-card">
        <div className="settings-card-title">会话工具卡</div>
        <Row title="默认形态" desc="新打开的会话中，右上角工具卡先显示为胶囊还是直接展开（会话内手动切换仍会记住）">
          <div style={{ display: "flex", gap: 6 }}>
            {(Object.keys(TOOL_CARD_FORMS) as ToolCardDefaultForm[]).map((f) => (
              <button
                key={f}
                type="button"
                className={"settings-pill" + (toolCardForm === f ? " active" : "")}
                onClick={() => saveToolCardForm(f)}
              >
                {TOOL_CARD_FORMS[f]}
              </button>
            ))}
          </div>
        </Row>
        {TOOL_CARD_SECTIONS.map((key) => (
          <Row key={key} title={`${SECTION_LABEL[key]}栏默认展开`} desc={TOOL_CARD_SECTION_DESC[key]}>
            <Sw checked={toolCardSections[key]} onChange={(v) => saveToolCardSection(key, v)} />
          </Row>
        ))}
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
