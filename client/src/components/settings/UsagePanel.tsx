/** 设置中心：使用统计（plan-152-704 重写；plan-20261021-usage-update 再强化）。
 *
 * 两个视图，共用同一条筛选栏（时间范围 / 供应商 / 刷新），切换视图不丢筛选：
 *  - 概览：KPI 卡片 → 趋势 → 模型分布（环形图 + 可排序明细）→ Token 活动热力图；
 *  - 调用记录：逐次调用的时间 / 供应商 / 账户 / 模型 / token / 首字 / 耗时 / 速度，后端分页，每页 20 条。
 * 模型显示「供应商/模型名」以区分同名模型。
 *
 * plan-20261022-release-ci：跟随筛选的卡片排在一起，不随筛选变化的全历史热力图挪到最后；
 * 单日区间的趋势改为 24 小时分布（此前「今天」只有一个点，整张图是空的），多日区间补齐无用量的日子；
 * 环形图与模型明细合并成一张卡，明细表兼作图例。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, type UsageRecordsOut, type UsageStatsOut } from "../../api/client";
import { IconAlertTriangle, IconBarChart, IconDatabase, IconListOrdered, IconRefresh } from "../icons";
import { UsageHeatmap } from "./usage/UsageHeatmap";
import { UsageTrendChart, type TrendSeries } from "./usage/UsageTrendChart";
import { UsageDonutChart, type DonutSeg } from "./usage/UsageDonutChart";
import { UsageKpis } from "./usage/UsageKpis";
import { UsageModelTable } from "./usage/UsageModelTable";
import { UsageRecordsTable } from "./usage/UsageRecordsTable";
import { addDays, modelColor, parseDate, toISO } from "./usage/chartUtils";
import { UsageDateRangePicker, presetRange, ymd, type RangeMode } from "./usage/UsageDateRangePicker";
import { Button, Empty, Segmented, Select, Tooltip } from "../ui";

const WEEKDAYS = ["日", "一", "二", "三", "四", "五", "六"];

/** 区间内的全部日期（含没有用量的日子）；旧后端没有 range 字段时退回「有数据的日期」 */
function rangeDates(stats: UsageStatsOut): string[] {
  if (!stats.range) return stats.daily.map((d) => d.date);
  const out: string[] = [];
  for (let d = parseDate(stats.range.start); toISO(d) <= stats.range.end; d = addDays(d, 1)) out.push(toISO(d));
  return out;
}

/** 按模型拆成折线 / 柱状序列：用量前 5 的模型各一条，其余合并为「其他」 */
function buildSeries(
  byModel: UsageStatsOut["by_model"],
  points: Array<{ key: string; bucket: string; tokens: number }>,
  buckets: Set<string>,
): TrendSeries[] {
  const recordByKey: Record<string, Record<string, number>> = {};
  for (const p of points) {
    if (!buckets.has(p.bucket)) continue;
    const rec = (recordByKey[p.key] ??= {});
    rec[p.bucket] = (rec[p.bucket] ?? 0) + p.tokens;
  }
  const top = byModel.slice(0, 5);
  const otherKeys = new Set(byModel.slice(5).map((b) => b.key));
  const otherRecord: Record<string, number> = {};
  for (const p of points) {
    if (otherKeys.has(p.key) && buckets.has(p.bucket)) otherRecord[p.bucket] = (otherRecord[p.bucket] ?? 0) + p.tokens;
  }
  const res: TrendSeries[] = top.map((b, i) => ({ display_name: b.display_name, color: modelColor(i), record: recordByKey[b.key] ?? {} }));
  if (otherKeys.size > 0) res.push({ display_name: "其他", color: modelColor(top.length), record: otherRecord });
  return res;
}

