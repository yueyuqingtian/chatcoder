/** 邮票的「聚焦鼠标」（plan-20261009-ui U2）。
 *
 * 机制：鼠标移到邮票上时，指针变成这枚邮票故事里的小物件——就像参考软件里的苹果。
 * 造型是本软件自己的：每枚邮票对应「办成那件事用的工具」（改→铅笔、修→扳手、测→烧瓶……），
 * 与邮票插画同一套 16×16 像素画语言，描边 + 明暗，指针热点在物件中心。
 *
 * 渲染：把 16×16 的像素图按整数倍铺到 canvas（关闭平滑），导出 PNG 作为 CSS cursor。
 * 1x 屏用 2 倍（32px），2x 屏用 4 倍（64 物理像素），经 -webkit-image-set 自动择优，
 * 因此在高分屏上同样是锐利的方块像素，而不是被放大糊开的位图。 */
import { Px, bar, lighten, sparkle } from "./pixel";

const S = 16;

/** 改：铅笔（笔尖朝左下，橡皮在右上） */
function curPencil(): Px {
  const p = new Px(S, S);
  // 沿笔身轴线 E→T 取点：s 为距橡皮端的距离
  const E: [number, number] = [14, 2];
  const T: [number, number] = [2, 14];
  const len = Math.hypot(T[0] - E[0], T[1] - E[1]);
  const at = (s: number): [number, number] => [E[0] + ((T[0] - E[0]) * s) / len, E[1] + ((T[1] - E[1]) * s) / len];
  const seg = (a: number, b: number, hw: number, c: string) => { const [x0, y0] = at(a); const [x1, y1] = at(b); bar(p, x0, y0, x1, y1, hw, c); };
  seg(0, 2.4, 2.1, "#F28DA6"); // 橡皮
  seg(2.4, 4.2, 2.1, "#C9D0D8"); // 金属箍
  seg(4.2, 13.2, 2.1, "#FFC93C"); // 笔身
  seg(5, 13, 0.7, "#F5A70F"); // 笔身暗纹
  // 笔尖削出的木头：前端收窄
  const [bx, by] = at(13.2);
  const [tx, ty] = at(len + 0.6);
  const nx = -(T[1] - E[1]) / len;
  const ny = (T[0] - E[0]) / len;
  p.poly([[bx + nx * 2.1, by + ny * 2.1], [bx - nx * 2.1, by - ny * 2.1], [tx, ty]], "#F6D9A8");
  const [gx, gy] = at(len - 1.2);
  p.poly([[gx + nx * 0.9, gy + ny * 0.9], [gx - nx * 0.9, gy - ny * 0.9], [tx, ty]], "#3B3B46");
  return p.outlined("#4A3418");
}

/** 修：扳手（开口朝右上） */
function curWrench(): Px {
  const p = new Px(S, S);
  const steel = "#A9B6C4";
  bar(p, 3.6, 12.4, 9.4, 6.6, 1.7, steel); // 柄
  p.disc(3.4, 12.6, 2.2, steel); // 尾端圆
  p.disc(3.2, 12.8, 0.8, null);
  p.disc(11.2, 4.8, 3.7, steel); // 头
  bar(p, 11.6, 4.4, 16, 0, 1.45, null); // 开口
  p.set(5, 10, lighten(steel, 0.5)).set(6, 9, lighten(steel, 0.5)).set(9, 3, lighten(steel, 0.55)).set(8, 4, lighten(steel, 0.5));
  return p.outlined("#2D3A4B");
}

/** 测：烧瓶（里面是绿色的「通过」） */
function curFlask(): Px {
  const p = new Px(S, S);
  p.rect(6, 1, 4, 5, "#E7F3FF"); // 瓶颈
  p.rect(5, 1, 6, 1, "#FFFFFF"); // 瓶口
  p.poly([[6, 5], [10, 5], [14.2, 12.6], [13.2, 14.6], [2.8, 14.6], [1.8, 12.6]], "#E7F3FF"); // 瓶身
  // 液体：只填瓶身下半
  for (let y = 9; y < 15; y++) for (let x = 0; x < S; x++) {
    if (p.get(x, y)) p.set(x, y, "#35C46F");
  }
  for (let x = 0; x < S; x++) if (p.get(x, 9) === "#35C46F") p.set(x, 9, "#6FE09C");
  p.set(6, 11, "#C9F5DA").set(9, 12, "#C9F5DA").set(7, 13, "#C9F5DA");
  p.set(6, 3, "#FFFFFF").set(4, 11, "#FFFFFF").set(3, 12, "#FFFFFF");
  return p.outlined("#2D4A5E");
}

