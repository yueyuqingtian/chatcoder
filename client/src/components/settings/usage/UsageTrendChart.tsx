/** 每日 Token 趋势图：多模型平滑折线（Catmull-Rom→贝塞尔）+ 悬停十字线与 tooltip。纯 SVG，无依赖。 */
import { useRef, useState, type MouseEvent } from "react";
import { fmtTokens } from "./chartUtils";

export interface TrendSeries {
  display_name: string;
  color: string;
  /** date(YYYY-MM-DD) → tokens */
  record: Record<string, number>;
}

const W = 760;
const H = 240;
const PAD = { left: 48, right: 16, top: 16, bottom: 30 };
const innerW = W - PAD.left - PAD.right;
const innerH = H - PAD.top - PAD.bottom;
const WEEKDAYS = ["日", "一", "二", "三", "四", "五", "六"];

function smoothPath(pts: Array<[number, number]>): string {
  if (pts.length === 0) return "";
  if (pts.length === 1) return `M ${pts[0][0]},${pts[0][1]}`;
  let d = `M ${pts[0][0]},${pts[0][1]}`;
  for (let i = 0; i < pts.length - 1; i++) {
    const p0 = pts[Math.max(0, i - 1)];
    const p1 = pts[i];
    const p2 = pts[i + 1];
    const p3 = pts[Math.min(pts.length - 1, i + 2)];
    const c1x = p1[0] + (p2[0] - p0[0]) / 6;
    const c1y = p1[1] + (p2[1] - p0[1]) / 6;
    const c2x = p2[0] - (p3[0] - p1[0]) / 6;
    const c2y = p2[1] - (p3[1] - p1[1]) / 6;
    d += ` C ${c1x},${c1y} ${c2x},${c2y} ${p2[0]},${p2[1]}`;
  }
  return d;
}

function dateLabel(iso: string): string {
  const [y, m, d] = iso.split("-").map(Number);
  return `${m}月${d}日 周${WEEKDAYS[new Date(y, m - 1, d).getDay()]}`;
}

export function UsageTrendChart({ series, dates }: { series: TrendSeries[]; dates: string[] }) {
  const n = dates.length;
  const maxY = Math.max(1, ...series.flatMap((s) => dates.map((d) => s.record[d] || 0)));
  const x = (i: number) => PAD.left + (n <= 1 ? innerW / 2 : (i / (n - 1)) * innerW);
  const y = (v: number) => PAD.top + innerH - (v / maxY) * innerH;

  const gridLevels = [0, 0.25, 0.5, 0.75, 1];
  const labelIdx = n === 0 ? [] : n === 1 ? [0] : [0, Math.floor((n - 1) / 2), n - 1];

  const svgRef = useRef<SVGSVGElement | null>(null);
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);
  const [hoverLegend, setHoverLegend] = useState<string | null>(null);
  const [tipPos, setTipPos] = useState<{ x: number; y: number }>({ x: 0, y: 0 });

  const onMove = (e: MouseEvent<SVGSVGElement>) => {
    if (n === 0) return;
    const rect = svgRef.current?.getBoundingClientRect();
    if (!rect) return;
    const svgX = ((e.clientX - rect.left) / rect.width) * W;
    const i = Math.max(0, Math.min(n - 1, Math.round((svgX - PAD.left) / innerW * (n - 1))));
    setHoverIdx(n <= 1 ? 0 : i);
    // x/y 统一为渲染像素坐标（viewBox 的 svgY 仅用于上面日期索引计算）
    setTipPos({ x: e.clientX - rect.left, y: e.clientY - rect.top });
  };
  const onLeave = () => setHoverIdx(null);

  const hoverDate = hoverIdx != null ? dates[hoverIdx] : null;
  const hoverTotal = hoverDate
    ? series.reduce((s, item) => s + (item.record[hoverDate] || 0), 0)
    : 0;

  // tooltip 接近右缘时整体左移，防止溢出（阈值随容器宽自适应）
  const tipRight = tipPos.x > (svgRef.current?.getBoundingClientRect().width || 760) * 0.55;
  const tipStyle = {
    left: tipPos.x,
    top: tipPos.y,
    transform: tipRight ? "translate(calc(-100% - 12px), -50%)" : "translate(12px, -50%)",
  };

  return (
    <div className="usage-trend">
      <div className="usage-trend-legend" onMouseLeave={() => setHoverLegend(null)}>
        {series.map((s) => (
          <span
            key={s.display_name}
            className="usage-trend-legend-item"
            onMouseEnter={() => setHoverLegend(s.display_name)}
          >
            <i className="usage-trend-dot" style={{ background: s.color }} />
            {s.display_name}
          </span>
        ))}
      </div>
      <div className="usage-trend-chart">
        <svg
          ref={svgRef}
          viewBox={`0 0 ${W} ${H}`}
          className="usage-trend-svg"
          preserveAspectRatio="xMidYMid meet"
          onMouseMove={onMove}
          onMouseLeave={onLeave}
        >
          {gridLevels.map((lv) => {
            const gy = y(lv * maxY);
            return (
              <g key={lv}>
                <line x1={PAD.left} x2={W - PAD.right} y1={gy} y2={gy} className="usage-trend-grid" />
                <text x={PAD.left - 6} y={gy + 3} className="usage-trend-ylabel" textAnchor="end">
                  {fmtTokens(Math.round(lv * maxY))}
                </text>
              </g>
            );
          })}
          {labelIdx.map((i) => {
            const d = dates[i];
            const parts = d.split("-");
            return (
              <text key={i} x={x(i)} y={H - 8} className="usage-trend-xlabel" textAnchor="middle">
                {`${Number(parts[1])}月${Number(parts[2])}日`}
              </text>
            );
          })}
          {series.map((s) => {
            const pts: Array<[number, number]> = dates.map((d, i) => [x(i), y(s.record[d] || 0)]);
            const dimmed = hoverLegend != null && hoverLegend !== s.display_name;
            return (
              <path
                key={s.display_name}
                d={smoothPath(pts)}
                fill="none"
                stroke={s.color}
                strokeWidth={hoverLegend === s.display_name ? 3 : 2}
                strokeLinecap="round"
                opacity={dimmed ? 0.35 : 1}
                style={{ transition: "stroke-width .12s ease, opacity .12s ease" }}
              />
            );
          })}
          {hoverIdx != null && (
            <g>
              <line x1={x(hoverIdx)} x2={x(hoverIdx)} y1={PAD.top} y2={PAD.top + innerH} className="usage-trend-hover-line" />
              {series.map((s) => {
                const v = s.record[dates[hoverIdx]] || 0;
                if (v <= 0) return null;
                return (
                  <circle key={s.display_name} cx={x(hoverIdx)} cy={y(v)} r={4} fill={s.color} className="usage-trend-hover-dot" />
                );
              })}
            </g>
          )}
        </svg>
        {hoverDate && (
          <div className="usage-trend-tip" style={tipStyle}>
            <div className="usage-tip-date">{dateLabel(hoverDate)}</div>
            {series.map((s) => {
              const v = s.record[hoverDate] || 0;
              return (
                <div key={s.display_name} className="usage-tip-row">
                  <i className="usage-trend-dot" style={{ background: s.color }} />
                  <span className="usage-tip-name">{s.display_name}</span>
                  <span className="usage-tip-val">{fmtTokens(v)}</span>
                </div>
              );
            })}
            <div className="usage-tip-sep" />
            <div className="usage-tip-row usage-tip-total">
              <span className="usage-tip-name">合计</span>
              <span className="usage-tip-val">{fmtTokens(hoverTotal)}</span>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
