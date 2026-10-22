/** Token 趋势图（纯 SVG，无依赖）。
 *
 * plan-20261022-release-ci 重做：
 *  · 多日区间画逐日折线，区间内没有用量的日子补 0（此前只画有数据的日期，空档被压没）；
 *    插值改为单调三次曲线，尖峰两侧不会再冲到 0 以下；点数不多时画出数据点，只有一天也看得见。
 *  · 单日区间（默认的「今天」）画 24 小时堆叠柱状图，按模型着色——此前只有一个点，整张图是空的。
 *  · 按容器实际宽度绘制（ResizeObserver），刻度文字始终 11px，不再随卡片变宽被等比放大。
 */
import { useEffect, useMemo, useRef, useState, type MouseEvent } from "react";
import { fmtTokens } from "./chartUtils";

export interface TrendSeries {
  display_name: string;
  color: string;
  /** x 轴键（日期 YYYY-MM-DD 或小时 "0"~"23"）→ tokens */
  record: Record<string, number>;
}

interface Props {
  series: TrendSeries[];
  /** x 轴键，按显示顺序排列 */
  keys: string[];
  kind: "line" | "bar";
  /** x 轴刻度文字 */
  tickLabel: (key: string) => string;
  /** 悬停浮层标题 */
  tipTitle: (key: string) => string;
}

const H = 220;
const PAD = { top: 12, right: 12, bottom: 26 };
/** 数据点不超过这个数时画出圆点（再多就只看曲线） */
const DOT_MAX_POINTS = 31;
/** x 轴刻度之间至少留这么宽（px） */
const TICK_MIN_GAP = 64;

/** 让 y 轴刻度落在 1 / 2 / 2.5 / 5 × 10^k 这类整数上 */
function niceMax(v: number): number {
  if (v <= 0) return 1;
  const step = v / 4;
  const mag = 10 ** Math.floor(Math.log10(step));
  const n = step / mag;
  const nice = n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10;
  return nice * mag * 4;
}

/** 单调三次插值（Fritsch–Carlson）：曲线平滑但不越过相邻点的取值范围 */
function monotonePath(pts: Array<[number, number]>): string {
  const n = pts.length;
  if (n === 0) return "";
  if (n < 3) return pts.map((p, i) => `${i ? "L" : "M"} ${p[0]},${p[1]}`).join(" ");
  const dx: number[] = [];
  const m: number[] = [];
  for (let i = 0; i < n - 1; i++) {
    dx[i] = pts[i + 1][0] - pts[i][0];
    m[i] = (pts[i + 1][1] - pts[i][1]) / (dx[i] || 1);
  }
  const t: number[] = new Array(n);
  t[0] = m[0];
  t[n - 1] = m[n - 2];
  for (let i = 1; i < n - 1; i++) t[i] = m[i - 1] * m[i] <= 0 ? 0 : (m[i - 1] + m[i]) / 2;
  for (let i = 0; i < n - 1; i++) {
    if (m[i] === 0) { t[i] = 0; t[i + 1] = 0; continue; }
    const a = t[i] / m[i];
    const b = t[i + 1] / m[i];
    const s = a * a + b * b;
    if (s > 9) {
      const k = 3 / Math.sqrt(s);
      t[i] = k * a * m[i];
      t[i + 1] = k * b * m[i];
    }
  }
  let d = `M ${pts[0][0]},${pts[0][1]}`;
  for (let i = 0; i < n - 1; i++) {
    const h = dx[i] / 3;
    d += ` C ${pts[i][0] + h},${pts[i][1] + t[i] * h} ${pts[i + 1][0] - h},${pts[i + 1][1] - t[i + 1] * h} ${pts[i + 1][0]},${pts[i + 1][1]}`;
  }
  return d;
}

/** 按宽度挑 x 轴刻度。
 *  折线（日期）：首尾必有，中间等距；柱状（24 小时）：按 1/2/3/4/6 小时取整，最多 8 个，不强加末尾刻度。 */
