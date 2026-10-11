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
import { UsageDateRangePicker, presetRange, type RangeMode } from "./usage/UsageDateRangePicker";
import { Select, Tooltip } from "../ui";

function buildRangeParams(mode: RangeMode, start: string, end: string, provider?: string): { start?: string; end?: string; days?: number; provider?: string } {
  // plan-308-1542 需求6：「今天」= 本地当日闭区间（days=1）
  if (mode === "today") return { days: 1, provider };
  if (mode === "7") return { days: 7, provider };
  if (mode === "30") return { days: 30, provider };
  // plan-91-393：「本月」按本地月初至今日的闭区间请求
  if (mode === "month") { const r = presetRange("month")!; return { start: r[0], end: r[1], provider }; }
  if (start && end) return { start, end, provider };
  return { days: 30, provider };
}

export function UsagePanel() {
  const [stats, setStats] = useState<UsageStatsOut | null>(null);
  // S14（plan-41-197）：默认「今天」——此前默认近 30 日，用户要求打开即看当日用量
  const [rangeMode, setRangeMode] = useState<RangeMode>("today");
  const [customStart, setCustomStart] = useState("");
  const [customEnd, setCustomEnd] = useState("");
  // plan-308-1542 需求6：按供应商筛选（空 = 全部供应商）
  const [provider, setProvider] = useState<string>("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (mode: RangeMode, start: string, end: string, prov?: string) => {
    setLoading(true);
    setError(null);
    try {
      const s = await api.getUsageStats(buildRangeParams(mode, start, end, prov ?? provider));
      setStats(s);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
    // provider 依赖：切换供应商后重新拉取（闭包读取最新值）
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [provider]);

  // plan-41-197：默认「今天」是既定需求，此前只在按钮高亮上生效，首屏却拉了近 30 日，
  // 这里让首次加载与默认选中项对齐（否则用户看到的数字口径与「今天」不符）。
  useEffect(() => { void load("today", "", ""); }, [load]);

  const applyRange = (mode: RangeMode, start = customStart, end = customEnd) => {
    setRangeMode(mode);
    if (mode === "custom") {
      setCustomStart(start);
      setCustomEnd(end);
      if (start && end) void load("custom", start, end); // 填全才刷新
      return;
    }
    void load(mode, start, end);
  };

  /** plan-308-1542 需求6：切换供应商（空字符串 = 全部供应商）。 */
  const applyProvider = (p: string) => {
    setProvider(p);
    void load(rangeMode, customStart, customEnd, p);
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
      {/* 时间范围 + 供应商 + 刷新（plan-91-393：日期改为浮层选择器） */}
      <div className="usage-toolbar sc-usage-toolbar">
        <UsageDateRangePicker
          mode={rangeMode}
          start={customStart}
          end={customEnd}
          disabled={loading}
          onApply={(m, s, e) => applyRange(m, s, e)}
        />
        {/* plan-308-1542 需求6：按供应商筛选（可切换查看对应供应商的使用情况） */}
        <Select
          size="sm"
          className="usage-provider-select"
          aria-label="按供应商筛选用量"
          value={provider}
          disabled={loading}
          onChange={applyProvider}
          options={[{ value: "", label: "全部供应商" }, ...(stats?.providers ?? []).map((p) => ({ value: p, label: p }))]}
        />
        <span className="sc-flex-spacer" />
        <Tooltip content={rangeMode === "custom" && (!customStart || !customEnd) ? "请先选择起止日期" : "重新加载统计"}>
          <button className="btn btn-ghost btn-sm" onClick={() => applyRange(rangeMode)} disabled={loading}>
            <span className={`sc-icon-slot ${loading ? "spin" : ""}`}><IconRefresh size={13} /></span>
            <span className="sc-fixed-label">{loading ? "加载中" : "刷新"}</span>
          </button>
        </Tooltip>
      </div>
      {provider && stats && stats.total.total === 0 && (
        <div className="navpage-empty">「{provider}」在所选区间无用量。</div>
      )}

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

      {/* KPI 卡片行（plan-91-393：大数字 + 标签 + 辅助对比行） */}
      {total && (
        <div className="sc-kpis">
          <div className="sc-kpi">
            <div className="sc-kpi-head"><IconDatabase size={14} /><span>区间 Token</span></div>
            <div className="sc-kpi-value">{fmtTokens(total.total)}</div>
            <div className="sc-kpi-delta">输入 {fmtTokens(total.prompt)} · 输出 {fmtTokens(total.completion)} · 缓存 {fmtTokens(total.cached)}</div>
          </div>
          <div className="sc-kpi">
            <div className="sc-kpi-head"><IconZap size={14} /><span>历史单日峰值</span></div>
            <div className="sc-kpi-value">{fmtTokens(stats?.peak_tokens ?? 0)}</div>
            {/* plan-91-393：峰值取全部历史（与热力图同源），与左侧「区间」口径不同，文案需写明避免误读 */}
            <div className="sc-kpi-delta">区间日均 {fmtTokens(dates.length ? Math.round(total.total / dates.length) : 0)}</div>
          </div>
          <div className="sc-kpi">
            <div className="sc-kpi-head"><IconHash size={14} /><span>区间调用</span></div>
            <div className="sc-kpi-value">{total.calls.toLocaleString()}</div>
            <div className="sc-kpi-delta">每次约 {fmtTokens(total.calls ? Math.round(total.total / total.calls) : 0)}</div>
          </div>
          <div className="sc-kpi">
            <div className="sc-kpi-head"><IconCalendar size={14} /><span>连续使用</span></div>
            <div className="sc-kpi-value">{stats?.streak_current ?? 0}<small> 天</small></div>
            <div className="sc-kpi-delta">最长 {stats?.streak_longest ?? 0} 天</div>
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

      {/* 模型/供应商明细表（plan-91-393：等宽数字 + 占比条） */}
      {byModel.length > 0 && (
        <div className="usage-section-block">
          <div className="usage-block-head">
            <IconHash size={16} />
            <div className="usage-block-titles">
              <div className="usage-section-title">模型明细</div>
              <div className="usage-block-sub">按 Token 用量排序</div>
            </div>
          </div>
          <div className="sc-table" role="table">
            <div className="sc-tr sc-th" role="row">
              <span>模型</span><span>输入</span><span>输出</span><span>缓存</span><span>调用</span><span>合计</span>
            </div>
            {byModel.map((b, i) => {
              const pct = totalTokens ? (b.total / totalTokens) * 100 : 0;
              return (
                <div className="sc-tr" role="row" key={b.key}>
                  <span className="sc-td-name">
                    <i className="sc-dot" style={{ background: modelColor(Math.min(i, 8)) }} />
                    <span className="sc-ellipsis">{b.display_name}</span>
                  </span>
                  <span>{fmtTokens(b.prompt)}</span>
                  <span>{fmtTokens(b.completion)}</span>
                  <span>{fmtTokens(b.cached)}</span>
                  <span>{b.calls.toLocaleString()}</span>
                  <span className="sc-td-total">
                    <span>{fmtTokens(b.total)}</span>
                    <span className="sc-bar"><i style={{ width: `${Math.max(2, pct)}%` }} /></span>
                    <span className="sc-pct">{pct.toFixed(1)}%</span>
                  </span>
                </div>
              );
            })}
          </div>
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
