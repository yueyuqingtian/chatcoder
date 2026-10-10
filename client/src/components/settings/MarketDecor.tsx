/** 拓展页装饰（plan-59-286 视觉重做 → plan-20261009-ui U9 重绘）。
 *
 * 本文件只保留两样东西：
 *  · KIND_ACCENT —— 三个品类的色阶（标题名 / 规模徽标 / 手绘线共用，插画取色见 MarketArt.tsx）；
 *  · WaveUnderline —— 副标题关键词下的铅笔手绘标记线。
 * 标题旁的小标（KindMark）与英雄区插画（MarketDecor）已整体重绘，见 MarketArt.tsx。 */
import type { MarketKind } from "../../api/client";

export { KindMark, MarketDecor } from "./MarketArt";

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
