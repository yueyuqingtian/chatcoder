/** 本机是否安装了某字体。
 *
 * 浏览器不提供「枚举系统字体」的同步接口，这里用 canvas 测宽：同一段文字分别用
 * 「候选字体, serif」与「serif」渲染，宽度不同说明候选字体真的被用上了；
 * 未安装时浏览器回落到 serif，测宽必然相同。基线选 serif 而不是 monospace，
 * 避免系统默认等宽字体恰好就是候选字体（如 Consolas）时误判为未安装。
 */
const PROBE_TEXT = "mmmmmmmmmmlli0OQ@#%&WMwm";
const PROBE_SIZE = 72;
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
  cache.set(name, installed);
  return installed;
}
