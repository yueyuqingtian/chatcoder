/** 拓展页「个性化图标」（plan-20261009-ui U9 重绘）。
 *
 * 上一版是四张随意散落的小卡片（盾形 / 圆环 / 六边形 + 米粒大的图案），形状互不相干、层次含糊，观感「碎」。
 * 这一版换成**每个品类一幅完整的小插画**，讲一个与品类含义直接相关的小故事：
 *   插件   —— 四块拼图拼成一个方块，最后一块（插件本身）「咔」地落进缺口；
 *   技能   —— 三张技能卡扇形展开，居中那张是一道闪电，底部有「启用」开关；
 *   连接器 —— 插头顺着线缆滑进插座，指示灯点亮、迸出一点电火花。
 * 统一的画法：纯色分面 + 一层挤出的厚度（深色底边）+ 左上高光，轮廓圆润；饱和色只用在主体上，
 * 卡片 / 面板类的中性部分取主题变量，所以深浅主题都立得住。
 * 入场与悬停动画见 styles/market.css 的 .mkt-art 系列（只动 transform / rotate / opacity，受「动画效果」档位约束）。
 *
 * 标题旁的小标（KindMark）同源：渐变圆角方块 + 白色字形（拼图块 / 闪电 / 插头）。 */
import { useId } from "react";
import type { MarketKind } from "../../api/client";

/* ── 调色 ─────────────────────────────────────────────────── */
type Hue = "amber" | "violet" | "teal" | "sky" | "green";
/** top 高光面 / base 主色 / deep 暗面 / edge 挤出的底边 */
const HUES: Record<Hue, { top: string; base: string; deep: string; edge: string }> = {
  amber: { top: "#FFD66B", base: "#FFB627", deep: "#EE9308", edge: "#B96A06" },
  violet: { top: "#BFA8FF", base: "#8E6FF7", deep: "#6C49E4", edge: "#4B31B3" },
  teal: { top: "#6BE8D5", base: "#26C8B2", deep: "#12A092", edge: "#0B746A" },
  sky: { top: "#86D0FF", base: "#3DA9F6", deep: "#2084DC", edge: "#165FA6" },
  green: { top: "#86E8A2", base: "#3CC96E", deep: "#22A458", edge: "#157C3F" },
};

/* ── 拼图块轮廓 ───────────────────────────────────────────── */
/** 边的类型：0 平 / 1 凸（向外鼓） / -1 凹（向内凹） */
type Edge = 0 | 1 | -1;
interface JigsawEdges { t: Edge; r: Edge; b: Edge; l: Edge }

/** 拼图块外轮廓路径（顺时针，原点在块左上角，边长 S，圆角 rr）。
 *  凸起是「蘑菇头」（头比脖子宽）；凹槽与相邻块的凸起**同心、半径大一圈缝隙 gap**，
 *  拼在一起时缝隙处处等宽，而不是一边紧贴一边留空。 */
export function jigsawPath(S: number, rr: number, e: JigsawEdges, knob: { R: number; k: number } = { R: 6.2, k: 3.7 }, gap = 2.4): string {
  const { R, k } = knob;
  const h = Math.sqrt(R * R - k * k); // 凸起圆心到边的距离
  const R2 = R + gap;
  const h2 = h - gap;
  const k2 = Math.sqrt(R2 * R2 - h2 * h2); // 凹槽口半宽
  const corner: Array<[number, number]> = [[0, 0], [S, 0], [S, S], [0, S]];
  const types = [e.t, e.r, e.b, e.l];
  const f = (n: number) => +n.toFixed(2);
  let d = "";
  for (let i = 0; i < 4; i++) {
    const [x0, y0] = corner[i];
    const [x1, y1] = corner[(i + 1) % 4];
    const dx = Math.sign(x1 - x0);
    const dy = Math.sign(y1 - y0);
    const sx = x0 + dx * rr;
    const sy = y0 + dy * rr;
    const ex = x1 - dx * rr;
    const ey = y1 - dy * rr;
    if (i === 0) d += `M${f(sx)} ${f(sy)}`;
    const mx = (x0 + x1) / 2;
    const my = (y0 + y1) / 2;
    const t = types[i];
    if (t === 0) {
      d += `L${f(ex)} ${f(ey)}`;
    } else {
      const kk = t === 1 ? k : k2;
      const rad = t === 1 ? R : R2;
      d += `L${f(mx - dx * kk)} ${f(my - dy * kk)}A${f(rad)} ${f(rad)} 0 1 ${t === 1 ? 1 : 0} ${f(mx + dx * kk)} ${f(my + dy * kk)}L${f(ex)} ${f(ey)}`;
    }
    const [x2, y2] = corner[(i + 2) % 4];
    const ndx = Math.sign(x2 - x1);
    const ndy = Math.sign(y2 - y1);
    d += `A${rr} ${rr} 0 0 1 ${f(x1 + ndx * rr)} ${f(y1 + ndy * rr)}`;
  }
  return d + "Z";
}

