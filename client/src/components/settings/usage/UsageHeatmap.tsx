/** Token 活动热力图：GitHub 风格贡献图，支持 每日/每周/累计 三种粒度。纯 SVG/div，无依赖。
 *  plan-20261022-release-ci：粒度切换改用 ui/Segmented（不再用页面私有按钮）；去掉格子上的原生 title
 *  （与自绘浮层重复，悬停时会出现两层提示）。热力图是全历史口径，不随上方时间范围变化，副标题写明。 */
import { useMemo, useRef, useState, type CSSProperties, type MouseEvent } from "react";
import { addDays, parseDate, toISO, fmtTokens } from "./chartUtils";
import { IconLayoutGrid } from "../../icons";
import { Segmented } from "../../ui";

type Mode = "daily" | "weekly" | "cum";

interface HmPoint { date: string; tokens: number; }

interface HmTip { x: number; y: number; title: string; sub: string; }

const WEEKDAYS = ["日", "一", "二", "三", "四", "五", "六"];

function tier(value: number, max: number): number {
  if (value <= 0) return 0;
  const r = value / (max || 1);
  if (r <= 0.25) return 1;
  if (r <= 0.5) return 2;
  if (r <= 0.75) return 3;
  return 4;
}

export function UsageHeatmap({ daily_all }: { daily_all: HmPoint[] }) {
  const [mode, setMode] = useState<Mode>("daily");
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const [tip, setTip] = useState<HmTip | null>(null);

  const showTip = (e: MouseEvent<HTMLElement>, title: string, sub: string) => {
    const wrap = wrapRef.current;
    const el = e.currentTarget;
    if (!wrap) return;
    const wr = wrap.getBoundingClientRect();
    const er = el.getBoundingClientRect();
    setTip({ x: er.left - wr.left + er.width / 2, y: er.top - wr.top, title, sub });
  };
  const hideTip = () => setTip(null);

  const model = useMemo(() => {
    const byDate: Record<string, number> = {};
    for (const p of daily_all) byDate[p.date] = p.tokens;

    const today = new Date();
    // 窗口起点：today 往前 364 天，回退到其所在周的周日
    let start = addDays(today, -364);
    while (start.getDay() !== 0) start = addDays(start, -1);

    // 构建周列（每列 7 天，日→六）
    const weeks: Array<Array<{ date: string; tokens: number } | null>> = [];
    let cur: Array<{ date: string; tokens: number } | null> = [];
    for (let d = new Date(start); d <= today; d = addDays(d, 1)) {
      const iso = toISO(d);
      cur.push({ date: iso, tokens: byDate[iso] || 0 });
      if (cur.length === 7) {
        weeks.push(cur);
        cur = [];
      }
    }
    if (cur.length) {
      // 末尾不足一周补空
      while (cur.length < 7) cur.push(null);
      weeks.push(cur);
    }

    // 每周合计
    const weekTotal = weeks.map((w) =>
      w.reduce((s, c) => s + (c ? c.tokens : 0), 0),
    );

    // 月度标签：每周首次出现新月份时标记该列
    let lastMonth = -1;
    const months: Array<{ ci: number; label: string }> = [];
    weeks.forEach((w, ci) => {
      const first = w.find(Boolean);
      if (!first) return;
      const d = parseDate(first.date);
      const key = d.getFullYear() * 100 + d.getMonth();
      if (key !== lastMonth) {
        months.push({ ci, label: `${d.getMonth() + 1}月` });
        lastMonth = key;
      }
    });

    // 累计值
    let running = 0;
    const cumByDate: Record<string, number> = {};
    for (const w of weeks) {
      for (const c of w) {
        if (c) {
          running += c.tokens;
          cumByDate[c.date] = running;
        }
      }
    }

    return { weeks, weekTotal, months, byDate, cumByDate };
  }, [daily_all]);

  // plan-20261021-usage-update：标题旁补「活跃天数」，让热力图不只是一片色块
  const activeDays = useMemo(() => daily_all.filter((p) => p.tokens > 0).length, [daily_all]);

  const maxValue = useMemo(() => {
    if (mode === "weekly") return Math.max(0, ...model.weekTotal);
    if (mode === "cum") {
      return Math.max(0, ...daily_all.map((p) => model.cumByDate[p.date] || 0));
    }
    return Math.max(0, ...daily_all.map((p) => p.tokens));
  }, [mode, model, daily_all]);

  function valueOf(date: string): number {
    if (mode === "cum") return model.cumByDate[date] || 0;
    return model.byDate[date] || 0;
  }

  // plan-20261021-usage-heatmap-fill：列宽不再写死 12px，改为按容器宽度等分铺满整行，
  // 行高由格子的 1:1 比例撑出（见 .usage-hm-grid）；列数固定，月份标签按列号百分比定位。
  const colCount = Math.max(1, model.weeks.length);
  const gridStyle: CSSProperties = {
    gridTemplateColumns: `repeat(${colCount}, minmax(0, 1fr))`,
  };

  return (
    <div className="usage-heatmap" ref={wrapRef} onMouseLeave={hideTip}>
      <div className="usage-heatmap-head">
        <div className="usage-block-head">
          <IconLayoutGrid size={16} />
          <div className="usage-block-titles">
            <span className="usage-heatmap-title">Token 活动</span>
            <span className="usage-block-sub">近 12 个月 · 活跃 {activeDays} 天 · 不随时间范围变化</span>
          </div>
        </div>
        <Segmented<Mode>
          size="sm"
          aria-label="热力图粒度"
          value={mode}
          onChange={setMode}
          options={[
            { value: "daily", label: "每日" },
            { value: "weekly", label: "每周" },
            { value: "cum", label: "累计" },
          ]}
        />
      </div>

      <div className="usage-heatmap-body">
        {mode !== "weekly" && (
          <div className="usage-heatmap-weekdays" aria-hidden>
            {WEEKDAYS.map((w) => (
              <span key={w}>{w}</span>
            ))}
          </div>
        )}

        <div className="usage-heatmap-main">
          <div className="usage-heatmap-months">
            {model.months.map((m) => (
              <span
                key={`${m.ci}-${m.label}`}
                className="usage-hm-month"
                style={{ left: `${(m.ci / colCount) * 100}%` }}
              >
                {m.label}
              </span>
            ))}
          </div>

          {mode === "weekly" ? (
            <div className="usage-hm-grid is-weekly" style={gridStyle}>
              {model.weekTotal.map((v, ci) => (
                <div
                  key={ci}
                  className={`usage-hm-cell usage-hm-${tier(v, maxValue)}`}
                  onMouseEnter={(e) => showTip(e, `第 ${ci + 1} 周`, `${fmtTokens(v)} tokens`)}
                />
              ))}
            </div>
          ) : (
            <div className="usage-hm-grid" style={gridStyle}>
              {model.weeks.flatMap((w, ci) =>
                w.map((c, ri) => {
                  if (!c) return <div key={`${ci}-${ri}`} className="usage-hm-cell usage-hm-0" />;
                  return (
                    <div
                      key={`${ci}-${ri}`}
                      className={`usage-hm-cell usage-hm-${tier(valueOf(c.date), maxValue)}`}
                      onMouseEnter={(e) => showTip(e, c.date, `${fmtTokens(valueOf(c.date))} tokens`)}
                    />
                  );
                }),
              )}
            </div>
          )}
        </div>
      </div>
      {/* 图例：色阶由浅到深 = 用量由少到多 */}
      <div className="usage-hm-legend" aria-hidden>
        <span>少</span>
        {[0, 1, 2, 3, 4].map((t) => <i key={t} className={`usage-hm-cell usage-hm-${t}`} />)}
        <span>多</span>
      </div>
      {tip && (
        <div
          className="usage-hm-tip"
          style={{
            left: tip.x,
            top: tip.y,
            transform: tip.y < 44 ? "translate(-50%, 12px)" : "translate(-50%, calc(-100% - 6px))",
          }}
        >
          <div className="usage-tip-date">{tip.title}</div>
          <div className="usage-tip-row">
            <span className="usage-tip-name">tokens</span>
            <span className="usage-tip-val">{tip.sub}</span>
          </div>
        </div>
      )}
    </div>
  );
}
