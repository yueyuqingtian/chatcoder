/** 市场页装饰（plan-59-286 视觉重做）：多色精绘图标 + 无边框纯色卡片组 + 铅笔标记线。
 *
 * 对照参考图（成熟产品）修正的四个关键差异：
 *  1. **卡片无边框**：参考图的卡片是**纯浅色填充块**，没有任何描边。
 *     上一版给每个形状都加了 1px stroke，视觉上"硬、廉价"，正是用户说的"还有边框"。
 *     现在只保留填充 + 由底色派生的极淡柔影（比边框更轻，但仍有"贴在页面上"的实体感）。
 *  2. **图标多色精绘**：参考图的图标是**彩色图标**（一个图形里有多档明暗），
 *     而上一版是单色实心块（菱形、方块），小尺寸下毫无层次，看着像色块而非图标。
 *     现在每个图案用三档色（主色 / 加深 / 提亮）分面绘制：立体面、凹槽、高光各司其职。
 *  3. **图标色在卡片底色上继续加深**：卡片底取主色 10% 的浅色，
 *     图标取主色**加深档**（`color-mix(base, black 20%)`）作为主体，
 *     形成"浅底包深图"的层次；高光档用主色提亮，用于凹槽与反光点。
 *  4. **手工定稿 path**：全部几何由圆角矩形、弧与有限控制点手写，
 *     每个图案 ≤ 6 个元素；不再用参数方程生成几何形状（那会产生"自动生成感"）。
 */
import type { ReactNode } from "react";
import type { MarketKind } from "../../api/client";

/** 图标三档色（一套色阶贯穿页面，保证同品类视觉自洽） */
export interface ArtColors {
  /** 主体色：图标的主要面（在卡片底上明显加深） */
  base: string;
  /** 加深档：凹槽、暗面、次要部件 */
  deep: string;
  /** 提亮档：高光、反光点 */
  light: string;
}

/** 品类色阶（标题名 / 标题图标 / 手绘线 / 卡组默认色共用） */
export const KIND_ACCENT: Record<MarketKind, {
  strong: string;
  soft: string;
  ink: string;
  deep: string;
  light: string;
  pencil: string;
}> = {
  plugin: {
    strong: "var(--color-amber-600)",
    soft: "color-mix(in oklab, var(--color-amber-600) 14%, var(--color-card))",
    ink: "color-mix(in oklab, var(--color-amber-600) 11%, var(--color-card))",
    deep: "color-mix(in oklab, var(--color-amber-600), black 18%)",
    light: "color-mix(in oklab, var(--color-amber-600) 40%, white)",
    pencil: "color-mix(in oklab, var(--color-amber-600) 55%, transparent)",
  },
  skill: {
    strong: "var(--color-green-600)",
    soft: "color-mix(in oklab, var(--color-green-600) 14%, var(--color-card))",
    ink: "color-mix(in oklab, var(--color-green-600) 11%, var(--color-card))",
    deep: "color-mix(in oklab, var(--color-green-600), black 18%)",
    light: "color-mix(in oklab, var(--color-green-600) 40%, white)",
    pencil: "color-mix(in oklab, var(--color-green-600) 55%, transparent)",
  },
  connector: {
    strong: "var(--color-teal-600)",
    soft: "color-mix(in oklab, var(--color-teal-600) 14%, var(--color-card))",
    ink: "color-mix(in oklab, var(--color-teal-600) 11%, var(--color-card))",
    deep: "color-mix(in oklab, var(--color-teal-600), black 18%)",
    light: "color-mix(in oklab, var(--color-teal-600) 40%, white)",
    pencil: "color-mix(in oklab, var(--color-teal-600) 55%, transparent)",
  },
};

/** 卡组允许的色系（每张卡自选一种，形成参考图那种"同一组里颜色各异"的丰富度） */
const PALETTE = {
  amber: "var(--color-amber-600)",
  green: "var(--color-green-600)",
  teal: "var(--color-teal-600)",
  violet: "var(--color-violet-600)",
  red: "var(--color-red-500)",
  sky: "var(--color-sky-600)",
} as const;
type Tone = keyof typeof PALETTE;

/** 色系 → 卡片底 / 图标三色 */
function toneOf(t: Tone): { ink: string; colors: ArtColors } {
  const base = PALETTE[t];
  return {
    ink: `color-mix(in oklab, ${base} 11%, var(--color-card))`,
    colors: {
      base: `color-mix(in oklab, ${base}, black 12%)`,
      deep: `color-mix(in oklab, ${base}, black 34%)`,
      light: `color-mix(in oklab, ${base} 42%, white)`,
    },
  };
}

