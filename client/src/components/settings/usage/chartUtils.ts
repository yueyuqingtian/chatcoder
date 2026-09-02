/** 用量图表公共工具：模型配色、token 格式化、日期工具。纯函数，无状态。 */

/** 模型稳定配色（按 by_model 排序位置分配，最多 10 色，超出循环复用）。 */
export const CHART_COLORS = [
  "#3b82f6", "#22c55e", "#a855f7", "#f59e0b", "#ef4444",
  "#06b6d4", "#84cc16", "#f97316", "#8b5cf6", "#ec4899",
];

export function modelColor(index: number): string {
  return CHART_COLORS[index % CHART_COLORS.length];
}

/** 把 token 数格式化为可读字符串（M/k 缩写）。 */
export function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
}

/** 日期字符串（YYYY-MM-DD）→ Date（本地时间 0 点）。 */
export function parseDate(s: string): Date {
  const [y, m, d] = s.split("-").map(Number);
  return new Date(y, m - 1, d);
}

/** Date → YYYY-MM-DD（本地时区）。 */
export function toISO(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

export function addDays(d: Date, n: number): Date {
  const r = new Date(d);
  r.setDate(r.getDate() + n);
  return r;
}
