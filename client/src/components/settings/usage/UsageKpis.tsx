/** 使用统计 · KPI 卡片行（plan-20261021-usage-update；plan-20261022-release-ci 调整）。
 *
 * 六张卡：Token / 调用 / 历史单日峰值 / 连续使用 / 平均首字 / 平均速度，固定 3×2。
 * 图标底色按卡片着色（沿用图表色板），文字保持中性三层；前两张带迷你折线
 * （多日区间按日、单日区间按小时），一眼看出是平稳还是尖峰。
 * 无数据的指标显示「—」并写明原因，不隐藏卡片——旧后端没有性能字段时也保持六张，布局不随数据跳变。
 */
import type { ReactNode } from "react";
import type { UsageStatsOut } from "../../../api/client";
import { IconCalendar, IconClock, IconDatabase, IconGauge, IconHash, IconZap } from "../../icons";
import { fmtTokens } from "./chartUtils";
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

const pct = (part: number, whole: number) => (whole > 0 ? Math.round((part / whole) * 100) : 0);

/** 「9月18日」——峰值日期只看月日 */
function monthDay(iso: string | null): string {
  if (!iso) return "";
  const [, m, d] = iso.split("-").map(Number);
  return `${m}月${d}日`;
}

export interface UsageKpisProps {
  stats: UsageStatsOut;
  /** 区间口径的称呼：单日今天 =「今日」，其余 =「区间」 */
  scope: "今日" | "区间";
  /** 区间天数（日均按自然日计，不是只算有用量的日子） */
  rangeDays: number;
  tokenSpark: number[];
  callSpark: number[];
}

export function UsageKpis({ stats, scope, rangeDays, tokenSpark, callSpark }: UsageKpisProps) {
  const { total, perf } = stats;
  const peakDate = stats.daily_all.reduce<{ date: string | null; tokens: number }>(
    (best, d) => (d.tokens > best.tokens ? { date: d.date, tokens: d.tokens } : best),
    { date: null, tokens: 0 },
  ).date;
  const cacheRate = pct(total.cached, total.prompt);
  const noTiming = !perf || perf.avg_first_token_ms == null;
  const noSpeed = !perf || perf.avg_speed == null;

  return (
    <div className="up-kpis">
      <Kpi
        tone="blue" icon={<IconDatabase size={15} />} label={`${scope} Token`}
        value={fmtTokens(total.total)}
        foot={`输入 ${fmtTokens(total.prompt)}（缓存命中 ${cacheRate}%）· 输出 ${fmtTokens(total.completion)}`}
        spark={tokenSpark}
      />
      <Kpi
        tone="green" icon={<IconHash size={15} />} label={`${scope}调用`}
        value={total.calls.toLocaleString()}
        foot={`每次约 ${fmtTokens(total.calls ? Math.round(total.total / total.calls) : 0)} token`}
        spark={callSpark}
      />
      <Kpi
        tone="amber" icon={<IconZap size={15} />} label="历史单日峰值"
        value={fmtTokens(stats.peak_tokens ?? 0)}
        foot={rangeDays > 1
          ? `区间日均 ${fmtTokens(Math.round(total.total / rangeDays))}${peakDate ? ` · 峰值在 ${monthDay(peakDate)}` : ""}`
          : peakDate ? `出现在 ${monthDay(peakDate)}` : "还没有用量"}
      />
      <Kpi
        tone="violet" icon={<IconCalendar size={15} />} label="连续使用"
        value={stats.streak_current ?? 0} unit=" 天"
        foot={`最长 ${stats.streak_longest ?? 0} 天`}
      />
      <Kpi
        tone="sky" icon={<IconClock size={15} />} label="平均首字"
        value={noTiming ? "—" : fmtSec(perf!.avg_first_token_ms)}
        foot={noTiming ? "这个范围内的调用没有首字计时" : "请求发出到第一个字出现"}
      />
      <Kpi
        tone="rose" icon={<IconGauge size={15} />} label="平均速度"
        value={noSpeed ? "—" : fmtSpeed(perf!.avg_speed)} unit={noSpeed ? undefined : " token/s"}
        foot={noSpeed ? "需要输出不少于 100 token 的实测调用" : `基于 ${perf!.speed_calls.toLocaleString()} 次调用 · 从首字算到结束`}
      />
    </div>
  );
}