/* ── 造型库（48 viewBox，手工定稿） ──
 * 四种轮廓差异明显的形状：圆角方 / 圆环 / 六边形 / 盾形。
 * 注意：这里只提供"填充轮廓"，描边一律不设（参考图卡片无边框）。 */
type DecoShape = "tile" | "ring" | "hex" | "shield";

const SHAPES: Record<DecoShape, ReactNode> = {
  /** 圆角方：最"正"的一张，作为成组里的主卡 */
  tile: <rect x="2" y="2" width="44" height="44" rx="14" />,
  /** 圆环：外圆挖内圆，轮廓最轻，单独放在一侧 */
  ring: <path d="M24 2.6a21.4 21.4 0 1 0 0 42.8 21.4 21.4 0 0 0 0-42.8Zm0 6.4a15 15 0 1 1 0 30 15 15 0 0 1 0-30Z" fillRule="evenodd" />,
  /** 六边形：内切半径 18.6，中心有足够面积安放图案 */
  hex: <path d="M24 2.5 42.6 13.3v21.4L24 45.5 5.4 34.7V13.3L24 2.5Z" />,
  /** 盾形：轮廓最有辨识度，压在组下沿 */
  shield: <path d="M24 2.8 44.4 12.6v17.8c0 8.4-8.6 13.8-20.4 15.6C12.2 44.2 3.6 38.8 3.6 30.4V12.6L24 2.8Z" />,
};

/* ── 图案库（24 viewBox，多色分面，手工定稿） ──
 * 每个图案用 base / deep / light 三档色绘制不同的面：
 * 主体用 base，凹槽与暗面用 deep，高光与镂空用 light 或卡片底色，
 * 在浅色卡片底上形成"立体小物件"的观感 —— 这是参考图图标显得精绘的关键。 */