function pickTicks(n: number, innerW: number, kind: "line" | "bar"): number[] {
  if (n <= 0) return [];
  if (n === 1) return [0];
  const max = Math.max(2, Math.floor(innerW / TICK_MIN_GAP));
  if (kind === "bar") {
    const limit = Math.min(max, 8);
    const step = [1, 2, 3, 4, 6, 12].find((s) => Math.ceil(n / s) <= limit) ?? Math.ceil(n / limit);
    return Array.from({ length: Math.ceil(n / step) }, (_, i) => i * step);
  }
  if (n <= max) return Array.from({ length: n }, (_, i) => i);
  const step = Math.ceil((n - 1) / (max - 1));
  const out: number[] = [];
  for (let i = 0; i < n - 1; i += step) out.push(i);
  // 最后一个刻度离末尾太近时让位给末尾，避免两段文字挤在一起
  if (n - 1 - out[out.length - 1] < step / 2) out.pop();
  out.push(n - 1);
  return out;
}

export function UsageTrendChart({ series, keys, kind, tickLabel, tipTitle }: Props) {
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const [width, setWidth] = useState(760);
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);
  const [hoverLegend, setHoverLegend] = useState<string | null>(null);
  const [tipPos, setTipPos] = useState({ x: 0, y: 0 });

  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const measure = () => {
      const w = Math.round(el.getBoundingClientRect().width);
      if (w > 0) setWidth((prev) => (prev === w ? prev : w));
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const n = keys.length;
  /** 每个键的堆叠合计（柱状图的高度、悬停浮层的合计） */
  const totals = useMemo(
    () => keys.map((k) => series.reduce((s, it) => s + (it.record[k] || 0), 0)),
    [keys, series],
  );
  const rawMax = kind === "bar"
    ? Math.max(0, ...totals)
    : Math.max(0, ...series.flatMap((s) => keys.map((k) => s.record[k] || 0)));
  const maxY = niceMax(rawMax);
  const levels = [0, 0.25, 0.5, 0.75, 1];
  const tickText = levels.map((lv) => fmtTokens(Math.round(lv * maxY)));
  const padLeft = Math.max(36, ...tickText.map((t) => Math.ceil(t.length * 6.6) + 12));
  const innerW = Math.max(40, width - padLeft - PAD.right);
  const innerH = H - PAD.top - PAD.bottom;
  const band = n > 0 ? innerW / n : innerW;
  const xLine = (i: number) => padLeft + (n <= 1 ? innerW / 2 : (i / (n - 1)) * innerW);
  const xBand = (i: number) => padLeft + band * i;
  const xCenter = (i: number) => (kind === "bar" ? xBand(i) + band / 2 : xLine(i));
  const y = (v: number) => PAD.top + innerH - (v / maxY) * innerH;
  const barW = Math.max(2, Math.min(28, band * 0.62));
  const ticks = useMemo(() => pickTicks(n, innerW, kind), [n, innerW, kind]);

  const onMove = (e: MouseEvent<SVGSVGElement>) => {
    if (n === 0) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const px = e.clientX - rect.left;
    const i = kind === "bar"
      ? Math.floor((px - padLeft) / band)
      : Math.round(((px - padLeft) / innerW) * (n - 1));
    setHoverIdx(Math.max(0, Math.min(n - 1, n <= 1 ? 0 : i)));
    setTipPos({ x: px, y: e.clientY - rect.top });
  };

  const hoverKey = hoverIdx != null ? keys[hoverIdx] : null;
  const tipRight = tipPos.x > width * 0.6;

  return (
    <div className="usage-trend">
      <div className="usage-trend-legend" onMouseLeave={() => setHoverLegend(null)}>
        {series.map((s) => (
          <span
            key={s.display_name}
            className={"usage-trend-legend-item" + (hoverLegend && hoverLegend !== s.display_name ? " is-dim" : "")}
            onMouseEnter={() => setHoverLegend(s.display_name)}
          >
            <i className="usage-trend-dot" style={{ background: s.color }} />
            {s.display_name}
          </span>
        ))}
      </div>
      <div className="usage-trend-chart" ref={wrapRef}>
        <svg
          width={width}
          height={H}
          viewBox={`0 0 ${width} ${H}`}
          className="usage-trend-svg"
          onMouseMove={onMove}
          onMouseLeave={() => setHoverIdx(null)}
        >
          {levels.map((lv, li) => {
            const gy = y(lv * maxY);
            return (
              <g key={lv}>
                <line x1={padLeft} x2={width - PAD.right} y1={gy} y2={gy} className={"usage-trend-grid" + (lv === 0 ? " is-base" : "")} />
                <text x={padLeft - 8} y={gy + 4} className="usage-trend-ylabel" textAnchor="end">{tickText[li]}</text>
              </g>
            );
          })}
          {ticks.map((i) => (
            <text
              key={i}
              x={xCenter(i)}
              y={H - 8}
              className="usage-trend-xlabel"
              textAnchor={kind === "line" && n > 1 ? (i === 0 ? "start" : i === n - 1 ? "end" : "middle") : "middle"}
            >
              {tickLabel(keys[i])}
            </text>
          ))}

          {kind === "bar" && hoverIdx != null && (
            <rect x={xBand(hoverIdx)} y={PAD.top} width={band} height={innerH} className="usage-trend-band" />
          )}

          {kind === "bar"
            ? keys.map((k, i) => {
                let acc = 0;
                return (
                  <g key={k}>
                    {series.map((s) => {
                      const v = s.record[k] || 0;
                      if (v <= 0) return null;
                      const y0 = y(acc);
                      acc += v;
                      const y1 = y(acc);
                      const dimmed = hoverLegend != null && hoverLegend !== s.display_name;
                      return (
                        <rect
                          key={s.display_name}
                          x={xBand(i) + (band - barW) / 2}
                          y={y1}
                          width={barW}
                          height={Math.max(1, y0 - y1)}
                          fill={s.color}
                          className={"usage-trend-bar" + (dimmed ? " is-dim" : "")}
                        />
                      );
                    })}
                  </g>
                );
              })
            : series.map((s) => {
                const pts: Array<[number, number]> = keys.map((k, i) => [xLine(i), y(s.record[k] || 0)]);
                const dimmed = hoverLegend != null && hoverLegend !== s.display_name;
                const focus = hoverLegend === s.display_name;
                return (
                  <g key={s.display_name} className={"usage-trend-series" + (dimmed ? " is-dim" : "") + (focus ? " is-focus" : "")}>
                    {n > 1 && <path d={monotonePath(pts)} fill="none" stroke={s.color} className="usage-trend-line" />}
                    {n <= DOT_MAX_POINTS && keys.map((k, i) => (
                      (s.record[k] || 0) > 0
                        ? <circle key={k} cx={xLine(i)} cy={y(s.record[k])} r={n === 1 ? 4 : 2.5} fill={s.color} className="usage-trend-point" />
                        : null
                    ))}
                  </g>
                );
              })}

          {kind === "line" && hoverIdx != null && (
            <g>
              <line x1={xLine(hoverIdx)} x2={xLine(hoverIdx)} y1={PAD.top} y2={PAD.top + innerH} className="usage-trend-hover-line" />
              {series.map((s) => {
                const v = s.record[keys[hoverIdx]] || 0;
                return v > 0
                  ? <circle key={s.display_name} cx={xLine(hoverIdx)} cy={y(v)} r={4} fill={s.color} className="usage-trend-hover-dot" />
                  : null;
              })}
            </g>
          )}
        </svg>

        {hoverKey != null && (
          <div
            className="usage-trend-tip"
            style={{
              left: tipPos.x,
              top: tipPos.y,
              transform: tipRight ? "translate(calc(-100% - 12px), -50%)" : "translate(12px, -50%)",
            }}
          >
            <div className="usage-tip-date">{tipTitle(hoverKey)}</div>
            {series.map((s) => (
              <div key={s.display_name} className="usage-tip-row">
                <i className="usage-trend-dot" style={{ background: s.color }} />
                <span className="usage-tip-name">{s.display_name}</span>
                <span className="usage-tip-val">{fmtTokens(s.record[hoverKey] || 0)}</span>
              </div>
            ))}
            {series.length > 1 && (
              <>
                <div className="usage-tip-sep" />
                <div className="usage-tip-row usage-tip-total">
                  <span className="usage-tip-name">合计</span>
                  <span className="usage-tip-val">{fmtTokens(totals[hoverIdx ?? 0])}</span>
                </div>
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
