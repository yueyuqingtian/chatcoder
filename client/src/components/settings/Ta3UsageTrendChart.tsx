/** Ta+3「用量趋势」柱状图——复用「用量统计」页的纯 SVG 组件 UsageTrendChart。
 *
 * 为什么改成薄封装：上一版用 echarts 单独画一套（坐标轴、浮层、渐变柱都要自己配），
 * 与用量统计页的图表观感对不上，用户反馈「样式丑」。现在本组件只做数据适配：
 * 把 ta3 的按天 / 按月聚合点转成 UsageTrendChart 需要的 series / keys / 刻度与浮层文案，
 * 外框、图例、坐标轴、悬停浮层全部沿用用量统计页，视觉口径完全一致，也不再背图表库依赖。
 *
 * 取色口径：项目的颜色一律走 CSS 令牌（--accent 等），而令牌计算值是 oklch / color-mix，
 * 且 SVG 的 fill / stroke 是呈现属性、不做 var() 替换；所以这里把 --accent 写进探针元素的
 * color，取计算值后画到 1×1 canvas 上读像素转成 rgb（与 PixelFlow 同一做法），
 * 并在 data-theme / data-world / <html> 内联主题变量变化时重新取色。
 */
import { useEffect, useMemo, useState } from "react";
import { UsageTrendChart, type TrendSeries } from "./usage/UsageTrendChart";

/** 与 Ta3QuotaSection 里 trendView.points 的字段保持一致 */
export interface Ta3UsageTrendPoint {
  date: string;
  label: string;
  requestCount: number;
  tokenCount: number;
}

interface Ta3UsageTrendChartProps {
  points: Ta3UsageTrendPoint[];
  period: "DAILY" | "MONTHLY";
}

/** 把 --accent 令牌转成可直接写进 SVG 属性的 rgb 字面量；取不到时用兜底深灰。 */
function readAccentRgb(): string {
  const fallback = "#2f2f34";
  if (typeof document === "undefined") return fallback;
  try {
    const probe = document.createElement("span");
    probe.style.display = "none";
    probe.style.color = "var(--accent)";
    document.body.appendChild(probe);
    const computed = getComputedStyle(probe).color;
    probe.remove();
    const canvas = document.createElement("canvas");
    canvas.width = canvas.height = 1;
    const ctx = canvas.getContext("2d", { willReadFrequently: true });
    if (!ctx) return fallback;
    // 先铺兜底色：浏览器不认识 computed（如未解析的 oklch 变量）时不会污染像素
    ctx.fillStyle = fallback;
    ctx.fillStyle = computed || fallback;
    ctx.fillRect(0, 0, 1, 1);
    const [r, g, b] = ctx.getImageData(0, 0, 1, 1).data;
    return `rgb(${r}, ${g}, ${b})`;
  } catch {
    return fallback;
  }
}

export function Ta3UsageTrendChart({ points, period }: Ta3UsageTrendChartProps) {
  const [accent, setAccent] = useState<string>(readAccentRgb);

  // 主题 / 世界 / 自定义主题色（写在 <html> 内联样式上）变化时令牌色会变，重新取色
  useEffect(() => {
    if (typeof MutationObserver === "undefined") return;
    const mo = new MutationObserver(() => {
      const next = readAccentRgb();
      setAccent((prev) => (prev === next ? prev : next));
    });
    mo.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["data-theme", "data-world", "style"],
    });
    return () => mo.disconnect();
  }, []);

  // 单序列：只有 token 一个维度（请求次数走浮层附加说明，不占柱形）
  const series = useMemo<TrendSeries[]>(
    () => [
      {
        display_name: period === "MONTHLY" ? "每月 token" : "每日 token",
        color: accent,
        record: Object.fromEntries(points.map((p) => [p.date, p.tokenCount])),
      },
    ],
    [points, period, accent],
  );

  const keys = useMemo(() => points.map((p) => p.date), [points]);
  // 按 x 轴键（日期）反查原始点，供刻度文字 / 浮层文案使用
  const byDate = useMemo(() => new Map(points.map((p) => [p.date, p])), [points]);

  return (
    <UsageTrendChart
      series={series}
      keys={keys}
      // 按天 / 按月都是离散周期聚合，柱状比折线更贴合「每期一根」的读法，
      // 也避免点数很少时折线只剩几段孤零零的线段
      kind="bar"
      tickLabel={(key) => byDate.get(key)?.label ?? key.slice(5)}
      tipTitle={(key) => key}
      tipExtra={(key) => {
        const point = byDate.get(key);
        return point ? `请求 ${point.requestCount} 次` : null;
      }}
    />
  );
}