/** 四角星芒（凹边四尖星） */
const SPARK = "M0-7C1-2 2-1 7 0 2 1 1 2 0 7-1 2-2 1-7 0-2-1-1-2 0-7Z";

/* ── 标题旁的小标 ─────────────────────────────────────────── */
const MARK_HUE: Record<MarketKind, Hue> = { plugin: "amber", skill: "green", connector: "teal" };

/** 24×24 字形（白色；描边类用 currentColor 以便统一） */
function Glyph({ kind }: { kind: MarketKind }) {
  if (kind === "plugin") {
    // 一块拼图：上、右各一个凸起，左侧一个凹口
    const d = jigsawPath(12.4, 3, { t: 1, r: 1, b: 0, l: -1 }, { R: 2.7, k: 1.65 }, 0);
    return <path d={d} transform="translate(3.6 7.6)" fill="currentColor" />;
  }
  if (kind === "skill") {
    return <path d="M13.4 1.8 4.6 13.2h5.8L8.9 22.2 19.4 10h-5.9l-.1-8.2Z" fill="currentColor" strokeLinejoin="round" stroke="currentColor" strokeWidth="1.1" />;
  }
  return (
    <>
      <path d="M8.2 2.6v5M15.8 2.6v5" stroke="currentColor" strokeWidth="2.3" strokeLinecap="round" />
      <path d="M5.4 7.6h13.2v3a6.6 6.6 0 0 1-13.2 0v-3Z" fill="currentColor" />
      <path d="M12 17.4v4.2" stroke="currentColor" strokeWidth="2.3" strokeLinecap="round" />
    </>
  );
}

/** 标题旁的小标：渐变圆角方块 + 白色字形；悬停整体轻轻转一下（动画见 market.css） */
export function KindMark({ kind, size = 32 }: { kind: MarketKind; size?: number }) {
  const hue = HUES[MARK_HUE[kind]];
  return (
    <span
      className="mkt-mark"
      data-kind={kind}
      style={{
        width: size,
        height: size,
        ["--m-top" as string]: hue.top,
        ["--m-base" as string]: hue.base,
        ["--m-deep" as string]: hue.deep,
      }}
      aria-hidden
    >
      <svg className="mkt-mark-glyph" viewBox="0 0 24 24" width={Math.round(size * 0.66)} height={Math.round(size * 0.66)}>
        <Glyph kind={kind} />
      </svg>
    </span>
  );
}

/* ── 插件：四块拼图，最后一块落下 ───────────────────────────── */
const PIECE = 54;
const GAP = 2.6;
/** 凸起：蘑菇头（头宽 14、脖子宽 8.4），比默认值大一档，缩小到标题小标以外的尺寸时仍一眼认得出拼图 */
const KNOB = { R: 7, k: 4.2 };
const PIECES: Array<{ hue: Hue; x: number; y: number; e: JigsawEdges; cls?: string }> = (() => {
  const x0 = 136 - (PIECE + GAP / 2);
  const y0 = 10;
  const step = PIECE + GAP;
  return [
    { hue: "violet", x: x0, y: y0, e: { t: 0, r: 1, b: -1, l: 0 } },
    { hue: "teal", x: x0 + step, y: y0, e: { t: 0, r: 0, b: 1, l: -1 } },
    { hue: "sky", x: x0, y: y0 + step, e: { t: 1, r: -1, b: 0, l: 0 } },
    { hue: "amber", x: x0 + step, y: y0 + step, e: { t: -1, r: 0, b: 0, l: 1 }, cls: "mkt-snap" },
  ];
})();