const ART: Record<string, (c: ArtColors) => ReactNode> = {
  /** 拼图块：把能力接进来（主体 + 左上亮面 + 右侧凸起底色区分） */
  puzzle: (c) => (
    <>
      <path
        d="M5.6 8.9a1.9 1.9 0 0 1 1.9-1.9h2.9a2.25 2.25 0 1 1 4.5 0h2.9a1.9 1.9 0 0 1 1.9 1.9v3a2.25 2.25 0 1 1 0 4.5v2.7a1.9 1.9 0 0 1-1.9 1.9H7.5a1.9 1.9 0 0 1-1.9-1.9v-2.7a2.25 2.25 0 1 0 0-4.5v-3Z"
        fill={c.base}
      />
      {/* 左上亮面：简洁的圆角块，不做复杂裁切（复杂路径在小尺寸下会渲染出奇怪形状） */}
      <path d="M7.5 7h4.2v3.6H7.5a1.4 1.4 0 0 1 0-3.6Z" fill={c.light} opacity="0.6" />
    </>
  ),
  /** 立方体：模块化组装（三面立体，最见"精绘"功力） */
  cube: (c) => (
    <>
      <path d="M12 2.6 21 7.6l-9 5.1-9-5.1 9-5Z" fill={c.light} />
      <path d="M3 7.6l9 5.1v9.1l-9-5.2V7.6Z" fill={c.base} />
      <path d="M21 7.6v9L12 21.8v-9.1l9-5.1Z" fill={c.deep} />
    </>
  ),
  /** 层叠：多能力叠加 */
  layers: (c) => (
    <>
      <path d="M12 2.8 22 8.3l-10 5.5L2 8.3l10-5.5Z" fill={c.light} />
      <path d="M2 12.2 12 17.7l10-5.5v1.9L12 19.6 2 14.1v-1.9Z" fill={c.base} />
      <path d="M2 16.6 12 22l10-5.4v1.8L12 24 2 18.4v-1.8Z" fill={c.deep} />
    </>
  ),
  /** 闪电：一步到位 */
  bolt: (c) => (
    <>
      <path d="M14.2 2.4 4.6 13.9h5.6l-1.4 8.1 9.6-11.5h-5.6l1.4-8.1Z" fill={c.base} />
      <path d="M14.2 2.4 9.4 8.2h3.2l-.9 5.6 4.7-5.8h-3.2l1-5.6Z" fill={c.light} opacity="0.75" />
    </>
  ),
  /** 灯泡：点子 */
  bulb: (c) => (
    <>
      <path d="M12 2.6a6.2 6.2 0 0 1 3.6 11.3c-.5.4-.9 1-.9 1.7v1H9.3v-1c0-.7-.4-1.3-.9-1.7A6.2 6.2 0 0 1 12 2.6Z" fill={c.base} />
      <path d="M12 6.2a3.4 3.4 0 0 1 1.7 6.3l-.5.3V9.9h-2.4v2.9l-.5-.3A3.4 3.4 0 0 1 12 6.2Z" fill={c.light} opacity="0.7" />
      <path d="M9.6 18.4h4.8v1.1a1 1 0 0 1-1 1h-2.8a1 1 0 0 1-1-1v-1.1Z" fill={c.deep} />
      <path d="M10.4 21.1h3.2v.5a.9.9 0 0 1-.9.9h-1.4a.9.9 0 0 1-.9-.9v-.5Z" fill={c.deep} />
    </>
  ),
  /** 书本：知识 */
  book: (c) => (
    <>
      <path d="M4.4 4.6c2.6-1.3 5.2-1.3 7.6.5 2.4-1.8 5-1.8 7.6-.5v13.2c-2.6-1.3-5.2-1.3-7.6.5-2.4-1.8-5-1.8-7.6-.5V4.6Z" fill={c.base} />
      <path d="M11 5.1v13.2c.4.1.7.3 1 .5.3-.2.6-.4 1-.5V5.1c-.3-.2-.7-.3-1-.4-.3.1-.7.2-1 .4Z" fill={c.light} opacity="0.6" />
      <path d="M4.4 17.8c2.6-1.3 5.2-1.3 7.6.5-2.4-1.1-5-1.1-7.6.2v-.7Z" fill={c.deep} />
      <path d="M19.6 17.8c-2.6-1.3-5.2-1.3-7.6.5 2.4-1.1 5-1.1 7.6.2v-.7Z" fill={c.deep} />
    </>
  ),
  /** 四角星芒：精选优质 */
  sparkle: (c) => (
    <>
      <path d="M11.4 2.4c1 5.2 2.9 7.1 8.1 8.1-5.2 1-7.1 2.9-8.1 8.1-1-5.2-2.9-7.1-8.1-8.1 5.2-1 7.1-2.9 8.1-8.1Z" fill={c.base} />
      <path d="M11.4 6.6c.4 2.6 1.4 3.6 4 4-2.6.4-3.6 1.4-4 4-.4-2.6-1.4-3.6-4-4 2.6-.4 3.6-1.4 4-4Z" fill={c.light} opacity="0.8" />
      <path d="M18.6 14.4c.5 2.4 1.3 3.2 3.7 3.7-2.4.5-3.2 1.3-3.7 3.7-.5-2.4-1.3-3.2-3.7-3.7 2.4-.5 3.2-1.3 3.7-3.7Z" fill={c.deep} />
    </>
  ),
  /** 齿轮：按需配置 */
  gear: (c) => (
    <>
      <path
        d="M10.6 2.4h2.8l.6 2.2c.7.2 1.3.5 1.9 1l2.2-.7 1.4 2.4-1.7 1.5c.1.4.1.8.1 1.2 0 .4 0 .8-.1 1.2l1.7 1.5-1.4 2.4-2.2-.7c-.6.4-1.2.8-1.9 1l-.6 2.2h-2.8l-.6-2.2c-.7-.2-1.3-.5-1.9-1l-2.2.7-1.4-2.4 1.7-1.5c-.1-.4-.1-.8-.1-1.2 0-.4 0-.8.1-1.2L3.9 7.3l1.4-2.4 2.2.7c.6-.4 1.2-.8 1.9-1l.6-2.2Zm1.4 6.2a3.4 3.4 0 1 0 0 6.8 3.4 3.4 0 0 0 0-6.8Z"
        fill={c.base}
        fillRule="evenodd"
      />
      <circle cx="12" cy="12" r="1.7" fill={c.light} />
    </>
  ),
  /** 插头：对接外部服务 */
  plug: (c) => (
    <>
      <path d="M6.2 7.1a1.3 1.3 0 0 1 1.3-1.3h9a1.3 1.3 0 0 1 1.3 1.3v3.4a5.8 5.8 0 0 1-11.6 0V7.1Z" fill={c.base} />
      <path d="M8.4 7.6h3.4v6.2H8.4c-.7-.9-1.1-2-1.1-3.1s.4-2.3 1.1-3.1Z" fill={c.light} opacity="0.55" />
      <path d="M9.1 2.8h1.9v4.2H9.1zM13 2.8h1.9v4.2H13z" fill={c.deep} />
      <path d="M11 16.3h2v1.6h-2z" fill={c.deep} />
      <path d="M10.4 17.9h3.2v2.4a1.6 1.6 0 0 1-3.2 0v-2.4Z" fill={c.base} />
    </>
  ),
  /** 云：云端服务（云体 + 左上反光圆，避免复杂裁切路径） */
  cloud: (c) => (
    <>
      <path d="M7.2 19.2h9.6a4.4 4.4 0 0 0 .5-8.7 5.8 5.8 0 0 0-11 .9 3.9 3.9 0 0 0 .9 7.8Z" fill={c.base} />
      <circle cx="10.4" cy="12.2" r="2.6" fill={c.light} opacity="0.55" />
    </>
  ),
  /** 链路：打通两端（双扣环，两色区分） */
  link: (c) => (
    <>
      <path d="M10.2 6.6a5.4 5.4 0 0 0 0 10.8h3.6v-2.4h-3.6a3 3 0 0 1 0-6h3.6V6.6h-3.6Z" fill={c.base} />
      <path d="M13.8 6.6v2.4h3.6a3 3 0 0 1 0 6h-3.6v2.4h3.6a5.4 5.4 0 0 0 0-10.8h-3.6Z" fill={c.deep} />
      <path d="M8.6 10.8h6.8a1.2 1.2 0 0 1 0 2.4H8.6a1.2 1.2 0 0 1 0-2.4Z" fill={c.light} />
    </>
  ),
  /** 叶片：生态与生长 */
  leaf: (c) => (
    <>
      <path d="M20.6 3.4c0 9.2-4.9 14.6-12.1 14.6-2.1 0-3.8-.5-4.8-1.4-.9-6 1.8-10.2 6.1-12 3.4-1.5 7.7-1.5 10.8-1.2Z" fill={c.base} />
      <path d="M18.4 5.6c-3.3 3.6-6.2 8-7.8 12.4.6.1 1.2.1 1.9.1 1-4.4 3.4-8.6 6.9-11.9-.3-.2-.6-.4-1-.6Z" fill={c.light} opacity="0.65" />
      <path d="M3.7 20.6c2.9-5.7 6.6-9.1 11.4-11.3-5.1 1.7-8.9 5.1-11.9 11.1l.5.2Z" fill={c.deep} />
    </>
  ),
};

