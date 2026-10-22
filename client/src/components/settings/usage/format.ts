/** 用量页的数值 / 时间格式化（纯函数，无状态）。 */

const pad2 = (n: number) => String(n).padStart(2, "0");

/** 千分位整数（调用记录里 token 数要看精确值，不缩写成 k / M） */
export function fmtInt(n: number | null | undefined): string {
  return (n ?? 0).toLocaleString("en-US");
}

/** 毫秒 → 秒（< 100 秒保留 2 位小数；更长的写成 `1分05秒`）。空值返回 null，由调用方决定占位。 */
export function fmtSec(ms: number | null | undefined): string | null {
  if (ms == null) return null;
  const s = ms / 1000;
  if (s < 100) return `${s.toFixed(2)}s`;
  const m = Math.floor(s / 60);
  const rest = Math.round(s - m * 60);
  return `${m}分${pad2(rest)}秒`;
}

/** 估算耗时只有秒级精度，不显示小数 */
export function fmtSecRough(ms: number | null | undefined): string | null {
  if (ms == null) return null;
  const s = Math.round(ms / 1000);
  if (s < 100) return `${s}s`;
  return `${Math.floor(s / 60)}分${pad2(s % 60)}秒`;
}

/** token/s：≥ 100 取整，否则保留 1 位小数 */
export function fmtSpeed(v: number | null | undefined): string | null {
  if (v == null) return null;
  return v >= 100 ? String(Math.round(v)) : v.toFixed(1);
}

const WEEKDAYS = ["日", "一", "二", "三", "四", "五", "六"];

/** 本地日期（YYYY-MM-DD）→ 分组标题：「今天 · 10月22日 周四」「昨天 · …」「10月20日 周一」，跨年时带年份 */
export function dayLabel(iso: string, now: Date = new Date()): string {
  const [y, m, d] = iso.split("-").map(Number);
  if (!y || !m || !d) return iso;
  const date = new Date(y, m - 1, d);
  const base = `${y !== now.getFullYear() ? `${y}年` : ""}${m}月${d}日 周${WEEKDAYS[date.getDay()]}`;
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const diff = Math.round((today.getTime() - date.getTime()) / 86_400_000);
  if (diff === 0) return `今天 · ${base}`;
  if (diff === 1) return `昨天 · ${base}`;
  return base;
}

/** UTC ISO（带 Z）→ 本地 年-月-日 与 时:分:秒，分开返回便于把日期弱化显示 */
export function splitLocalTime(iso: string | null | undefined): { date: string; clock: string } | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return {
    date: `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`,
    clock: `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`,
  };
}
