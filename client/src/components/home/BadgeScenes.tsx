/** 嗒嗒徽章的六幅主题插画（plan-114-452）。
 * 每个主题世界一幅，同一套构图语言：天空渐变（back）→ 远景与点缀（mid）→ 嗒嗒 → 前景遮挡（front），
 * 嗒嗒总是从前景后面「探出头」——与应用图标「从口袋探头」同一个意思。
 * 颜色全部来自 .es-badge 上的 --bd-* 变量（home.css 按世界 × 明暗给值），所以换主题 / 切深色只改变量，不重绘。
 * 三层各自带景深 --d：指针移动时前景位移大、远景位移小，形成视差。 */
import type { ReactNode } from "react";
import type { DadaMood } from "../mascot/Dada";
import type { World } from "../../store/theme";

export interface SceneDef {
  back: ReactNode;
  mid: ReactNode;
  front: ReactNode;
  /** 嗒嗒：中心点（占图块的百分比）与尺寸（占图块宽度的百分比） */
  dada: { x: number; y: number; size: number; mood: DadaMood };
}

const stop = (o: number, c: string) => <stop offset={o} style={{ stopColor: `var(${c})` }} />;
/** 天空渐变：每个场景一个唯一 id（同一页面可能同时渲染多枚） */
function Sky({ id }: { id: string }) {
  return (
    <>
      <defs>
        <linearGradient id={id} x1="0" y1="0" x2="0" y2="1">{stop(0, "--bd-1")}{stop(1, "--bd-2")}</linearGradient>
      </defs>
      <rect width="112" height="112" fill={`url(#${id})`} />
    </>
  );
}

/** 五瓣花：樱时世界的点缀 */
function Blossom({ x, y, s = 1, o = 1 }: { x: number; y: number; s?: number; o?: number }) {
  return (
    <g transform={`translate(${x} ${y}) scale(${s})`} opacity={o}>
      {[0, 72, 144, 216, 288].map((a) => (
        <ellipse key={a} cx="0" cy="-4.2" rx="3.2" ry="4.4" transform={`rotate(${a})`} style={{ fill: "var(--bd-5)" }} />
      ))}
      <circle r="1.6" style={{ fill: "var(--bd-6)" }} />
    </g>
  );
}

const Spark = ({ x, y, r = 2, o = 1 }: { x: number; y: number; r?: number; o?: number }) => (
  <path d={`M${x} ${y - r}Q${x} ${y} ${x + r} ${y}Q${x} ${y} ${x} ${y + r}Q${x} ${y} ${x - r} ${y}Q${x} ${y} ${x} ${y - r}Z`} style={{ fill: "var(--bd-6)" }} opacity={o} />
);

/* ── 石墨：编辑器窗口，嗒嗒从窗口上沿探头 ───────────────────── */
function graphite(id: string): SceneDef {
  return {
    back: (<svg viewBox="0 0 112 112" preserveAspectRatio="xMidYMid slice" aria-hidden><Sky id={id} />
      <circle cx="84" cy="30" r="26" style={{ fill: "var(--bd-3)" }} opacity=".35" />
      <circle cx="84" cy="30" r="13" style={{ fill: "var(--bd-3)" }} opacity=".4" /></svg>),
    mid: (<svg viewBox="0 0 112 112" preserveAspectRatio="xMidYMid slice" aria-hidden>
      <g fill="none" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" style={{ stroke: "var(--bd-3)" }} opacity=".85">
        <path d="M14 26l-6 5 6 5M24 26l6 5-6 5" />
        <path d="M90 14l-3 12" />
      </g>
      <Spark x={96} y={50} r={3} /><Spark x={20} y={52} r={2} o={.7} /></svg>),
    front: (<svg viewBox="0 0 112 112" preserveAspectRatio="xMidYMid slice" aria-hidden>
      <rect x="9" y="66" width="94" height="60" rx="10" style={{ fill: "var(--bd-4)" }} />
      <circle cx="19" cy="74" r="2" style={{ fill: "var(--bd-5)" }} /><circle cx="26" cy="74" r="2" style={{ fill: "var(--bd-6)" }} /><circle cx="33" cy="74" r="2" style={{ fill: "var(--bd-3)" }} />
      <g strokeWidth="3" strokeLinecap="round" fill="none">
        <path d="M19 86h20" style={{ stroke: "var(--bd-5)" }} /><path d="M44 86h26" style={{ stroke: "var(--bd-3)" }} />
        <path d="M27 95h30" style={{ stroke: "var(--bd-3)" }} /><path d="M62 95h14" style={{ stroke: "var(--bd-6)" }} />
        <path d="M19 104h12" style={{ stroke: "var(--bd-3)" }} />
      </g>
      <rect x="36" y="101.2" width="2.4" height="6" rx="1.2" className="bd-caret" style={{ fill: "var(--bd-6)" }} /></svg>),
    dada: { x: 50, y: 46, size: 58, mood: "calm" },
  };
}

