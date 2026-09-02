/** 每日 Token 趋势图：多模型平滑折线（Catmull-Rom→贝塞尔）。纯 SVG，无依赖。 */
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

export function UsageTrendChart({ series, dates }: { series: TrendSeries[]; dates: string[] }) {
  const n = dates.length;
  const maxY = Math.max(1, ...series.flatMap((s) => dates.map((d) => s.record[d] || 0)));
  const x = (i: number) => PAD.left + (n <= 1 ? innerW / 2 : (i / (n - 1)) * innerW);
  const y = (v: number) => PAD.top + innerH - (v / maxY) * innerH;

  const gridLevels = [0, 0.25, 0.5, 0.75, 1];
  const labelIdx = n === 0 ? [] : n === 1 ? [0] : [0, Math.floor((n - 1) / 2), n - 1];

  return (
    <div className="usage-trend">
      <div className="usage-trend-legend">
        {series.map((s) => (
          <span key={s.display_name} className="usage-trend-legend-item">
            <i className="usage-trend-dot" style={{ background: s.color }} />
            {s.display_name}
          </span>
        ))}
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} className="usage-trend-svg" preserveAspectRatio="xMidYMid meet">
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
          return (
            <path key={s.display_name} d={smoothPath(pts)} fill="none" stroke={s.color} strokeWidth={2} strokeLinecap="round" />
          );
        })}
      </svg>
    </div>
  );
}
