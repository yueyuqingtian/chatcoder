/** 秒级时钟（plan-73-323 阶段2）：供胶囊/面板显示"已耗时"。
 *
 * 独立小 hook：仅在有运行任务时启用，空闲态不产生定时器与重渲染。
 */
import { useEffect, useState } from "react";

export function useNow(active: boolean, intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(timer);
  }, [active, intervalMs]);
  return now;
}

/** 已耗时格式化：<1h 用 mm:ss，否则 h:mm:ss（与主窗"运行时长"观感一致） */
export function formatElapsed(from: number | null, now: number): string {
  if (!from) return "";
  const sec = Math.max(0, Math.floor((now - from) / 1000));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  const pad = (n: number) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}

/** 相对时间（最近完成列表用） */
export function formatRelative(at: number, now: number): string {
  const diff = Math.max(0, now - at);
  const min = Math.floor(diff / 60000);
  if (min < 1) return "刚刚";
  if (min < 60) return `${min} 分钟前`;
  const hour = Math.floor(min / 60);
  if (hour < 24) return `${hour} 小时前`;
  return `${Math.floor(hour / 24)} 天前`;
}
