/** 吉祥物「嗒嗒」v5（plan-114-450）。
 * 用户在第四轮预览（ai/dada-v5-preview.html）选定：白色圆团子 + 头顶两撮呆毛 + 一双小脚，
 * 黑椭圆大眼 + 淡腮红，没有描边、没有嘴——体积只靠「受光渐变 + 柔和投影」表达。
 * 所有出场位置只引用本组件，不得复制 SVG。
 *
 * 三个版本，同一骨架：
 *  - rich   完整版：受光渐变、投影、呆毛、小脚、腮红；关于页 / 乐园 / 主题卡 / 首页邮票
 *  - icon   简化版：去掉小脚与投影，小尺寸（≤32px）也干净；头像 / 菜单中心
 *  - sketch 柔和单色版：身体是 currentColor 的淡底，眼睛 currentColor；空状态 / Loader 等界面点缀，深浅色自动适配
 * 旧值 color/line 分别映射到 rich/sketch；v5 不戴配件，已移除 gear 参数。
 * 唯一循环动画是眨眼（mascot.css），reduced-motion 下关闭。 */
import { useId, type CSSProperties } from "react";

export type DadaMood = "calm" | "happy" | "think" | "oops" | "wow";
export type DadaVariant = "rich" | "icon" | "sketch" | "color" | "line";

export interface DadaProps {
  mood?: DadaMood;
  variant?: DadaVariant;
  size?: number;
  /** 眼睛看向的方向（-1~1），用于环形菜单等「看向」交互 */
  look?: { x: number; y: number };
  blink?: boolean;
  className?: string;
  style?: CSSProperties;
  title?: string;
}

/** 120 网格：身体上窄下宽，宽 92 高 90；呆毛偏左两撮、短而圆，打破对称 */
export const DADA_BODY = "M60 20 C87 20 105 40 106 66 C107 93 88 110 60 110 C32 110 13 93 14 66 C15 40 33 20 60 20 Z";
export const DADA_TUFT = "M52 24 C49 17 51 11 56 11 C59 14 58 20 56 24 Z M60 23 C61 15 66 11 71 13 C71 18 66 22 62 25 Z";
export const DADA_EYE = "#1B1A24";
const BLUSH = "#FF8FA8";
/** 身体受光三档：亮面纯白 → 中间调 → 背光面冷灰蓝（只比底色低一点，保持干净） */
const LIT = "#FFFFFF", MID = "#F3F5FB", SHADE = "#D9DFEF";

function normVariant(v: DadaVariant): "rich" | "icon" | "sketch" {
  if (v === "color") return "rich";
  if (v === "line") return "sketch";
  return v;
}

/** 眼睛：左右眼中心 (48,62)/(72,62)；眼高约为身宽的 1/5，眼距收窄让脸更集中 */
function Eyes({ mood, look, ink }: { mood: DadaMood; look: { x: number; y: number }; ink: string }) {
  const dx = look.x * 4, dy = look.y * 3.2;
  const L = 48 + dx, R = 72 + dx, Y = 62 + dy;
  const arc = { fill: "none", stroke: ink, strokeWidth: 5, strokeLinecap: "round" as const, strokeLinejoin: "round" as const };
  switch (mood) {
    case "happy":
      return (
        <g {...arc}>
          <path d={`M${L - 7} ${Y + 3} Q${L} ${Y - 7} ${L + 7} ${Y + 3}`} />
          <path d={`M${R - 7} ${Y + 3} Q${R} ${Y - 7} ${R + 7} ${Y + 3}`} />
        </g>
      );
    case "oops":
      return (
        <g {...arc}>
          <path d={`M${L - 6} ${Y - 7} L${L + 5} ${Y} L${L - 6} ${Y + 7}`} />
          <path d={`M${R + 6} ${Y - 7} L${R - 5} ${Y} L${R + 6} ${Y + 7}`} />
        </g>
      );
    case "think":
      // 思考：眼睛略小、望向右上方
      return (
        <g className="dada-eyes" fill={ink}>
          <ellipse cx={L + 3} cy={Y - 3} rx={6.2} ry={9.2} />
          <ellipse cx={R + 3} cy={Y - 3} rx={6.2} ry={9.2} />
        </g>
      );
    case "wow":
      return (
        <g className="dada-eyes" fill={ink}>
          <ellipse cx={L} cy={Y} rx={8.6} ry={12.6} />
          <ellipse cx={R} cy={Y} rx={8.6} ry={12.6} />
        </g>
      );
    default:
      return (
        <g className="dada-eyes" fill={ink}>
          <ellipse cx={L} cy={Y} rx={7} ry={10.4} />
          <ellipse cx={R} cy={Y} rx={7} ry={10.4} />
        </g>
      );
  }
}

export function Dada({
  mood = "calm", variant = "sketch", size = 32, look = { x: 0, y: 0 },
  blink = true, className, style, title,
}: DadaProps) {
  const uid = useId().replace(/:/g, "");
  const v = normVariant(variant);
  const sketch = v === "sketch";
  const rich = v === "rich";
  const fill = sketch ? "currentColor" : `url(#dg${uid})`;
  const ink = sketch ? "currentColor" : DADA_EYE;
  // 柔和单色版：身体只铺一层很淡的 currentColor，眼睛用实色，深浅主题自动适配
  const bodyOpacity = sketch ? 0.13 : 1;

  return (
    <svg
      viewBox="0 0 120 120" width={size} height={size}
      className={"dada dada-" + v + (blink ? " dada-blink" : "") + (className ? ` ${className}` : "")}
      style={style}
      role={title ? "img" : undefined} aria-label={title} aria-hidden={title ? undefined : true}
    >
      {!sketch && (
        <defs>
          <radialGradient id={`dg${uid}`} cx="40%" cy="32%" r="78%">
            <stop offset="0" stopColor={LIT} />
            <stop offset="0.62" stopColor={MID} />
            <stop offset="1" stopColor={SHADE} />
          </radialGradient>
          {rich && (
            <filter id={`ds${uid}`} x="-20%" y="-20%" width="140%" height="150%">
              <feDropShadow dx="0" dy="5" stdDeviation="5" floodColor="#3A4270" floodOpacity="0.16" />
            </filter>
          )}
        </defs>
      )}

      <g filter={rich ? `url(#ds${uid})` : undefined} opacity={bodyOpacity}>
        {/* 小脚在身体后面，只露出一点，读作「站着」；简化版省略 */}
        {v !== "icon" && (
          <g fill={sketch ? "currentColor" : SHADE}>
            <ellipse cx="44" cy="107" rx="11" ry="7" />
            <ellipse cx="76" cy="107" rx="11" ry="7" />
          </g>
        )}
        <path d={DADA_TUFT} fill={fill} />
        <path d={DADA_BODY} fill={fill} />
      </g>

      {!sketch && (
        <g fill={BLUSH} opacity={0.38}>
          <ellipse cx="35" cy="77" rx="7" ry="4" />
          <ellipse cx="85" cy="77" rx="7" ry="4" />
        </g>
      )}
      <Eyes mood={mood} look={look} ink={ink} />
    </svg>
  );
}
