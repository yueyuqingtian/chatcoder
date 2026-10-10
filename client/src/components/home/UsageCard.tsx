/** 首页用量卡（plan-20261009-ui U2）：近一年活跃热力图，「会话 / Token」两个维度。
 *
 * 与「设置 → 使用统计」里的热力图同一视觉语言（同一套绿色分级 .usage-hm-0..4、同款悬停浮窗），
 * 区别是这里按卡片宽度等比铺满、Monday 起始、只做轻量概览。
 *  · 会话：按会话「创建日」统计（来自 store.sessions，无需额外请求）；
 *  · Token：首次切到该维度时才请求 /usage/stats（近 371 天），请求期间网格保持原尺寸，不跳变。 */
import { useEffect, useMemo, useRef, useState, type MouseEvent } from "react";
import { api } from "../../api/client";
import { useChatStore } from "../../store/chat";
import { parseUtc } from "../../utils/time";
import { Segmented } from "../ui";
import { addDays, fmtTokens, toISO } from "../settings/usage/chartUtils";

const WEEKS = 53;

type Tab = "sessions" | "tokens";

function tier(v: number, max: number): number {
  if (v <= 0 || max <= 0) return 0;
  return Math.min(4, Math.max(1, Math.ceil((v / max) * 4)));
}

function prettyDate(key: string): string {
  const [y, m, d] = key.split("-").map(Number);
  return `${y} 年 ${m} 月 ${d} 日`;
}

export function UsageCard() {
  const sessions = useChatStore((s) => s.sessions);
  const [tab, setTab] = useState<Tab>("sessions");
  const [tokenMap, setTokenMap] = useState<Record<string, number> | null>(null);
  const [tip, setTip] = useState<{ x: number; y: number; title: string; sub: string } | null>(null);
  const wrapRef = useRef<HTMLDivElement>(null);

  // Token 维度懒加载（失败按空数据处理，不阻塞首页）
  useEffect(() => {
    if (tab !== "tokens" || tokenMap) return;
    let off = false;
    api.getUsageStats({ days: 371 })
      .then((res) => {
        if (off) return;
        const m: Record<string, number> = {};
        for (const p of res.daily_all ?? []) m[p.date] = p.tokens;
        setTokenMap(m);
      })
      .catch(() => { if (!off) setTokenMap({}); });
    return () => { off = true; };
  }, [tab, tokenMap]);

  const sessionMap = useMemo(() => {
    const m: Record<string, number> = {};
    for (const s of sessions) {
      const ts = parseUtc(s.created_at) || parseUtc(s.last_activity_at);
      if (!ts) continue;
      const k = toISO(new Date(ts));
      m[k] = (m[k] ?? 0) + 1;
    }
    return m;
  }, [sessions]);

  /** 网格：53 列 × 7 行（周一起），列优先；月份标签落在每月首个出现的列 */
  const grid = useMemo(() => {
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const todayKey = toISO(today);
    const lastMon = addDays(today, -((today.getDay() + 6) % 7));
    const firstMon = addDays(lastMon, -(WEEKS - 1) * 7);
    const cells: Array<{ key: string; future: boolean }> = [];
    const months: Array<{ col: number; label: string }> = [];
    let lastMonth = -1;
    let lastLabelCol = -99;
    for (let c = 0; c < WEEKS; c++) {
      const mon = addDays(firstMon, c * 7);
      const mk = mon.getFullYear() * 12 + mon.getMonth();
      if (mk !== lastMonth) {
        if (c - lastLabelCol >= 4) { months.push({ col: c, label: `${mon.getMonth() + 1}月` }); lastLabelCol = c; }
        lastMonth = mk;
      }
      for (let r = 0; r < 7; r++) {
        const key = toISO(addDays(mon, r));
        cells.push({ key, future: key > todayKey });
      }
    }
    return { cells, months, todayKey };
  }, []);

  const series = tab === "sessions" ? sessionMap : tokenMap ?? {};
  const loading = tab === "tokens" && tokenMap === null;

  const stats = useMemo(() => {
    let max = 0;
    let sum = 0;
    for (const c of grid.cells) {
      if (c.future) continue;
      const v = series[c.key] ?? 0;
      sum += v;
      if (v > max) max = v;
    }
    // 连续活跃天数：从今天往回数（今天还没动静则从昨天起算，避免清晨显示 0）
    let streak = 0;
    let d = new Date();
    d.setHours(0, 0, 0, 0);
    if ((series[toISO(d)] ?? 0) === 0) d = addDays(d, -1);
    while ((series[toISO(d)] ?? 0) > 0 && streak < 400) { streak++; d = addDays(d, -1); }
    return { max, sum, streak };
  }, [grid, series]);

  const onOver = (e: MouseEvent<HTMLDivElement>) => {
    const el = (e.target as HTMLElement).closest<HTMLElement>("[data-k]");
    const wrap = wrapRef.current;
    if (!el || !wrap) { setTip(null); return; }
    const key = el.dataset.k as string;
    const v = series[key] ?? 0;
    const wr = wrap.getBoundingClientRect();
    const er = el.getBoundingClientRect();
    setTip({
      x: er.left - wr.left + er.width / 2,
      y: er.top - wr.top,
      title: prettyDate(key),
      sub: tab === "sessions" ? `${v} 个会话` : `${fmtTokens(v)} tokens`,
    });
  };

  const summary = loading
    ? "统计中…"
    : tab === "sessions"
      ? `近一年 ${stats.sum} 个会话`
      : `近一年 ${fmtTokens(stats.sum)} tokens`;

  return (
    <div className="es-usage" ref={wrapRef} onMouseLeave={() => setTip(null)}>
      <div className="es-usage-head">
        <Segmented
          size="sm"
          value={tab}
          onChange={setTab}
          aria-label="用量维度"
          options={[{ value: "sessions", label: "会话" }, { value: "tokens", label: "Token" }]}
        />
        <span className="es-usage-sum">
          {summary}
          {!loading && stats.streak > 0 && <span className="es-usage-streak"> · 已连续 {stats.streak} 天</span>}
        </span>
      </div>
      <div className={"es-heat" + (loading ? " is-loading" : "")} onMouseOver={onOver} aria-label="近一年活跃热力图" role="img">
        {grid.cells.map((c) => (
          <div
            key={c.key}
            data-k={c.future ? undefined : c.key}
            className={`usage-hm-cell usage-hm-${c.future ? 0 : tier(series[c.key] ?? 0, stats.max)}${c.future ? " is-future" : ""}`}
          />
        ))}
      </div>
      <div className="es-heat-months" aria-hidden>
        {grid.months.map((mo) => (
          <span key={mo.col} className="es-heat-month" style={{ gridColumn: `${mo.col + 1} / span 4` }}>{mo.label}</span>
        ))}
      </div>
      {tip && (
        <div
          className="usage-hm-tip"
          style={{ left: tip.x, top: tip.y, transform: tip.y < 56 ? "translate(-50%, 18px)" : "translate(-50%, calc(-100% - 6px))" }}
        >
          <div className="usage-tip-date">{tip.title}</div>
          <div className="usage-tip-row">
            <span className="usage-tip-name">{tab === "sessions" ? "会话" : "用量"}</span>
            <span className="usage-tip-val">{tip.sub}</span>
          </div>
        </div>
      )}
    </div>
  );
}