/* ── 森林：层叠松林，嗒嗒在两棵松之间的坡后 ─────────────────── */
const Pine = ({ x, y, s, c }: { x: number; y: number; s: number; c: string }) => (
  <g transform={`translate(${x} ${y}) scale(${s})`} style={{ fill: `var(${c})` }}>
    <path d="M0 -26l9 14h-5l8 12h-6l9 14H-15l9-14h-6l8-12h-5z" />
    <rect x="-1.6" y="14" width="3.2" height="6" rx="1" />
  </g>
);
function forest(id: string): SceneDef {
  return {
    back: (<svg viewBox="0 0 112 112" preserveAspectRatio="xMidYMid slice" aria-hidden><Sky id={id} />
      <circle cx="82" cy="26" r="17" style={{ fill: "var(--bd-6)" }} opacity=".25" />
      <circle cx="82" cy="26" r="9" style={{ fill: "var(--bd-6)" }} /></svg>),
    mid: (<svg viewBox="0 0 112 112" preserveAspectRatio="xMidYMid slice" aria-hidden>
      <path d="M0 78C18 62 34 66 52 74c18-12 40-12 60 2v36H0z" style={{ fill: "var(--bd-3)" }} opacity=".7" />
      <Pine x={22} y={66} s={.62} c="--bd-3" /><Pine x={92} y={64} s={.7} c="--bd-3" /><Pine x={74} y={72} s={.5} c="--bd-3" />
      <circle cx="30" cy="44" r="1.3" style={{ fill: "var(--bd-6)" }} className="bd-fly" /><circle cx="66" cy="34" r="1.1" style={{ fill: "var(--bd-6)" }} className="bd-fly" />
      <circle cx="96" cy="52" r="1.2" style={{ fill: "var(--bd-6)" }} className="bd-fly" /></svg>),
    front: (<svg viewBox="0 0 112 112" preserveAspectRatio="xMidYMid slice" aria-hidden>
      <path d="M-4 90C16 76 34 82 56 88c20-8 40-8 62 2V120H-4z" style={{ fill: "var(--bd-4)" }} />
      <Pine x={14} y={80} s={1.05} c="--bd-4" /><Pine x={100} y={82} s={.95} c="--bd-4" />
      <path d="M0 102c20-6 40-4 56 0 18-4 38-4 56 2v14H0z" style={{ fill: "var(--bd-5)" }} opacity=".5" /></svg>),
    dada: { x: 52, y: 54, size: 56, mood: "calm" },
  };
}