function PluginScene() {
  const uid = useId().replace(/[^a-zA-Z0-9_-]/g, "");
  return (
    <svg className="mkt-art-svg" viewBox="0 0 272 144" aria-hidden>
      <defs>
        {(Object.keys(HUES) as Hue[]).map((h) => (
          <linearGradient key={h} id={`g-${uid}-${h}`} x1="0" y1="0" x2="1" y2="1">
            <stop offset="0" stopColor={HUES[h].top} />
            <stop offset="1" stopColor={HUES[h].deep} />
          </linearGradient>
        ))}
        {PIECES.map((p, i) => (
          <clipPath key={i} id={`c-${uid}-${i}`}><path d={jigsawPath(PIECE, 10, p.e, KNOB, GAP)} /></clipPath>
        ))}
      </defs>
      <ellipse cx="136" cy="131" rx="64" ry="6.5" fill="#000" opacity="0.1" />
      {PIECES.map((p, i) => {
        const d = jigsawPath(PIECE, 10, p.e, KNOB, GAP);
        return (
          <g key={i} transform={`translate(${p.x} ${p.y})`}>
            <g className={p.cls}>
            <path d={d} transform="translate(0 4.5)" fill={HUES[p.hue].edge} />
            <path d={d} fill={`url(#g-${uid}-${p.hue})`} />
            <g clipPath={`url(#c-${uid}-${i})`}>
              <path d={`M0 0H${PIECE}L0 ${PIECE}Z`} fill="#fff" opacity="0.2" />
              <path d={`M0 ${PIECE - 7}H${PIECE}V${PIECE}H0Z`} fill="#000" opacity="0.05" />
            </g>
            <path d={d} fill="none" stroke="#fff" strokeOpacity="0.35" strokeWidth="1" />
            </g>
          </g>
        );
      })}
      <g transform="translate(222 26)"><path className="mkt-twinkle mkt-tw-a" d={SPARK} fill="#FFB627" /></g>
      <g transform="translate(52 40) scale(0.8)"><path className="mkt-twinkle mkt-tw-b" d={SPARK} fill="#8E6FF7" /></g>
      <g transform="translate(228 100) scale(0.62)"><path className="mkt-twinkle mkt-tw-c" d={SPARK} fill="#26C8B2" /></g>
      <circle className="mkt-dot" cx="48" cy="96" r="2.4" fill="#3DA9F6" />
      <circle className="mkt-dot mkt-dot-b" cx="238" cy="62" r="2" fill="#8E6FF7" />
    </svg>
  );
}

/* ── 技能：三张技能卡扇形展开 ───────────────────────────────── */
const PIVOT = "136px 136px";

/** 一张技能卡（中性卡面取主题变量，彩色只在图标章与开关上）。
 *  side：l / r 是被压在后面的两张——图标章放在朝外的那个角，扇形展开后能露出来；front 是居中的主卡。 */
