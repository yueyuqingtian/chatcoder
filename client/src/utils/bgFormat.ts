/** 后台命令的展示用纯函数（plan-20261020-bgtask）：无 store / 网络依赖，timeline 与右上角卡片共用，便于单测。 */

/** 「45 秒 / 3 分 12 秒 / 1 小时 05 分」：运行时间的统一写法。 */
export function formatBgElapsed(totalSec: number): string {
  const sec = Math.max(0, Math.floor(Number.isFinite(totalSec) ? totalSec : 0));
  if (sec < 60) return `${sec} 秒`;
  const m = Math.floor(sec / 60);
  if (sec < 3600) return `${m} 分 ${String(sec % 60).padStart(2, "0")} 秒`;
  const h = Math.floor(sec / 3600);
  return `${h} 小时 ${String(m % 60).padStart(2, "0")} 分`;
}

/** 命令的行内展示：取首个非空行并压缩空白（完整命令走 Tooltip）。 */
export function bgCommandLine(command: string): string {
  const first = command.split(/\r?\n/).map((l) => l.trim()).find((l) => l.length > 0) ?? "";
  return first.replace(/\s+/g, " ");
}