/* ── 篝火：暮色星空 + 一堆火，嗒嗒在火边取暖 ───────────────── */
function campfire(id: string): SceneDef {
  return {
    back: (<svg viewBox="0 0 112 112" preserveAspectRatio="xMidYMid slice" aria-hidden><Sky id={id} />
      <circle cx="84" cy="24" r="9" style={{ fill: "var(--bd-6)" }} opacity=".9" /><circle cx="88" cy="21" r="8" style={{ fill: "var(--bd-1)" }} /></svg>),
    mid: (<svg viewBox="0 0 112 112" preserveAspectRatio="xMidYMid slice" aria-hidden>
      <Spark x={20} y={22} r={3} /><Spark x={44} y={14} r={2} o={.8} /><Spark x={64} y={30} r={2.4} o={.9} /><Spark x={100} y={50} r={2} o={.7} />
      <path d="M0 82C20 70 36 74 54 80c20-10 40-8 58 0v32H0z" style={{ fill: "var(--bd-3)" }} opacity=".8" /></svg>),
    front: (<svg viewBox="0 0 112 112" preserveAspectRatio="xMidYMid slice" aria-hidden>
      <path d="M-4 96C20 88 44 90 64 94c18-4 36-4 56 0v26H-4z" style={{ fill: "var(--bd-4)" }} />
      <g className="bd-flame" style={{ transformOrigin: "82px 98px" }}>
        <path d="M82 66c8 9 13 15 13 24a13 13 0 0 1-26 0c0-6 3-10 7-14 0 5 2 8 5 9-2-8 0-14 1-19z" style={{ fill: "var(--bd-5)" }} />
        <path d="M82 78c5 6 8 10 8 15a8 8 0 0 1-16 0c0-4 2-7 5-10 0 3 1 5 3 6-1-4-1-7 0-11z" style={{ fill: "var(--bd-6)" }} />
      </g>
      <g strokeLinecap="round" strokeWidth="5" style={{ stroke: "var(--bd-4)" }}><path d="M68 102l28-6" /><path d="M70 96l26 7" /></g>
      <path d="M68 102l28-6M70 96l26 7" strokeLinecap="round" strokeWidth="3.4" style={{ stroke: "var(--bd-3)" }} fill="none" opacity=".7" /></svg>),
    dada: { x: 34, y: 60, size: 56, mood: "happy" },
  };
}

/* ── 煦风：云海，嗒嗒从云里探头，纸飞机掠过 ─────────────────── */
function breeze(id: string): SceneDef {
  return {
    back: (<svg viewBox="0 0 112 112" preserveAspectRatio="xMidYMid slice" aria-hidden><Sky id={id} />
      <circle cx="26" cy="26" r="11" style={{ fill: "var(--bd-6)" }} opacity=".9" />
      <circle cx="26" cy="26" r="19" style={{ fill: "var(--bd-6)" }} opacity=".25" /></svg>),
    mid: (<svg viewBox="0 0 112 112" preserveAspectRatio="xMidYMid slice" aria-hidden>
      <g style={{ fill: "var(--bd-3)" }} opacity=".85"><rect x="52" y="30" width="46" height="13" rx="6.5" /><rect x="64" y="22" width="26" height="14" rx="7" /></g>
      <path d="M72 62l24-10-7 18-6-6-6 4z" style={{ fill: "var(--bd-5)" }} /><path d="M83 64l6-12" stroke="#fff" strokeWidth="1.2" opacity=".6" /></svg>),
    front: (<svg viewBox="0 0 112 112" preserveAspectRatio="xMidYMid slice" aria-hidden>
      <g style={{ fill: "var(--bd-4)" }}>
        <rect x="-6" y="78" width="124" height="40" rx="14" />
        <circle cx="20" cy="82" r="16" /><circle cx="46" cy="76" r="14" /><circle cx="92" cy="80" r="16" />
      </g>
      <path d="M-6 100c24-6 48-4 62 2 20-6 40-6 62 0v18H-6z" style={{ fill: "var(--bd-3)" }} opacity=".35" /></svg>),
    dada: { x: 54, y: 54, size: 58, mood: "calm" },
  };
}

