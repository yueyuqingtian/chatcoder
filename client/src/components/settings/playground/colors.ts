/** 把任意 CSS 颜色（含 oklch / color-mix / var）解析成 three 能用的 `#rrggbb`：
 * 先让浏览器算出计算值，再画到 1×1 画布上读像素。three 的 Color 只认 sRGB 字面量，
 * 直接传 oklch 字符串会静默失败，场景退化成黑白。 */
export function makeColorResolver(probe: HTMLElement) {
  const c = document.createElement("canvas");
  c.width = c.height = 1;
  const x = c.getContext("2d", { willReadFrequently: true });
  return (cssVar: string, fallback: string): string => {
    probe.style.color = `var(${cssVar}, ${fallback})`;
    const computed = getComputedStyle(probe).color || fallback;
    if (!x) return fallback;
    x.clearRect(0, 0, 1, 1);
    x.fillStyle = fallback;
    x.fillStyle = computed;
    x.fillRect(0, 0, 1, 1);
    const [r, g, b] = x.getImageData(0, 0, 1, 1).data;
    return "#" + [r, g, b].map((v) => v.toString(16).padStart(2, "0")).join("");
  };
}
