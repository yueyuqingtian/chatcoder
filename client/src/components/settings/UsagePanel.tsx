/** 设置中心：用量统计（plan-152-704 重写，严格按图二图三设计）。
 * 自上而下：统计卡片行 → Token 活动热力图（每日/每周/累计）→ 时间范围（近7日/近30日/自定义）→
 * 每日 Token 趋势折线图（多模型）→ 模型用量环形图 → 刷新按钮。
 * 移除原"会话上下文占用"折叠区。模型显示"供应商/模型名"以区分同名模型。
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { api, type UsageStatsOut } from "../../api/client";
import { IconRefresh } from "../icons";
import { UsageHeatmap } from "./usage/UsageHeatmap";
import { UsageTrendChart, type TrendSeries } from "./usage/UsageTrendChart";
import { UsageDonutChart, type DonutSeg } from "./usage/UsageDonutChart";
import { fmtTokens, modelColor } from "./usage/chartUtils";

type RangeMode = "7" | "30" | "custom";

const RANGE_OPTS: Array<{ mode: RangeMode; label: string }> = [
  { mode: "7", label: "近 7 日" },
  { mode: "30", label: "近 30 日" },
  { mode: "custom", label: "自定义" },
];

function buildRangeParams(mode: RangeMode, start: string, end: string): { start?: string; end?: string; days?: number } {
  if (mode === "7") return { days: 7 };
  if (mode === "30") return { days: 30 };
  if (start && end) return { start, end };
  return { days: 30 };
}

export function UsagePanel() {
  const [stats, setStats] = useState<UsageStatsOut | null>(null);
  const [rangeMode, setRangeMode] = useState<RangeMode>("30");
  const [customStart, setCustomStart] = useState("");
  const [customEnd, setCustomEnd] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (mode: RangeMode, start: string, end: string) => {
    setLoading(true);
    setError(null);
    try {
      const s = await api.getUsageStats(buildRangeParams(mode, start, end));
      setStats(s);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load("30", "", ""); }, [load]);

  const applyRange = (mode: RangeMode, start = customStart, end = customEnd) => {
    if (mode === "custom" && (!start || !end)) return; // 起止未填全则不刷新
    setRangeMode(mode);
    void load(mode, start, end);
  };

  // ── 由 stats 派生的展示数据 ──
  const total = stats?.total;
  const byModel = stats?.by_model ?? [];
  const totalTokens = total?.total ?? 0;

  const trendSeries = useMemo<TrendSeries[]>(() => {
    if (!stats) return [];
    const dateKey = new Set(stats.daily.map((d) => d.date));
    const recordByKey: Record<string, Record<string, number>> = {};
    for (const d of stats.daily_by_model) {
      if (!dateKey.has(d.date)) continue;
      (recordByKey[d.key] ??= {})[d.date] = (recordByKey[d.key]?.[d.date] ?? 0) + d.tokens;
    }
    const top = byModel.slice(0, 5);
    const otherKeys = new Set(byModel.slice(5).map((b) => b.key));
    const otherRecord: Record<string, number> = {};
    for (const d of stats.daily_by_model) {
      if (otherKeys.has(d.key)) otherRecord[d.date] = (otherRecord[d.date] ?? 0) + d.tokens;
    }
    const res: TrendSeries[] = top.map((b, i) => ({
      display_name: b.display_name,
      color: modelColor(i),
      record: recordByKey[b.key] ?? {},
    }));
    if (otherKeys.size > 0) {
      res.push({ display_name: "其他", color: modelColor(top.length), record: otherRecord });
    }
    return res;
  }, [stats, byModel]);

  const donutSegs = useMemo<DonutSeg[]>(() => {
    const t = Math.max(1, totalTokens);
    const top8 = byModel.slice(0, 8);
    const rest = byModel.slice(8).reduce((s, b) => s + b.total, 0);
    const res: DonutSeg[] = top8.map((b, i) => ({
      label: b.display_name,
      value: b.total,
      color: modelColor(i),
      pct: Math.round((b.total / t) * 100),
    }));
    if (byModel.length > 8) {
      res.push({ label: "其他", value: rest, color: modelColor(8), pct: Math.round((rest / t) * 100) });
    }
    return res;
  }, [byModel, totalTokens]);

  const dates = stats?.daily.map((d) => d.date) ?? [];

  return (
    <div className="usage-panel">
      {/* 时间范围 + 刷新 */}
      <div className="usage-toolbar">
        <div className="usage-range">
          {RANGE_OPTS.map((o) => (
            <button
              key={o.mode}
              className={`btn btn-sm ${rangeMode === o.mode ? "btn-primary" : "btn-ghost"}`}
              disabled={loading}
              onClick={() => applyRange(o.mode)}
            >
              {o.label}
            </button>
          ))}
          {rangeMode === "custom" && (
            <span className="usage-range-custom">
              <input
                type="date"
                value={customStart}
                max={customEnd || undefined}
                onChange={(e) => setCustomStart(e.target.value)}
                onBlur={() => applyRange("custom")}
                className="sp-input usage-date-input"
              />
              <span className="usage-range-sep">至</span>
              <input
                type="date"
                value={customEnd}
                min={customStart || undefined}
                onChange={(e) => setCustomEnd(e.target.value)}
                onBlur={() => applyRange("custom")}
                className="sp-input usage-date-input"
              />
            </span>
          )}
        </div>
        <button className="btn btn-ghost btn-sm" onClick={() => applyRange(rangeMode)} disabled={loading}>
          <IconRefresh size={13} /> {loading ? "加载中…" : "刷新"}
        </button>
      </div>

      {error && <div className="navpage-empty" style={{ color: "var(--error)" }}>加载失败：{error}</div>}

      {/* 统计卡片行 */}
      {total && (
        <div className="usage-cards">
          <div className="usage-card">
            <div className="usage-card-value">{fmtTokens(total.total)}</div>
            <div className="usage-card-label">累计 Token</div>
            <div className="usage-card-sub">输入 {fmtTokens(total.prompt)} · 输出 {fmtTokens(total.completion)} · 缓存 {fmtTokens(total.cached)}</div>
          </div>
          <div className="usage-card">
            <div className="usage-card-value">{fmtTokens(stats?.peak_tokens ?? 0)}</div>
            <div className="usage-card-label">峰值 Token（单日最高）</div>
          </div>
          <div className="usage-card">
            <div className="usage-card-value">{total.calls.toLocaleString()}</div>
            <div className="usage-card-label">调用次数</div>
          </div>
          <div className="usage-card">
            <div className="usage-card-value">{stats?.streak_current ?? 0} 天</div>
            <div className="usage-card-label">当前连续天数</div>
          </div>
          <div className="usage-card">
            <div className="usage-card-value">{stats?.streak_longest ?? 0} 天</div>
            <div className="usage-card-label">最长连续天数</div>
          </div>
        </div>
      )}

      {/* Token 活动热力图 */}
      {stats && <UsageHeatmap daily_all={stats.daily_all} />}

      {/* 每日 Token 趋势 */}
      {trendSeries.length > 0 && (
        <div className="usage-section-block">
          <div className="usage-section-title">每日 Token 趋势</div>
          <UsageTrendChart series={trendSeries} dates={dates} />
        </div>
      )}

      {/* 模型用量环形图 */}
      {donutSegs.length > 0 && (
        <div className="usage-section-block">
          <div className="usage-section-title">模型用量</div>
          <UsageDonutChart segments={donutSegs} centerValue={totalTokens} />
        </div>
      )}

      {!loading && !error && !total && byModel.length === 0 && (
        <div className="navpage-empty">暂无用量数据，发送消息后统计</div>
      )}
    </div>
  );
}
