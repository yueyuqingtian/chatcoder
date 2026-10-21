/** 用量统计 · KPI 卡片行（plan-20261021-usage-update）。
 *
 * 六张卡：区间 Token / 区间调用 / 历史单日峰值 / 连续使用 / 平均首字 / 平均速度。
 * 图标底色按卡片着色（沿用图表色板），文字保持中性三层；前两张带逐日迷你折线，
 * 一眼看出区间内是平稳还是尖峰。无数据的指标显示「—」并写明原因，不隐藏卡片（避免布局随数据跳变）。
 */
import { useMemo, type ReactNode } from "react";
import type { UsageStatsOut } from "../../../api/client";
import { IconCalendar, IconClock, IconDatabase, IconGauge, IconHash, IconZap } from "../../icons";
import { addDays, fmtTokens, parseDate, toISO } from "./chartUtils";
import { fmtSec, fmtSpeed } from "./format";

type Tone = "blue" | "green" | "amber" | "violet" | "sky" | "rose";

/** 迷你折线：纯 SVG，单色描边 + 同色浅填充（设计语言无渐变）。少于 2 个点不画。 */
function Sparkline({ values }: { values: number[] }) {
  const W = 88;
  const H = 30;
  const PAD = 2;
  if (values.length < 2) return null;
  const max = Math.max(1, ...values);
  const x = (i: number) => PAD + (i / (values.length - 1)) * (W - PAD * 2);
  const y = (v: number) => H - PAD - (v / max) * (H - PAD * 2);
  const line = values.map((v, i) => `${i === 0 ? "M" : "L"} ${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const area = `${line} L ${x(values.length - 1).toFixed(1)},${H} L ${x(0).toFixed(1)},${H} Z`;
  return (
    <svg className="up-spark" viewBox={`0 0 ${W} ${H}`} width={W} height={H} aria-hidden>
      <path d={area} className="up-spark-area" />
      <path d={line} className="up-spark-line" />
    </svg>
  );
}

function Kpi({ tone, icon, label, value, unit, foot, spark }: {
  tone: Tone;
  icon: ReactNode;
  label: string;
  value: ReactNode;
  unit?: string;
  foot: ReactNode;
  spark?: number[];
}) {
  return (
    <div className={`up-kpi tone-${tone}`}>
      <div className="up-kpi-head">
        <span className="up-kpi-ic" aria-hidden>{icon}</span>
        <span className="up-kpi-label">{label}</span>
      </div>
      <div className="up-kpi-main">
        <div className="up-kpi-value">{value}{unit && <small>{unit}</small>}</div>
        {spark && <Sparkline values={spark} />}
      </div>
      <div className="up-kpi-foot">{foot}</div>
    </div>
  );
}

/** 把「只含有数据的日子」补成连续日期序列（缺的日子记 0），迷你折线才不会把空档压扁 */
function fillDays(daily: UsageStatsOut["daily"], pick: (d: UsageStatsOut["daily"][number]) => number): number[] {
  if (daily.length === 0) return [];
  const byDate = new Map(daily.map((d) => [d.date, pick(d)]));
  const first = parseDate(daily[0].date);
  const last = parseDate(daily[daily.length - 1].date);
  const out: number[] = [];
  for (let d = first; d <= last; d = addDays(d, 1)) out.push(byDate.get(toISO(d)) ?? 0);
  return out;
}

export function UsageKpis({ stats }: { stats: UsageStatsOut }) {
  const { total, perf } = stats;
  const days = stats.daily.length;

  const tokenSpark = useMemo(() => fillDays(stats.daily, (d) => d.tokens), [stats.daily]);
  const callSpark = useMemo(() => fillDays(stats.daily, (d) => d.calls), [stats.daily]);

  const noTiming = !perf || perf.avg_first_token_ms == null;
  return (
    <div className="up-kpis">
      <Kpi
        tone="blue" icon={<IconDatabase size={15} />} label="区间 Token"
        value={fmtTokens(total.total)}
        foot={`输入 ${fmtTokens(total.prompt)} · 输出 ${fmtTokens(total.completion)} · 缓存 ${fmtTokens(total.cached)}`}
        spark={tokenSpark}
      />
      <Kpi
        tone="green" icon={<IconHash size={15} />} label="区间调用"
        value={total.calls.toLocaleString()}
        foot={`每次约 ${fmtTokens(total.calls ? Math.round(total.total / total.calls) : 0)} token`}
        spark={callSpark}
      />
      <Kpi
        tone="amber" icon={<IconZap size={15} />} label="历史单日峰值"
        value={fmtTokens(stats.peak_tokens ?? 0)}
        foot={`区间日均 ${fmtTokens(days ? Math.round(total.total / days) : 0)}`}
      />
      <Kpi
        tone="violet" icon={<IconCalendar size={15} />} label="连续使用"
        value={stats.streak_current ?? 0} unit=" 天"
        foot={`最长 ${stats.streak_longest ?? 0} 天`}
      />
      {perf && (
        <>
          <Kpi
            tone="sky" icon={<IconClock size={15} />} label="平均首字"
            value={noTiming ? "—" : fmtSec(perf.avg_first_token_ms)}
            foot={noTiming ? "这个范围内的调用没有首字计时" : "请求发出到第一个字出现"}
          />
          <Kpi
            tone="rose" icon={<IconGauge size={15} />} label="平均速度"
            value={perf.avg_speed == null ? "—" : fmtSpeed(perf.avg_speed)} unit={perf.avg_speed == null ? undefined : " token/s"}
            foot={perf.avg_speed == null ? "需要输出不少于 100 token 的实测调用" : `基于 ${perf.speed_calls.toLocaleString()} 次调用 · 从首字算到结束`}
          />
        </>
      )}
    </div>
  );
}