/** 渲染图案：图案函数按三档色（主体 / 加深 / 提亮）分面绘制 */
function Art({ name, size, colors }: { name: string; size: number; colors: ArtColors }) {
  const draw = ART[name];
  if (!draw) return null;
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} aria-hidden>
      {draw(colors)}
    </svg>
  );
}

/** 双卡叠加的标题图标（发现 <图标> Plugins）。
 *
 * 两张卡**都有图案**：前卡放品类主图案，后卡露出一角并放点阵角标，
 * 表达"这是一叠卡、后面还有更多"。鼠标移到标题上时两卡向两侧散开。
 * 无边框：前卡靠白底与极淡柔影区别于页面背景，后卡是纯浅色块。 */
export function KindMark({ kind, size = 32 }: { kind: MarketKind; size?: number }) {
  const accent = KIND_ACCENT[kind];
  const art = kind === "plugin" ? "puzzle" : kind === "skill" ? "bolt" : "plug";
  const colors: ArtColors = {
    base: accent.deep,
    deep: `color-mix(in oklab, ${accent.strong}, black 38%)`,
    light: accent.light,
  };
  return (
    <span className="mkt-kind-mark" style={{ width: size, height: size }} aria-hidden>
      <span className="mkt-kind-card mkt-kind-card-back" style={{ background: accent.soft }}>
        <svg viewBox="0 0 20 20" width={Math.round(size * 0.32)} height={Math.round(size * 0.32)}>
          <circle cx="4.4" cy="4.4" r="2.1" fill={accent.strong} opacity="0.5" />
          <circle cx="13.6" cy="4.4" r="2.1" fill={accent.strong} opacity="0.5" />
          <circle cx="4.4" cy="13.6" r="2.1" fill={accent.strong} opacity="0.5" />
        </svg>
      </span>
      <span className="mkt-kind-card mkt-kind-card-front">
        <Art name={art} size={Math.round(size * 0.7)} colors={colors} />
      </span>
    </span>
  );
}