const dayTick = (iso: string) => { const [, m, d] = iso.split("-").map(Number); return `${m}月${d}日`; };
const dayTip = (iso: string) => {
  const [y, m, d] = iso.split("-").map(Number);
  return `${m}月${d}日 周${WEEKDAYS[new Date(y, m - 1, d).getDay()]}`;
};
const hourTick = (h: string) => `${h}时`;
const hourTip = (h: string) => `${h.padStart(2, "0")}:00 – ${String(Number(h) + 1).padStart(2, "0")}:00`;

/** 调用记录每页条数（与后端默认一致） */
const RECORDS_PAGE_SIZE = 20;

type UsageTab = "overview" | "records";

interface Filters {
  mode: RangeMode;
  start: string;
  end: string;
  /** 空 = 全部供应商 */
  provider: string;
}

function buildRangeParams(f: Filters): { start?: string; end?: string; days?: number; provider?: string } {
  const provider = f.provider || undefined;
  // plan-308-1542 需求6：「今天」= 本地当日闭区间（days=1）
  if (f.mode === "today") return { days: 1, provider };
  if (f.mode === "7") return { days: 7, provider };
  if (f.mode === "30") return { days: 30, provider };
  // plan-91-393：「本月」按本地月初至今日的闭区间请求
  if (f.mode === "month") { const r = presetRange("month")!; return { start: r[0], end: r[1], provider }; }
  if (f.start && f.end) return { start: f.start, end: f.end, provider };
  return { days: 30, provider };
}