/* ── 海潮：两层波浪，嗒嗒浮在浪上 ───────────────────────────── */
function tide(id: string): SceneDef {
  return {
    back: (<svg viewBox="0 0 112 112" preserveAspectRatio="xMidYMid slice" aria-hidden><Sky id={id} />
      <circle cx="82" cy="34" r="12" style={{ fill: "var(--bd-6)" }} />
      <g style={{ fill: "var(--bd-6)" }} opacity=".4"><rect x="70" y="52" width="24" height="2.4" rx="1.2" /><rect x="74" y="58" width="16" height="2.4" rx="1.2" /></g></svg>),
    mid: (<svg viewBox="0 0 112 112" preserveAspectRatio="xMidYMid slice" aria-hidden>
      <path d="M-4 72c14-8 28-8 42 0s28 8 42 0 24-6 36 0V120H-4z" style={{ fill: "var(--bd-3)" }} opacity=".8" />
      <circle cx="18" cy="40" r="2" fill="none" strokeWidth="1.4" style={{ stroke: "var(--bd-6)" }} /><circle cx="26" cy="30" r="1.4" fill="none" strokeWidth="1.2" style={{ stroke: "var(--bd-6)" }} /></svg>),
    front: (<svg viewBox="0 0 112 112" preserveAspectRatio="xMidYMid slice" aria-hidden>
      <g className="bd-wave">
        <path d="M-20 84c14-9 28-9 42 0s28 9 42 0 28-9 42 0 28 9 42 0V124H-20z" style={{ fill: "var(--bd-4)" }} />
        <path d="M-20 84c14-9 28-9 42 0s28 9 42 0 28-9 42 0 28 9 42 0" fill="none" strokeWidth="2" strokeLinecap="round" style={{ stroke: "var(--bd-6)" }} opacity=".7" />
      </g></svg>),
    dada: { x: 50, y: 56, size: 56, mood: "happy" },
  };
}

/* ── 樱时：花枝垂下，嗒嗒在树下，花瓣飘落 ───────────────────── */
function sakura(id: string): SceneDef {
  return {
    back: (<svg viewBox="0 0 112 112" preserveAspectRatio="xMidYMid slice" aria-hidden><Sky id={id} />
      <circle cx="30" cy="78" r="34" style={{ fill: "var(--bd-6)" }} opacity=".35" /></svg>),
    mid: (<svg viewBox="0 0 112 112" preserveAspectRatio="xMidYMid slice" aria-hidden>
      <g fill="none" strokeLinecap="round" strokeWidth="3" style={{ stroke: "var(--bd-4)" }}><path d="M116 8C92 12 74 18 58 30" /><path d="M84 14C84 22 82 28 76 34" strokeWidth="2" /></g>
      <Blossom x={96} y={12} s={1.1} /><Blossom x={78} y={20} s={.9} /><Blossom x={64} y={28} s={1.15} /><Blossom x={84} y={34} s={.8} o={.9} />
      <g style={{ fill: "var(--bd-5)" }} opacity=".85">
        <ellipse cx="30" cy="38" rx="3" ry="1.8" transform="rotate(30 30 38)" className="bd-petal" /><ellipse cx="46" cy="58" rx="2.6" ry="1.6" transform="rotate(-20 46 58)" className="bd-petal" />
        <ellipse cx="94" cy="62" rx="3" ry="1.8" transform="rotate(50 94 62)" className="bd-petal" /><ellipse cx="18" cy="68" rx="2.4" ry="1.5" transform="rotate(10 18 68)" className="bd-petal" />
      </g></svg>),
    front: (<svg viewBox="0 0 112 112" preserveAspectRatio="xMidYMid slice" aria-hidden>
      <path d="M-4 92C18 82 40 86 58 92c18-6 38-8 62 0v28H-4z" style={{ fill: "var(--bd-3)" }} />
      <path d="M-4 100c22-6 44-4 62 0 20-4 38-4 58 2v18H-4z" style={{ fill: "var(--bd-5)" }} opacity=".35" />
      <g style={{ fill: "var(--bd-6)" }} opacity=".9"><ellipse cx="24" cy="98" rx="3" ry="1.7" /><ellipse cx="86" cy="100" rx="3.2" ry="1.8" transform="rotate(-18 86 100)" /></g></svg>),
    dada: { x: 48, y: 56, size: 56, mood: "calm" },
  };
}

const MAKERS: Record<World, (id: string) => SceneDef> = { graphite, forest, campfire, breeze, tide, sakura };

export function makeScene(world: World, uid: string): SceneDef {
  return (MAKERS[world] ?? graphite)(`bd-sky-${uid}`);
}