/** 单张装饰卡片的描述 */
interface DecoSpec {
  shape: DecoShape;
  /** 色系（决定卡片底与图标三色） */
  tone: Tone;
  /** 图案名（ART 的键，组内不重复） */
  art: string;
  /** 目标位置（相对容器左上角，px） */
  x: number;
  y: number;
  /** 视觉层级（越大越靠上） */
  z?: number;
  rotate?: number;
  /** 展开时长与曲线：让"爆发"有先后与快慢的层次 */
  dur: string;
  ease: string;
  /** 与主卡成组时的联动标记（hover 主卡时一并微动） */
  groupWith?: number;
}

/** 容器尺寸：必须与 global.css 的 .market-hero-art / .mkt-deco-* 一致，否则折叠位移会算偏。 */
export const DECO_BOX = { w: 272, h: 144 };
const DECO_SIZE: Record<DecoShape, number> = { shield: 66, tile: 60, ring: 56, hex: 44 };
/** 图案尺寸（按卡片大小分级）：形状本身已是实心面，图案取卡片内切圆的约 6 成 */
const DECO_ART_SIZE: Record<DecoShape, number> = { shield: 30, tile: 28, ring: 27, hex: 22 };

const SPRING = "var(--ease-spring)";
const DECEL = "var(--ease-emphasized-decel)";

/** 每类的卡片组：布局一致（右组成叠 + 左卡独立 + 下卡轻叠），图案与配色随品类不同。
 *  坐标设计（容器 272×144）：
 *    · 主卡 tile 在右上 (178,4)，角标卡 hex 叠在其左下 (148,44) —— 两两重叠；
 *    · 下卡 shield 压在组下沿 (126,74) —— 与 hex 轻叠；
 *    · 左卡 ring 独立在左侧 (6,42) —— 与其余三张完全分离（"有些在两边"）。
 *  重心右上偏重、左下留白，形成对角平衡。 */
const DECOR: Record<MarketKind, DecoSpec[]> = {
  plugin: [
    { shape: "tile", tone: "amber", art: "puzzle", x: 178, y: 4, z: 3, rotate: -4, dur: "980ms", ease: DECEL },
    { shape: "hex", tone: "violet", art: "cube", x: 148, y: 44, z: 2, rotate: 6, dur: "860ms", ease: SPRING, groupWith: 0 },
    { shape: "shield", tone: "green", art: "layers", x: 126, y: 74, z: 1, rotate: -5, dur: "1080ms", ease: DECEL },
    { shape: "ring", tone: "sky", art: "bolt", x: 6, y: 42, dur: "760ms", ease: SPRING },
  ],
  skill: [
    { shape: "tile", tone: "green", art: "book", x: 178, y: 4, z: 3, rotate: -4, dur: "980ms", ease: DECEL },
    { shape: "hex", tone: "amber", art: "bulb", x: 148, y: 44, z: 2, rotate: 6, dur: "860ms", ease: SPRING, groupWith: 0 },
    { shape: "shield", tone: "teal", art: "sparkle", x: 126, y: 74, z: 1, rotate: -5, dur: "1080ms", ease: DECEL },
    { shape: "ring", tone: "violet", art: "gear", x: 6, y: 42, dur: "760ms", ease: SPRING },
  ],
  connector: [
    { shape: "tile", tone: "teal", art: "plug", x: 178, y: 4, z: 3, rotate: -4, dur: "980ms", ease: DECEL },
    { shape: "hex", tone: "sky", art: "cloud", x: 148, y: 44, z: 2, rotate: 6, dur: "860ms", ease: SPRING, groupWith: 0 },
    { shape: "shield", tone: "green", art: "leaf", x: 126, y: 74, z: 1, rotate: -5, dur: "1080ms", ease: DECEL },
    { shape: "ring", tone: "amber", art: "link", x: 6, y: 42, dur: "760ms", ease: SPRING },
  ],
};

