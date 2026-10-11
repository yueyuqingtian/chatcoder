/** 供应商头像（plan-114-450）：取代灰色首字母方块。
 * 做法：按名称稳定哈希到 8 种低彩度色相，柔和渐变底 + 白色首字（中文取首字、英文取首字母），
 * 与嗒嗒同一套「受光渐变 + 无描边」画法；同一供应商在任何地方颜色不变。
 * 只做视觉区分，不承载状态（状态由右侧圆点表达）。 */
const HUES = [250, 200, 160, 120, 60, 30, 350, 300];

function hashName(s: string): number {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  return h;
}

export function ProviderAvatar({ name, size = 28, muted = false }: { name: string; size?: number; muted?: boolean }) {
  const clean = (name || "?").trim();
  const glyph = (/^[\x00-\x7F]/.test(clean) ? clean.slice(0, 1).toUpperCase() : clean.slice(0, 1)) || "?";
  const h = HUES[hashName(clean.toLowerCase()) % HUES.length];
  return (
    <span
      className={"prov-avatar" + (muted ? " is-muted" : "")}
      aria-hidden
      style={{
        width: size, height: size, fontSize: Math.round(size * 0.46),
        ["--pa-h" as string]: String(h),
      }}
    >
      {glyph}
    </span>
  );
}
