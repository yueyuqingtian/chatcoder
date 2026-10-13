/** 相对时间工具：今天显示 HH:mm，否则显示 M月d日 HH:mm（支持中英双语）。 */
import type { Language } from "../store/ui";

const EN_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** 服务端时间 → 毫秒（无法解析返回 NaN）。
 *  无时区后缀的串按 UTC 解析：SQLite `func.now()` 为 "YYYY-MM-DD HH:MM:SS"，
 *  Python `utcnow().isoformat()` 也不带后缀，二者都是 UTC。若按本地时间解析，
 *  显示会与本机时钟相差时区偏移（工作流进展曾因此相差 8 小时）。 */
export function parseServerTime(s?: string | null): number {
  if (!s) return NaN;
  const raw = s.trim();
  const hasZone = /(Z|[+-]\d{2}:?\d{2})$/i.test(raw);
  const iso = raw.replace(" ", "T").replace(/(\.\d{3})\d+/, "$1");
  return Date.parse(hasZone ? iso : `${iso}Z`);
}

export function formatTime(iso: string | null, lang: Language = "zh"): string {
  if (!iso) return "";
  const d = new Date(parseServerTime(iso));
  if (isNaN(d.getTime())) return "";
  const now = new Date();
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  const isToday =
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate();
  if (isToday) return `${hh}:${mm}`;
  if (lang === "en") {
    return `${EN_MONTHS[d.getMonth()]} ${d.getDate()} ${hh}:${mm}`;
  }
  return `${d.getMonth() + 1}月${d.getDate()}日 ${hh}:${mm}`;
}

/** 服务端时间按 UTC 解析（见 parseServerTime）；无法解析返回 0 */
export function parseUtc(s?: string | null): number {
  const t = parseServerTime(s);
  return Number.isNaN(t) ? 0 : t;
}

/** 相对时间（中英自适应：19分 / 19m，2小时 / 2h，3天 / 3d） */
export function formatRelativeTime(s?: string | null, lang: Language = "zh"): string {
  if (!s) return "";
  const ts = parseUtc(s);
  if (!ts) return "";
  const diff = Date.now() - ts;
  const isEn = lang === "en";
  if (diff < 0) return isEn ? "just now" : "刚刚";
  const min = Math.floor(diff / 60000);
  if (min < 1) return isEn ? "just now" : "刚刚";
  if (min < 60) return isEn ? `${min}m` : `${min}分`;
  const hour = Math.floor(min / 60);
  if (hour < 24) return isEn ? `${hour}h` : `${hour}小时`;
  const day = Math.floor(hour / 24);
  if (day < 30) return isEn ? `${day}d` : `${day}天`;
  const d = new Date(ts);
  if (isEn) {
    return `${EN_MONTHS[d.getMonth()]} ${d.getDate()}`;
  }
  return `${d.getMonth() + 1}月${d.getDate()}日`;
}