function SkillCard({ hue, icon, side }: { hue: Hue; icon: "book" | "bulb" | "bolt"; side: "l" | "r" | "front" }) {
  const c = HUES[hue];
  const x = 97;
  const y = 28;
  const w = 78;
  const h = 100;
  const front = side === "front";
  // 图标章中心 / 半径
  const r = front ? 17 : 12.5;
  const cx = front ? x + w / 2 : side === "l" ? x + 21 : x + w - 21;
  const cy = y + (front ? 31 : 21);
  // 文字线贴近朝外一侧
  const lx = side === "r" ? x + w - 14 : x + 14;
  const anchorEnd = side === "r";
  const bar = (wd: number) => (anchorEnd ? lx - wd : lx);
  return (
    <g>
      <rect x={x} y={y} width={w} height={h} rx="14" className="mkt-card-face" />
      <circle cx={cx} cy={cy} r={r} fill={c.base} />
      <circle cx={cx} cy={cy} r={r} fill="none" stroke="#fff" strokeOpacity="0.35" strokeWidth="1" />
      {icon === "bolt" && <path d={`M${cx + 2.4} ${cy - 11.5}l-9 12.6h5.8l-1.5 8.6 9.4-12.9h-5.8l1.1-8.3Z`} fill="#fff" />}
      {icon === "book" && <path d={`M${cx - 6.4} ${cy - 5.2}c2.1-1 4.1-1 6.4.4 2.3-1.4 4.3-1.4 6.4-.4v10.2c-2.1-1-4.1-1-6.4.4-2.3-1.4-4.3-1.4-6.4-.4z`} fill="#fff" />}
      {icon === "bulb" && (
        <>
          <path d={`M${cx} ${cy - 7.2}a5.2 5.2 0 0 1 3 9.5c-.4.3-.7.8-.7 1.3v.9h-4.6v-.9c0-.5-.3-1-.7-1.3a5.2 5.2 0 0 1 3-9.5Z`} fill="#fff" />
          <path d={`M${cx - 2} ${cy + 6.4}h4`} stroke="#fff" strokeWidth="1.6" strokeLinecap="round" />
        </>
      )}
      {/* 文字线：标题 + 说明 */}
      <rect x={bar(front ? 50 : 40)} y={y + (front ? 58 : 44)} width={front ? 50 : 40} height="6" rx="3" className="mkt-card-line" />
      <rect x={bar(front ? 38 : 30)} y={y + (front ? 70 : 56)} width={front ? 38 : 30} height="5" rx="2.5" className="mkt-card-line is-soft" />
      {front ? (
        <>
          {/* 启用开关：技能可以随时启停 */}
          <rect x={x + w / 2 - 14} y={y + 80} width="28" height="14" rx="7" fill={c.base} />
          <circle cx={x + w / 2 + 7} cy={y + 87} r="5.2" fill="#fff" />
        </>
      ) : (
        <rect x={bar(24)} y={y + 68} width="24" height="5" rx="2.5" className="mkt-card-line is-soft" />
      )}
    </g>
  );
}

function SkillScene() {
  return (
    <svg className="mkt-art-svg" viewBox="0 0 272 144" aria-hidden>
      <ellipse cx="136" cy="134" rx="62" ry="5.5" fill="#000" opacity="0.1" />
      <g className="mkt-fan mkt-fan-l" style={{ ["--a" as string]: "-22deg", transformOrigin: PIVOT, transformBox: "view-box" }}><SkillCard hue="amber" icon="book" side="l" /></g>
      <g className="mkt-fan mkt-fan-r" style={{ ["--a" as string]: "22deg", transformOrigin: PIVOT, transformBox: "view-box" }}><SkillCard hue="sky" icon="bulb" side="r" /></g>
      <g className="mkt-fan-front"><SkillCard hue="green" icon="bolt" side="front" /></g>
      <g transform="translate(224 28)"><path className="mkt-twinkle mkt-tw-a" d={SPARK} fill="#3CC96E" /></g>
      <g transform="translate(48 30) scale(0.8)"><path className="mkt-twinkle mkt-tw-b" d={SPARK} fill="#FFB627" /></g>
      <g transform="translate(236 100) scale(0.6)"><path className="mkt-twinkle mkt-tw-c" d={SPARK} fill="#3DA9F6" /></g>
      <circle className="mkt-dot" cx="40" cy="100" r="2.4" fill="#3CC96E" />
      <circle className="mkt-dot mkt-dot-b" cx="246" cy="66" r="2" fill="#FFB627" />
    </svg>
  );
}

