/** 相对时间工具：今天显示 HH:mm，否则显示 M月d日 HH:mm。 */
export function formatTime(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "";
  const now = new Date();
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  const isToday =
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate();
  if (isToday) return `${hh}:${mm}`;
  return `${d.getMonth() + 1}月${d.getDate()}日 ${hh}:${mm}`;
}

/** SQLite func.now() 为 UTC "YYYY-MM-DD HH:MM:SS"，需按 UTC 解析 */
export function parseUtc(s: string): number {
  const t = Date.parse(s.includes("T") ? s : s.replace(" ", "T") + "Z");
  return Number.isNaN(t) ? 0 : t;
}

/** 相对时间（对齐 zcode：19分 / 2小时 / 3天） */
export function formatRelativeTime(s?: string | null): string {
  if (!s) return "";
  const ts = parseUtc(s);
  if (!ts) return "";
  const diff = Date.now() - ts;
  if (diff < 0) return "刚刚";
  const min = Math.floor(diff / 60000);
  if (min < 1) return "刚刚";
  if (min < 60) return `${min}分`;
  const hour = Math.floor(min / 60);
  if (hour < 24) return `${hour}小时`;
  const day = Math.floor(hour / 24);
  if (day < 30) return `${day}天`;
  const d = new Date(ts);
  return `${d.getMonth() + 1}月${d.getDate()}日`;
}
