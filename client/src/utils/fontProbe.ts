/** 本机是否安装了某字体。
 *
 * 浏览器不提供「枚举系统字体」的同步接口，这里用 canvas 测宽：同一段文字分别用
 * 「候选字体, serif」与「serif」渲染，宽度不同说明候选字体真的被用上了；
 * 未安装时浏览器回落到 serif，测宽必然相同。基线选 serif 而不是 monospace，
 * 避免系统默认等宽字体恰好就是候选字体（如 Consolas）时误判为未安装。
 */
const PROBE_TEXT = "mmmmmmmmmmlli0OQ@#%&WMwm";
const PROBE_SIZE = 72;
/** 只缓存「已安装」结果。
 *  未安装→已安装的转变是真实存在的（用户装字体、或应用版本升级把字体改成内置），
 *  若把 false 也永久缓存，界面会一直显示「未安装」直到重启，且没有失效入口。
 *  实测成本很低（一次 canvas 测宽），因此 false 不缓存。 */
const cache = new Map<string, boolean>();

export function isFontInstalled(family: string): boolean {
  const name = family.trim();
  if (!name) return false;
  const hit = cache.get(name);
  if (hit !== undefined) return hit;
  let installed = false;
  try {
    const ctx = document.createElement("canvas").getContext("2d");
    if (ctx) {
      ctx.font = `${PROBE_SIZE}px serif`;
      const base = ctx.measureText(PROBE_TEXT).width;
      ctx.font = `${PROBE_SIZE}px "${name.replace(/"/g, "")}", serif`;
      installed = ctx.measureText(PROBE_TEXT).width !== base;
    }
  } catch {
    installed = false;
  }
  // 只缓存正向结果（见上方注释）
  if (installed) cache.set(name, true);
  return installed;
}

/** 清空探测缓存：字体可能在使用期间被装上/卸载（或由版本升级带来内置字体），
 *  设置页重新进入时调用即可刷新「未安装」标注。 */
export function revalidateFonts(): void {
  cache.clear();
}