/** 装饰卡片组：三类各自不同，key=kind 保证切标签时重挂载以重播「奇点爆发」入场动画 */
export function MarketDecor({ kind }: { kind: MarketKind }) {
  const cards = DECOR[kind];
  return (
    <div className="mkt-decor" aria-hidden key={kind}>
      {cards.map((d, i) => {
        // 「奇点爆发」：把每张卡片折回容器中心所需的位移交给 CSS 变量，
        // 由 keyframes 从该位移的极小尺度散开（TSX 算坐标、CSS 只管动画）。
        const size = DECO_SIZE[d.shape];
        const foldX = Math.round(DECO_BOX.w / 2 - (d.x + size / 2));
        const foldY = Math.round(DECO_BOX.h / 2 - (d.y + size / 2));
        const { ink, colors } = toneOf(d.tone);
        return (
          <span
            key={i}
            className={`mkt-deco mkt-deco-${d.shape}${d.groupWith != null ? " is-grouped" : ""}`}
            style={{
              left: `${d.x}px`,
              top: `${d.y}px`,
              zIndex: d.z ?? 1,
              ["--deco-rotate" as string]: `${d.rotate ?? 0}deg`,
              ["--deco-delay" as string]: `${i * 70}ms`,
              ["--deco-dur" as string]: d.dur,
              ["--deco-ease" as string]: d.ease,
              ["--fold-x" as string]: `${foldX}px`,
              ["--fold-y" as string]: `${foldY}px`,
              ["--deco-ink" as string]: ink,
            }}
          >
            {/* 卡片本体：造型轮廓（SVG 承载，因为圆环/六边/盾形都不是矩形）
                + **纯浅色填充、不设描边**（参考图卡片无边框）；
                投影用底色派生，比边框更轻但仍有"贴在页面上"的实体感。 */}
            <svg className="mkt-deco-shape" viewBox="0 0 48 48" aria-hidden style={{ fill: ink }}>
              {SHAPES[d.shape]}
            </svg>
            {/* 图案直接绘制在卡片上（多色分层），颜色在卡片底色上继续加深 */}
            <span className="mkt-deco-art">
              <Art name={d.art} size={DECO_ART_SIZE[d.shape]} colors={colors} />
            </span>
          </span>
        );
      })}
    </div>
  );
}

/** 确定性伪随机（同一 seed 每次渲染得到同一笔迹，避免动画/重渲染时线条抖动跳变） */
function noise(seed: number): number {
  const x = Math.sin(seed * 12.9898) * 43758.5453;
  return x - Math.floor(x);
}

/** 铅笔笔锋笔迹：填充路径（上沿 + 下沿围成一条带子），宽度沿长度连续变化。
 *  等宽 `stroke` 无论曲线怎么调都是"一根粗细不变的管子"，表达不出笔锋；
 *  这里宽度包络两端收尖、中段饱满，下沿比上沿更贴基线，像笔尖压出的痕迹。 */
function pencilRibbon(w: number, h: number, seed: number, weight: number): string {
  const steps = Math.max(16, Math.round(w / 6));
  const mid = h / 2;
  const top: string[] = [];
  const bottom: string[] = [];
  for (let i = 0; i <= steps; i++) {
    const t = i / steps;
    const x = 1.5 + t * (w - 3);
    // 行笔起伏：低频摆动 + 细噪声，幅度收在 1.1px 内（太大会像抖动的手）
    const wobble = Math.sin(t * Math.PI * 2.1 + seed) * 0.7 + (noise(seed + i * 4.7) - 0.5) * 0.9;
    const y = mid + wobble;
    // 宽度包络：两端 → 0（收尖），中段 → 1（饱满）
    const env = Math.pow(Math.abs(Math.sin(Math.PI * t)), 0.72);
    const half = Math.max(0.15, (weight / 2) * env);
    top.push(`${x.toFixed(1)} ${(y - half).toFixed(2)}`);
    bottom.push(`${x.toFixed(1)} ${(y + half * 0.78).toFixed(2)}`);
  }
  return `M${top.join("L")}L${bottom.reverse().join("L")}Z`;
}

/** 铅笔手绘标记线（关键词下划线）：主笔 + 更淡的复描，
 *  入场用 clip-path 从左往右揭示，等价于"从起始端画到结尾端"。 */
export function WaveUnderline({ color, chars = 2 }: { color: string; chars?: number }) {
  const h = 16;
  const w = Math.max(32, Math.round(chars * 16)) + 6;
  return (
    <svg className="mkt-wave" viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" aria-hidden>
      <path className="mkt-wave-ink mkt-wave-echo" d={pencilRibbon(w, h, 19.7, 2.6)} style={{ fill: color }} />
      <path className="mkt-wave-ink" d={pencilRibbon(w, h, 7.3, 5)} style={{ fill: color }} />
    </svg>
  );
}
