/** 设置中心：外观（v2.2 对齐 zcode 3.18 / S5 精简 plan-41-197）。
 * 主题模式、毛玻璃效果与强度、对话排版、左侧面板外观。
 *
 * S5（plan-41-197）按用户要求精简：
 *  - 毛玻璃开关不再提示「需重启生效」（开关即时生效，重启提示与真实行为不符）；
 *  - 移除「动画效果」配置（固定标准动画，动效统一由 --dur-* / --motion-scale 控制）；
 *  - 移除「面板拖拽排版 / 拖拽自动降级」（按要求固定"实时、不降级"）；
 *  - 移除左/右面板宽度滑杆（改为主界面直接拖拽分隔条，使用合理默认值）。 */
import { useEffect, useMemo, useState } from "react";
import { useThemeStore, WORLDS, type Theme, type World } from "../../store/theme";
import {
  SECTION_LABEL, TOOL_CARD_SECTIONS, hydrateToolCardPrefs, saveToolCardForm, saveToolCardSection,
  useToolCardStore, type ToolCardDefaultForm, type ToolCardSection,
} from "../../store/toolCard";
import { useUiStore, UI_FONT_OPTIONS } from "../../store/ui";
import { isFontInstalled, revalidateFonts } from "../../utils/fontProbe";
import { CardRow, Section, Segmented, Select, Slider, Switch, type SelectOption } from "../ui";
import { Dada } from "../mascot/Dada";

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
  git: "工具卡展开时默认展开 Git 栏，显示未提交改动、分支与提交入口",
  plan: "工具卡展开时默认展开计划栏，显示当前计划的步骤",
  process: "工具卡展开时默认展开进程栏，显示正在运行与待执行的进程",
  subagent: "工具卡展开时默认展开子代理栏，显示子代理列表",
  workflow: "工具卡展开时默认展开工作流栏，显示工作流进度",
  bgtask: "工具卡展开时默认展开后台任务栏，显示运行中的后台命令（含运行时间与停止按钮）",
};

/** 需要系统已安装才会生效的界面字体：选项置灰并标注「未安装」；内置与系统自带字体无需探测。
 *  plan-20261116-bundled-fonts：思源黑体（sourcehan）已随应用内置、HarmonyOS Sans（harmony）
 *  已从候选移除，两者都不再进探测表。 */