/** 懂：放大镜 */
function curMagnifier(): Px {
  const p = new Px(S, S);
  bar(p, 10.4, 10.4, 14.4, 14.4, 1.45, "#9A6A44"); // 手柄
  p.disc(6.4, 6.4, 5.4, "#4B5B70"); // 镜框
  p.disc(6.4, 6.4, 3.9, "#CFE8FF"); // 镜片
  p.disc(8, 8.2, 2.3, "#B4D6F5");
  p.set(4, 4, "#FFFFFF").set(5, 3, "#FFFFFF").set(3, 5, "#FFFFFF").set(4, 3, "#FFFFFF");
  return p.outlined("#1F2937");
}

/** 重：魔法棒（结构换了，行为没变——像变魔术） */
function curWand(): Px {
  const p = new Px(S, S);
  bar(p, 2.4, 13.6, 10.6, 5.4, 1.15, "#3A4A86"); // 棒身
  bar(p, 8.8, 7.2, 10.8, 5.2, 1.15, "#FFFFFF"); // 白色笔端
  const body = p.outlined("#1B2347");
  sparkle(body, 12, 3, "#FFD54A", 2);
  body.set(12, 3, "#FFFFFF");
  sparkle(body, 3, 3, "#FFE58A", 1);
  body.set(14, 8, "#FFE58A").set(7, 1, "#FFE58A");
  return body;
}

/** 夜：月牙 + 小星 */
function curMoon(): Px {
  const p = new Px(S, S);
  p.disc(7.6, 8.4, 6.9, "#FFD86B");
  p.disc(12, 5, 5.9, null);
  p.set(3, 8, "#FFF1B8").set(3, 9, "#FFF1B8").set(4, 11, "#F0B13C").set(6, 13, "#F0B13C").set(9, 14, "#F0B13C");
  const moon = p.outlined("#8A5A12");
  sparkle(moon, 11, 7, "#FFF6C9", 1);
  moon.set(14, 3, "#FFF6C9").set(8, 2, "#FFF6C9");
  return moon;
}

/** 建：锤子 */
function curHammer(): Px {
  const p = new Px(S, S);
  bar(p, 6.2, 6.2, 13.8, 13.8, 1.45, "#B8845A"); // 柄
  bar(p, 9.9, 2.6, 2.7, 9.8, 2.1, "#8FA1B3"); // 锤头
  bar(p, 9.4, 2.4, 2.4, 9.4, 0.7, "#C8D4E0"); // 锤头高光
  return p.outlined("#2D3A4B");
}

/** 审：图章（盖下去，就算过审） */
function curStamp(): Px {
  const p = new Px(S, S);
  p.roundRect(6, 0, 4, 4, 1.5, "#C8553D"); // 把手顶
  p.rect(7, 3, 2, 4, "#C8553D"); // 把手
  p.rect(4, 7, 8, 2, "#E5483D"); // 护圈
  p.roundRect(3, 8, 10, 4, 1, "#E5483D"); // 章身
  p.rect(4, 9, 8, 1, "#FF7B6E");
  p.rect(2, 13, 12, 2, "#2F3A67"); // 印泥盒
  const out = p.outlined("#4A1D14");
  out.rect(3, 13, 10, 1, "#4B5FA8");
  return out;
}

const DRAW: Record<string, () => Px> = {
  radius: curPencil,
  http500: curWrench,
  "unit-test": curFlask,
  explain: curMagnifier,
  refactor: curWand,
  night: curMoon,
  scaffold: curHammer,
  review: curStamp,
};

/** 取某枚邮票的指针素材（16×16 像素图） */
export function stampCursorPx(id: string): Px {
  return (DRAW[id] ?? curPencil)();
}

const cssCache = new Map<string, string>();

/** 生成 CSS cursor 值；非浏览器环境（无 canvas）返回 null，调用方保持默认手型 */
export function stampCursorCss(id: string): string | null {
  const hit = cssCache.get(id);
  if (hit) return hit;
  if (typeof document === "undefined") return null;
  const px = stampCursorPx(id);
  const render = (scale: number): string | null => {
    const c = document.createElement("canvas");
    c.width = S * scale;
    c.height = S * scale;
    const g = c.getContext("2d");
    if (!g) return null;
    g.imageSmoothingEnabled = false;
    for (let y = 0; y < S; y++) for (let x = 0; x < S; x++) {
      const col = px.get(x, y);
      if (!col) continue;
      g.fillStyle = col;
      g.fillRect(x * scale, y * scale, scale, scale);
    }
    return c.toDataURL("image/png");
  };
  const x1 = render(2);
  const x2 = render(4);
  if (!x1 || !x2) return null;
  const hot = S; // 32px 图的中心
  const css = `-webkit-image-set(url(${x1}) 1x, url(${x2}) 2x) ${hot} ${hot}, pointer`;
  cssCache.set(id, css);
  return css;
}

