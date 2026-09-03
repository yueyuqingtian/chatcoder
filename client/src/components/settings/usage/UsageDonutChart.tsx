/** 模型用量环形图：SVG donut + 右侧图例列表 + 悬停分段高亮/tooltip/图例联动。纯 SVG，无依赖。 */
import { useState, type CSSProperties } from "react";
import { fmtTokens } from "./chartUtils";

export interface DonutSeg {
  label: string;
  value: number;
  color: string;
  pct: number;
  calls: number;
  /** "其他"聚合段的组成模型明细（非聚合段为空） */
  children?: DonutSeg[];
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
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);

  let angle = -Math.PI / 2;
  const arcs = segments.map((seg) => {
    const sweep = (seg.value / total) * Math.PI * 2;
    const a0 = angle + PAD;
    const a1 = angle + sweep - PAD;
    angle += sweep;
    return a1 > a0 ? { seg, a0, a1 } : null;
  });

  const hover = hoverIdx != null ? segments[hoverIdx] : null;

  // tooltip 定位到 hover 段圆弧中点外侧（viewBox 坐标 == donut 容器像素坐标，1:1）
  let tipStyle: CSSProperties | undefined;
  if (hoverIdx != null && arcs[hoverIdx]) {
    const a = arcs[hoverIdx]!;
    const mid = (a.a0 + a.a1) / 2;
    const tr = R + STROKE / 2 + 8;
    const tx = CX + tr * Math.cos(mid);
    const ty = CY + tr * Math.sin(mid);
    const dx = tx > SIZE * 0.6 ? "calc(-100% - 12px)" : "12px";
    const dy = ty < SIZE * 0.5 ? "8px" : "calc(-100% - 8px)";
    tipStyle = { left: tx, top: ty, transform: `translate(${dx}, ${dy})` };
  }

  return (
    <div className="usage-donut-wrap" onMouseLeave={() => setHoverIdx(null)}>
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
                strokeWidth={hoverIdx === i ? STROKE + 4 : STROKE}
                strokeLinecap="butt"
                opacity={hoverIdx == null || hoverIdx === i ? 1 : 0.45}
                style={{ cursor: "pointer", transition: "stroke-width .12s ease, opacity .12s ease" }}
                onMouseEnter={() => setHoverIdx(i)}
              />
            ) : null,
          )}
        </svg>
        <div className="usage-donut-center">
          <span className="usage-donut-value">{fmtTokens(centerValue)}</span>
          <span className="usage-donut-unit">tokens</span>
        </div>
        {hover && tipStyle && (
          <div className="usage-donut-tip" style={tipStyle}>
            <div className="usage-tip-date">{hover.label}</div>
            <div className="usage-tip-row">
              <span className="usage-tip-name">用量</span>
              <span className="usage-tip-val">{fmtTokens(hover.value)}</span>
            </div>
            <div className="usage-tip-row">
              <span className="usage-tip-name">占比</span>
              <span className="usage-tip-val">{hover.pct}%</span>
            </div>
            <div className="usage-tip-row">
              <span className="usage-tip-name">调用次数</span>
              <span className="usage-tip-val">{hover.calls.toLocaleString()} 次</span>
            </div>
            {hover.children && hover.children.length > 0 && (
              <>
                <div className="usage-tip-sep" />
                <div className="usage-tip-sub">包含模型（{hover.children.length} 个）</div>
                {hover.children.map((c) => (
                  <div key={c.label} className="usage-tip-row">
                    <i className="usage-trend-dot" style={{ background: c.color }} />
                    <span className="usage-tip-name" title={c.label}>{c.label}</span>
                    <span className="usage-tip-val">{fmtTokens(c.value)}</span>
                  </div>
                ))}
              </>
            )}
          </div>
        )}
      </div>
      <div className="usage-donut-legend">
        {segments.map((s, i) => (
          <div
            key={s.label}
            className={`usage-donut-legend-item${hoverIdx === i ? " active" : ""}`}
            onMouseEnter={() => setHoverIdx(i)}
          >
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