const UI_FONT_PROBE: Record<string, string> = {
  yahei: "Microsoft YaHei UI",
};
/** 随应用打包的界面字体（@font-face 见 styles/fonts.css）：判断可用性时不必探测本机 */
const BUNDLED_UI_FONTS = new Set(
  Object.entries(UI_FONT_OPTIONS).filter(([, f]) => f.bundled).map(([key]) => key),
);
const isUiFontInstalled = (key: string): boolean =>
  BUNDLED_UI_FONTS.has(key) || !UI_FONT_PROBE[key] || isFontInstalled(UI_FONT_PROBE[key]);

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
  // 每次进入设置页重新探测一次字体可用性：字体可能在使用期间装上 / 卸载，
  // 探测缓存（isFontInstalled）里只留正向结果，这里清掉并重渲染，避免「明明装了还显示未安装」。
  const [probeVersion, bumpProbe] = useState(0);
  useEffect(() => { revalidateFonts(); bumpProbe((n) => n + 1); }, []);
  // 选中时让该卡片里的嗒嗒眨一下眼；key 递增保证重复点击也会触发
  const [wink, setWink] = useState(0);
  // 内置字体（Inter / 思源黑体）恒可用，不提示「未安装」；「未安装」只针对候选表里存在、
  // 且本机探测确实没有的系统字体（历史配置里已移除的 harmony 之类不算，直接按默认字体展示）。
  const selectedUiFont = UI_FONT_OPTIONS[ui.uiFontFamily] ?? UI_FONT_OPTIONS.inter;
  /* plan-723-3029 R3：字体可用性探测要跑 canvas 测宽，且只缓存正向结果（未安装的系统字体每次都要重测）。
   * 选项表原先在 render 里现算，拖「对话字号」滑块时每变一个值就重测一轮；现在按
   * 「探测版本（revalidateFonts 后递增）+ 当前所选字体」缓存，字号/行距/宽度滑动不再触发探测。 */
  const uiFontOptions = useMemo<SelectOption[]>(
    () => Object.entries(UI_FONT_OPTIONS).map(([value, f]) => {
      // 内置字体恒可用：不置灰、不带「未安装」标注
      const ok = isUiFontInstalled(value);
      return { value, label: f.label, disabled: !ok, hint: ok ? undefined : "未安装" };
    }),
    // probeVersion 只在重新探测后变化；uiFontFamily 变化需要更新「未安装」提示
    [probeVersion, ui.uiFontFamily],
  );
  const selectedUiFontMissing = !!UI_FONT_OPTIONS[ui.uiFontFamily]
    && (uiFontOptions.find((o) => o.value === ui.uiFontFamily)?.disabled ?? false);

  return (
    <>
      <Section title="主题与毛玻璃">
        <CardRow title="主题模式" desc="切换界面的浅色或深色配色，立即生效">
          <Segmented<Theme>
            size="sm"
            value={theme}
            onChange={setTheme}
            aria-label="主题模式"
            options={(Object.keys(THEMES) as Theme[]).map((t) => ({ value: t, label: THEMES[t] }))}
          />
        </CardRow>
        <CardRow title="主题世界" desc="决定强调色、背景场景与嗒嗒的装扮；深色模式下底色保持中性" />
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
        <CardRow
          title="毛玻璃效果"
          desc={
            (ui.glassmorphism ? "左侧面板半透明磨砂，透出桌面壁纸" : "左侧面板使用实色底")
            + (ui.glassNeedRestart ? "（设置已保存，重启应用后生效）" : "")
          }
        >
          <Switch checked={ui.glassmorphism} onChange={(v) => ui.setPrefs({ glassmorphism: v })} />
        </CardRow>
        {ui.glassmorphism && (
          <CardRow
            title="玻璃强度"
            desc={`调节左侧面板透出桌面壁纸的程度（${GLASS_STRENGTHS.find((s) => s.value === (ui.glassStrength ?? 1))?.desc ?? ""}）`}
          >
            <Segmented<string>
              size="sm"
              value={String(ui.glassStrength ?? 1)}
              onChange={(v) => ui.setPrefs({ glassStrength: Number(v) })}
              aria-label="玻璃强度"
              options={GLASS_STRENGTHS.map((s) => ({ value: String(s.value), label: s.label }))}
            />
          </CardRow>
        )}
      </Section>

      <Section title="排版与字体">
        <CardRow
          title="界面字体"
          desc={
            selectedUiFont.desc
            + (selectedUiFontMissing ? "（所选字体未安装，当前已回退为默认字体）" : "")
          }
        >
          <Select
            value={UI_FONT_OPTIONS[ui.uiFontFamily] ? ui.uiFontFamily : "inter"}
            onChange={(v) => ui.setPrefs({ uiFontFamily: v })}
            options={uiFontOptions}
            aria-label="界面字体"
          />
        </CardRow>
        <CardRow title="对话字号" desc="对话消息的文字大小（立即生效）">
          <Slider min={11} max={18} step={1} value={ui.chatFontSize}
            onChange={(v) => ui.setPrefs({ chatFontSize: v })} format={(v) => `${v}px`} aria-label="对话字号" />
        </CardRow>
        <CardRow title="消息行距" desc="对话消息的行高倍数，数值越大行间越宽松（立即生效）">
          <Slider min={1.2} max={2.2} step={0.05} value={ui.chatLineHeight}
            onChange={(v) => ui.setPrefs({ chatLineHeight: v })} format={(v) => v.toFixed(2)} aria-label="消息行距" />
        </CardRow>
        <CardRow title="内容展示宽度" desc="消息与输入框的最大宽度；拖到最左（不限）时铺满整个内容区">
          <Slider min={0} max={1600} step={50} value={ui.contentMaxWidth}
            onChange={(v) => ui.setPrefs({ contentMaxWidth: v })}
            format={(v) => (v === 0 ? "不限" : `${v}px`)} aria-label="内容展示宽度" />
        </CardRow>
        <CardRow
          title="代码块自动换行"
          desc="开启后消息流与文档预览中的代码块、文件预览长行自动折行；也可直接在卡片右上角点击图标切换"
        >
          <Switch checked={ui.codeWordWrap} onChange={(v) => ui.setPrefs({ codeWordWrap: v })} />
        </CardRow>
      </Section>

      <Section title="会话工具卡">
        <CardRow title="默认形态" desc="新打开的会话中，右上角工具卡先显示为胶囊还是直接展开（会话内手动切换仍会记住）">
          <Segmented<ToolCardDefaultForm>
            size="sm"
            value={toolCardForm}
            onChange={(v) => saveToolCardForm(v)}
            aria-label="默认形态"
            options={(Object.keys(TOOL_CARD_FORMS) as ToolCardDefaultForm[]).map((f) => ({
              value: f,
              label: TOOL_CARD_FORMS[f],
            }))}
          />
        </CardRow>
        {TOOL_CARD_SECTIONS.map((key) => (
          <CardRow key={key} title={`${SECTION_LABEL[key]}栏默认展开`} desc={TOOL_CARD_SECTION_DESC[key]}>
            <Switch checked={toolCardSections[key]} onChange={(v) => saveToolCardSection(key, v)} />
          </CardRow>
        ))}
      </Section>

      <Section title="左侧面板外观">
        <CardRow title="文字大小" desc="左侧面板会话与导航文字大小">
          <Slider min={11} max={18} step={1} value={ui.sidebarFontSize}
            onChange={(v) => ui.setPrefs({ sidebarFontSize: v })} format={(v) => `${v}px`} aria-label="侧栏文字大小" />
        </CardRow>
        <CardRow title="图标大小" desc="左侧面板图标尺寸">
          <Slider min={12} max={22} step={1} value={ui.sidebarIconSize}
            onChange={(v) => ui.setPrefs({ sidebarIconSize: v })} format={(v) => `${v}px`} aria-label="侧栏图标大小" />
        </CardRow>
      </Section>
    </>
  );
}
