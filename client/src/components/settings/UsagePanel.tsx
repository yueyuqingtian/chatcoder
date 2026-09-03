/** 设置中心：用量统计（plan-152-704 重写，严格按图二图三设计）。
 * 自上而下：统计卡片行 → Token 活动热力图（每日/每周/累计）→ 时间范围（近7日/近30日/自定义）→
 * 每日 Token 趋势折线图（多模型）→ 模型用量环形图 → 刷新按钮。
 * 移除原"会话上下文占用"折叠区。模型显示"供应商/模型名"以区分同名模型。
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { api, type UsageStatsOut } from "../../api/client";
import { IconAlertTriangle, IconBarChart, IconCalendar, IconDatabase, IconHash, IconRefresh, IconZap } from "../icons";
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
    setRangeMode(mode); // 先切换模式（自定义时立即渲染起止输入框）
    if (mode === "custom") {
      if (start && end) void load("custom", start, end); // 填全才刷新
      return;
    }
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
    const res: DonutSeg[] = top8.map((b, i) => ({
      label: b.display_name,
      value: b.total,
      color: modelColor(i),
      pct: Math.round((b.total / t) * 100),
      calls: b.calls,
    }));
    if (byModel.length > 8) {
      const rest = byModel.slice(8);
      const restValue = rest.reduce((s, b) => s + b.total, 0);
      const restCalls = rest.reduce((s, b) => s + b.calls, 0);
      res.push({
        label: "其他",
        value: restValue,
        color: modelColor(8),
        pct: Math.round((restValue / t) * 100),
        calls: restCalls,
        children: rest.map((b, i) => ({
          label: b.display_name,
          value: b.total,
          color: modelColor(8 + i),
          pct: Math.round((b.total / t) * 100),
          calls: b.calls,
        })),
      });
    }
    return res;
  }, [byModel, totalTokens]);

  const dates = stats?.daily.map((d) => d.date) ?? [];

  return (
    <div className="usage-panel">
      {/* 时间范围 + 刷新 */}
      <div className="usage-toolbar">
        <div className="usage-range usage-seg-toggle">
          {RANGE_OPTS.map((o) => (
            <button
              key={o.mode}
              className={`usage-seg-btn ${rangeMode === o.mode ? "active" : ""}`}
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
        <button
          className="btn btn-ghost btn-sm"
          onClick={() => applyRange(rangeMode)}
          disabled={loading}
          title={rangeMode === "custom" && (!customStart || !customEnd) ? "请先选择起止日期" : undefined}
        >
          <IconRefresh size={13} /> {loading ? "加载中…" : "刷新"}
        </button>
      </div>

      {error && (
        <div className="usage-error">
          <IconAlertTriangle size={16} />
          <span>加载失败：{error}</span>
        </div>
      )}

      {/* 首屏加载骨架 */}
      {stats === null && loading && (
        <div className="usage-skeleton">
          <div className="usage-skeleton-cards">
            <div className="usage-skeleton-card" />
            <div className="usage-skeleton-card" />
            <div className="usage-skeleton-card" />
          </div>
          <div className="usage-skeleton-block" />
        </div>
      )}

      {/* 统计卡片行 */}
      {total && (
        <div className="usage-cards">
          <div className="usage-card usage-card-accent">
            <div className="usage-card-icon"><IconDatabase size={18} /></div>
            <div className="usage-card-value">{fmtTokens(total.total)}</div>
            <div className="usage-card-label">累计 Token</div>
            <div className="usage-card-sub">
              <span className="k">输入</span><span className="v">{fmtTokens(total.prompt)}</span>
              <span className="k">输出</span><span className="v">{fmtTokens(total.completion)}</span>
              <span className="k">缓存</span><span className="v">{fmtTokens(total.cached)}</span>
            </div>
          </div>
          <div className="usage-card">
            <div className="usage-card-icon"><IconZap size={18} /></div>
            <div className="usage-card-value">{fmtTokens(stats?.peak_tokens ?? 0)}</div>
            <div className="usage-card-label">峰值 Token（单日最高）</div>
          </div>
          <div className="usage-card">
            <div className="usage-card-icon"><IconHash size={18} /></div>
            <div className="usage-card-value">{total.calls.toLocaleString()}</div>
            <div className="usage-card-label">调用次数</div>
          </div>
          <div className="usage-card">
            <div className="usage-card-icon"><IconCalendar size={18} /></div>
            <div className="usage-card-value">{stats?.streak_current ?? 0} 天</div>
            <div className="usage-card-label">当前连续天数</div>
          </div>
          <div className="usage-card">
            <div className="usage-card-icon"><IconCalendar size={18} /></div>
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
          <div className="usage-block-head">
            <IconBarChart size={16} />
            <div className="usage-block-titles">
              <div className="usage-section-title">每日 Token 趋势</div>
              <div className="usage-block-sub">区间内按日汇总</div>
            </div>
          </div>
          <UsageTrendChart series={trendSeries} dates={dates} />
        </div>
      )}

      {/* 模型用量环形图 */}
      {donutSegs.length > 0 && (
        <div className="usage-section-block">
          <div className="usage-block-head">
            <IconDatabase size={16} />
            <div className="usage-block-titles">
              <div className="usage-section-title">模型用量</div>
              <div className="usage-block-sub">区间内模型分布</div>
            </div>
          </div>
          <UsageDonutChart segments={donutSegs} centerValue={totalTokens} />
        </div>
      )}

      {!loading && !error && !total && byModel.length === 0 && (
        <div className="usage-empty">
          <IconBarChart size={32} />
          <span>暂无用量数据，发送消息后统计</span>
        </div>
      )}
    </div>
  );
}