export function UsagePanel() {
  const [tab, setTab] = useState<UsageTab>("overview");
  // S14（plan-41-197）：默认「今天」——用户要求打开即看当日用量
  const [filters, setFilters] = useState<Filters>({ mode: "today", start: "", end: "", provider: "" });
  /** 刷新按钮：递增即重新拉取当前视图 */
  const [tick, setTick] = useState(0);

  const [stats, setStats] = useState<UsageStatsOut | null>(null);
  const [statsLoading, setStatsLoading] = useState(false);
  const [statsError, setStatsError] = useState<string | null>(null);
  const statsSeq = useRef(0);

  const [page, setPage] = useState(1);
  const [records, setRecords] = useState<UsageRecordsOut | null>(null);
  const [recLoading, setRecLoading] = useState(false);
  const [recError, setRecError] = useState<string | null>(null);
  const recSeq = useRef(0);

  // 自定义区间只填了一半时不请求（选择器填全才会回调，这里是兜底）
  const rangeReady = filters.mode !== "custom" || (!!filters.start && !!filters.end);
  const rangeParams = useMemo(() => buildRangeParams(filters), [filters]);

  // ── 概览数据：筛选或刷新变化时拉取；用序号丢弃过期响应，快速连点不会让旧结果覆盖新结果 ──
  useEffect(() => {
    if (!rangeReady) return;
    const seq = ++statsSeq.current;
    setStatsLoading(true);
    setStatsError(null);
    api.getUsageStats(rangeParams)
      .then((s) => { if (seq === statsSeq.current) setStats(s); })
      .catch((e) => { if (seq === statsSeq.current) setStatsError(String(e)); })
      .finally(() => { if (seq === statsSeq.current) setStatsLoading(false); });
  }, [rangeParams, rangeReady, tick]);

  // ── 调用记录：只在「调用记录」视图下拉取；翻页 / 筛选 / 刷新都会触发 ──
  useEffect(() => {
    if (tab !== "records" || !rangeReady) return;
    const seq = ++recSeq.current;
    setRecLoading(true);
    setRecError(null);
    api.getUsageRecords({ ...rangeParams, page, page_size: RECORDS_PAGE_SIZE })
      .then((r) => { if (seq === recSeq.current) setRecords(r); })
      .catch((e) => { if (seq === recSeq.current) setRecError(String(e)); })
      .finally(() => { if (seq === recSeq.current) setRecLoading(false); });
  }, [tab, rangeParams, rangeReady, page, tick]);

  const applyFilters = useCallback((patch: Partial<Filters>) => {
    setFilters((f) => ({ ...f, ...patch }));
    setPage(1); // 换了范围 / 供应商，回到第一页
  }, []);

  const loading = tab === "records" ? recLoading : statsLoading;

  // ── 由 stats 派生的展示数据 ──
  const total = stats?.total;
  const byModel = useMemo(() => stats?.by_model ?? [], [stats]);
  const totalTokens = total?.total ?? 0;

  // ── 趋势：单日区间按小时（后端只在单日区间返回 hourly），多日区间按日并补齐空档 ──
  const trend = useMemo(() => {
    if (!stats) return null;
    const hourly = stats.hourly ?? [];
    if (hourly.length === 24) {
      // 今天：只画到当前小时，后面还没发生的小时不画成 0（迷你折线结尾不会掉到底）
      const isToday = stats.range?.end === ymd(new Date());
      const lastHour = isToday ? new Date().getHours() : 23;
      const keys = hourly.map((h) => String(h.hour));
      const points = (stats.hourly_by_model ?? []).map((p) => ({ key: p.key, bucket: String(p.hour), tokens: p.tokens }));
      const upToNow = hourly.filter((h) => h.hour <= lastHour);
      return {
        kind: "bar" as const,
        keys,
        series: buildSeries(byModel, points, new Set(keys)),
        tokenSpark: upToNow.map((h) => h.tokens),
        callSpark: upToNow.map((h) => h.calls),
        days: 1,
        isToday,
      };
    }
    const keys = rangeDates(stats);
    const byDate = new Map(stats.daily.map((d) => [d.date, d]));
    const points = stats.daily_by_model.map((p) => ({ key: p.key, bucket: p.date, tokens: p.tokens }));
    return {
      kind: "line" as const,
      keys,
      series: buildSeries(byModel, points, new Set(keys)),
      tokenSpark: keys.map((k) => byDate.get(k)?.tokens ?? 0),
      callSpark: keys.map((k) => byDate.get(k)?.calls ?? 0),
      days: Math.max(1, keys.length),
      isToday: keys.length === 1 && keys[0] === ymd(new Date()),
    };
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

  // 当前选中的供应商即使不在本区间的列表里也要保留为可选项，否则下拉框会显示成空白
  const providerOptions = useMemo(() => {
    const list = stats?.providers ?? [];
    const opts = [{ value: "", label: "全部供应商" }, ...list.map((p) => ({ value: p, label: p }))];
    if (filters.provider && !list.includes(filters.provider)) opts.push({ value: filters.provider, label: filters.provider });
    return opts;
  }, [stats, filters.provider]);

  const empty = !!stats && stats.total.calls === 0;

  return (
    <div className="up">
      {/* 筛选栏：视图切换 + 时间范围 + 供应商 + 刷新（两个视图共用） */}
      <div className="up-bar">
        <Segmented<UsageTab>
          size="sm"
          aria-label="用量视图"
          value={tab}
          onChange={setTab}
          options={[
            { value: "overview", label: "概览", icon: <IconBarChart size={14} /> },
            { value: "records", label: "调用记录", icon: <IconListOrdered size={14} />, count: stats?.total.calls },
          ]}
        />
        <span className="up-bar-spacer" />
        <UsageDateRangePicker
          mode={filters.mode}
          start={filters.start}
          end={filters.end}
          disabled={loading}
          onApply={(m, s, e) => applyFilters({ mode: m, start: s, end: e })}
        />
        {/* plan-308-1542 需求6：按供应商筛选（可切换查看对应供应商的使用情况） */}
        <Select
          size="sm"
          className="usage-provider-select"
          aria-label="按供应商筛选用量"
          value={filters.provider}
          disabled={loading}
          onChange={(p) => applyFilters({ provider: p })}
          options={providerOptions}
        />
        <Tooltip content={filters.mode === "custom" && !rangeReady ? "请先选择起止日期" : "重新加载统计"}>
          <Button variant="ghost" size="sm" onClick={() => setTick((n) => n + 1)} disabled={loading || !rangeReady}>
            <span className={`sc-icon-slot ${loading ? "spin" : ""}`}><IconRefresh size={13} /></span>
            <span className="sc-fixed-label">{loading ? "加载中" : "刷新"}</span>
          </Button>
        </Tooltip>
      </div>

      {/* ── 调用记录视图 ── */}
      {tab === "records" && (
        <div className="up-view" key="records" role="tabpanel">
          <UsageRecordsTable
            data={records}
            loading={recLoading}
            error={recError}
            page={page}
            pageSize={RECORDS_PAGE_SIZE}
            onPage={setPage}
            onRetry={() => setTick((n) => n + 1)}
          />
        </div>
      )}

      {/* ── 概览视图 ── */}
      {tab === "overview" && (
        <div className="up-view" key="overview" role="tabpanel">
          {statsError && (
            <div className="usage-error">
              <IconAlertTriangle size={16} />
              <span>加载失败：{statsError}</span>
              <Button variant="ghost" size="xs" icon={<IconRefresh size={12} />} onClick={() => setTick((n) => n + 1)}>重试</Button>
            </div>
          )}

          {/* 首屏加载骨架 */}
          {stats === null && statsLoading && (
            <div className="usage-skeleton">
              <div className="usage-skeleton-cards">
                {Array.from({ length: 6 }, (_, i) => <div key={i} className="usage-skeleton-card" />)}
              </div>
              <div className="usage-skeleton-block" />
            </div>
          )}

          {stats && trend && (
            <div className={"up-stack" + (statsLoading ? " is-loading" : "")}>
              <UsageKpis
                stats={stats}
                scope={trend.isToday ? "今日" : "区间"}
                rangeDays={trend.days}
                tokenSpark={trend.tokenSpark}
                callSpark={trend.callSpark}
              />

              {empty ? (
                <div className="up-card">
                  <Empty
                    size="md"
                    title={filters.provider ? `「${filters.provider}」在所选区间没有用量` : "所选区间还没有用量"}
                    desc="换一个时间范围，或发送消息后再来看看"
                  />
                </div>
              ) : (
                <>
                  <section className="up-card">
                    <header className="up-card-head">
                      <IconBarChart size={16} />
                      <div className="up-card-titles">
                        <div className="up-card-title">{trend.kind === "bar" ? "每小时 Token" : "每日 Token 趋势"}</div>
                        <div className="up-card-sub">
                          {trend.kind === "bar"
                            ? `${trend.isToday ? "今天" : stats.range?.start ?? ""}按小时汇总（本地时间），按模型堆叠`
                            : `${trend.days} 天 · 没有用量的日子记为 0`}
                        </div>
                      </div>
                    </header>
                    <div className="up-card-body">
                      <UsageTrendChart
                        series={trend.series}
                        keys={trend.keys}
                        kind={trend.kind}
                        tickLabel={trend.kind === "bar" ? hourTick : dayTick}
                        tipTitle={trend.kind === "bar" ? hourTip : dayTip}
                      />
                    </div>
                  </section>

                  {byModel.length > 0 && (
                    <section className="up-card">
                      <header className="up-card-head">
                        <IconDatabase size={16} />
                        <div className="up-card-titles">
                          <div className="up-card-title">模型分布</div>
                          <div className="up-card-sub">{byModel.length} 个模型 · 点表头可按任意一列排序</div>
                        </div>
                      </header>
                      <div className="up-card-body up-models">
                        <UsageDonutChart segments={donutSegs} centerValue={totalTokens} legend={false} />
                        <UsageModelTable rows={byModel} totalTokens={totalTokens} />
                      </div>
                    </section>
                  )}
                </>
              )}

              {/* Token 活动热力图：全历史口径、不随上方时间范围变化，放在跟随筛选的卡片之后 */}
              <UsageHeatmap daily_all={stats.daily_all} />
            </div>
          )}
        </div>
      )}
    </div>
  );
}
