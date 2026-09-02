/** 模型用量环形图：SVG donut + 右侧图例列表。纯 SVG，无依赖。 */
import { fmtTokens } from "./chartUtils";

export interface DonutSeg {
  label: string;
  value: number;
  color: string;
  pct: number;
}

const SIZE = 200;
const CX = SIZE / 2;
const CY = SIZE / 2;
const R = 78;
const STROKE = 22;
const PAD = 0.02; // 段间空隙（弧度）

function arc(a0: number, a1: number, r: number): string {
  const large = a1 - a0 > Math.PI ? 1 : 0;
  const sx = CX + r * Math.cos(a0);
  const sy = CY + r * Math.sin(a0);
  const ex = CX + r * Math.cos(a1);
  const ey = CY + r * Math.sin(a1);
  return `M ${sx},${sy} A ${r},${r} 0 ${large} 1 ${ex},${ey}`;
}

export function UsageDonutChart({ segments, centerValue }: { segments: DonutSeg[]; centerValue: number }) {
  const total = Math.max(1, segments.reduce((s, x) => s + x.value, 0));
  let angle = -Math.PI / 2;
  const arcs = segments.map((seg) => {
    const sweep = (seg.value / total) * Math.PI * 2;
    const a0 = angle + PAD;
    const a1 = angle + sweep - PAD;
    angle += sweep;
    return a1 > a0 ? { seg, a0, a1 } : null;
  });

  return (
    <div className="usage-donut-wrap">
      <div className="usage-donut" style={{ width: SIZE, height: SIZE }}>
        <svg viewBox={`0 0 ${SIZE} ${SIZE}`} width={SIZE} height={SIZE}>
          <circle cx={CX} cy={CY} r={R} fill="none" stroke="var(--bg-hover)" strokeWidth={STROKE} />
          {arcs.map((a, i) =>
            a ? (
              <path
                key={i}
                d={arc(a.a0, a.a1, R)}
                fill="none"
                stroke={a.seg.color}
                strokeWidth={STROKE}
                strokeLinecap="butt"
              />
            ) : null,
          )}
        </svg>
        <div className="usage-donut-center">
          <span className="usage-donut-value">{fmtTokens(centerValue)}</span>
          <span className="usage-donut-unit">tokens</span>
        </div>
      </div>
      <div className="usage-donut-legend">
        {segments.map((s) => (
          <div key={s.label} className="usage-donut-legend-item">
            <i className="usage-donut-dot" style={{ background: s.color }} />
            <span className="usage-donut-legend-name" title={s.label}>{s.label}</span>
            <span className="usage-donut-legend-pct">{s.pct}%</span>
            <span className="usage-donut-legend-val">{fmtTokens(s.value)}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