/* ── 连接器：插头滑进插座 ───────────────────────────────────── */
function ConnectorScene() {
  const uid = useId().replace(/[^a-zA-Z0-9_-]/g, "");
  return (
    <svg className="mkt-art-svg" viewBox="0 0 272 144" aria-hidden>
      <defs>
        <linearGradient id={`b-${uid}`} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor={HUES.teal.top} />
          <stop offset="1" stopColor={HUES.teal.deep} />
        </linearGradient>
        <linearGradient id={`m-${uid}`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#F4F6F9" />
          <stop offset="1" stopColor="#B9C3CF" />
        </linearGradient>
      </defs>
      <ellipse cx="136" cy="124" rx="78" ry="6" fill="#000" opacity="0.1" />
      {/* 右：线缆 + 插座 */}
      <path d="M232 76c18 0 20-22 40-22" fill="none" stroke={HUES.teal.edge} strokeWidth="9" strokeLinecap="round" />
      <path d="M232 76c18 0 20-22 40-22" fill="none" stroke="#fff" strokeOpacity="0.28" strokeWidth="2.4" strokeLinecap="round" transform="translate(0 -2)" />
      <g>
        <rect x="150" y="44" width="86" height="68" rx="17" fill={HUES.teal.edge} />
        <rect x="150" y="40" width="86" height="68" rx="17" className="mkt-socket-face" />
        <rect x="157" y="47" width="72" height="54" rx="11" className="mkt-socket-inset" />
        {/* 插孔口：深色凹槽，插脚正好伸进去 */}
        <rect x="150" y="56" width="14" height="12" rx="4" fill="#2C3947" />
        <rect x="150" y="80" width="14" height="12" rx="4" fill="#2C3947" />
        <circle className="mkt-led" cx="212" cy="74" r="4.6" />
        <circle cx="172" cy="52" r="1.8" className="mkt-screw" />
        <circle cx="224" cy="96" r="1.8" className="mkt-screw" />
      </g>
      {/* 左：线缆 + 插头（整体滑入） */}
      <g className="mkt-plug">
        <path d="M84 74c-22 0-24 24-46 24S18 80 0 80" fill="none" stroke={HUES.teal.edge} strokeWidth="9" strokeLinecap="round" />
        <path d="M84 74c-22 0-24 24-46 24S18 80 0 80" fill="none" stroke="#fff" strokeOpacity="0.28" strokeWidth="2.4" strokeLinecap="round" transform="translate(0 -2)" />
        {/* 插脚：金属色，末端圆头，刚好伸进插孔口 */}
        <rect x="138" y="58" width="22" height="8" rx="4" fill={`url(#m-${uid})`} />
        <rect x="138" y="82" width="22" height="8" rx="4" fill={`url(#m-${uid})`} />
        {/* 插头本体 */}
        <rect x="80" y="54" width="62" height="44" rx="14" fill={HUES.teal.edge} />
        <rect x="80" y="50" width="62" height="44" rx="14" fill={`url(#b-${uid})`} />
        <path d="M86 62a8 8 0 0 1 8-8h22" fill="none" stroke="#fff" strokeOpacity="0.5" strokeWidth="2.4" strokeLinecap="round" />
        <rect x="94" y="66" width="30" height="5" rx="2.5" fill="#fff" opacity="0.28" />
        <rect x="94" y="76" width="20" height="5" rx="2.5" fill="#fff" opacity="0.18" />
      </g>
      {/* 接通时迸出的电火花 */}
      <g transform="translate(147 30)">
        <path className="mkt-zap" d="M2.4-15 -6 -1.5h5.2L-2.4 11 8.2-4.6H2.6l2.4-10.4Z" fill="#FFB627" stroke="#E48A0B" strokeWidth="0.8" strokeLinejoin="round" />
      </g>
      <g transform="translate(110 22) scale(0.7)"><path className="mkt-twinkle mkt-tw-a" d={SPARK} fill="#26C8B2" /></g>
      <g transform="translate(252 106) scale(0.7)"><path className="mkt-twinkle mkt-tw-b" d={SPARK} fill="#3DA9F6" /></g>
      <circle className="mkt-dot" cx="40" cy="40" r="2.4" fill="#26C8B2" />
      <circle className="mkt-dot mkt-dot-b" cx="200" cy="126" r="2" fill="#FFB627" />
    </svg>
  );
}

/** 英雄区右侧插画：三类各一幅；key=kind 保证切标签时重挂载以重播入场动画 */
export function MarketDecor({ kind }: { kind: MarketKind }) {
  return (
    <div className="mkt-art" data-kind={kind} key={kind} aria-hidden>
      {kind === "plugin" ? <PluginScene /> : kind === "skill" ? <SkillScene /> : <ConnectorScene />}
    </div>
  );
}
