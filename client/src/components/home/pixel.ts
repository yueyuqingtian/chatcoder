/** 像素画引擎（plan-20261009-ui U2）：邮票插画用的极小绘制 DSL。
 *
 * 为什么不手敲字符网格：32×32 的像素画要有明暗与描边才精致，手敲既难校对又难改；
 * 这里用几何图元（矩形 / 圆 / 椭圆 / 多边形 / 线）+ 一键描边 + 混色，按「图层」组合场景，
 * 渲染时再把同色横向游程合并成 <rect>，一枚邮票只产生一两百个节点。
 * 约定：光源在左上；每个主体先画在独立图层上，再 `.outlined(深色)` 一键描边后叠到场景。 */

export type Cell = string | null;

function hexToRgb(h: string): [number, number, number] {
  const s = h.replace("#", "");
  const f = s.length === 3 ? s.split("").map((c) => c + c).join("") : s;
  return [parseInt(f.slice(0, 2), 16), parseInt(f.slice(2, 4), 16), parseInt(f.slice(4, 6), 16)];
}
function rgbToHex(r: number, g: number, b: number): string {
  const c = (v: number) => Math.max(0, Math.min(255, Math.round(v))).toString(16).padStart(2, "0");
  return `#${c(r)}${c(g)}${c(b)}`;
}
/** 混色：t=0 为 a，t=1 为 b */
export function mix(a: string, b: string, t: number): string {
  const [ar, ag, ab] = hexToRgb(a);
  const [br, bg, bb] = hexToRgb(b);
  return rgbToHex(ar + (br - ar) * t, ag + (bg - ag) * t, ab + (bb - ab) * t);
}
export const lighten = (c: string, t: number) => mix(c, "#ffffff", t);
export const darken = (c: string, t: number) => mix(c, "#000000", t);

export interface PixelRun { x: number; y: number; w: number; color: string }

export class Px {
  readonly cells: Cell[];
  constructor(readonly w = 32, readonly h = 32) {
    this.cells = new Array<Cell>(w * h).fill(null);
  }

  set(x: number, y: number, c: Cell): this {
    const xi = Math.round(x);
    const yi = Math.round(y);
    if (xi < 0 || yi < 0 || xi >= this.w || yi >= this.h) return this;
    this.cells[yi * this.w + xi] = c;
    return this;
  }
  get(x: number, y: number): Cell {
    if (x < 0 || y < 0 || x >= this.w || y >= this.h) return null;
    return this.cells[y * this.w + x];
  }
  rect(x: number, y: number, w: number, h: number, c: Cell): this {
    for (let j = 0; j < h; j++) for (let i = 0; i < w; i++) this.set(x + i, y + j, c);
    return this;
  }
  /** 圆盘：(cx, cy) 为几何圆心（像素中心为 +0.5），可取半整数 */
  disc(cx: number, cy: number, r: number, c: Cell): this {
    return this.ellipse(cx, cy, r, r, c);
  }
  ellipse(cx: number, cy: number, rx: number, ry: number, c: Cell): this {
    for (let y = Math.floor(cy - ry); y <= Math.ceil(cy + ry); y++) {
      for (let x = Math.floor(cx - rx); x <= Math.ceil(cx + rx); x++) {
        const dx = (x + 0.5 - cx) / rx;
        const dy = (y + 0.5 - cy) / ry;
        if (dx * dx + dy * dy <= 1) this.set(x, y, c);
      }
    }
    return this;
  }
  ring(cx: number, cy: number, ro: number, ri: number, c: Cell): this {
    for (let y = Math.floor(cy - ro); y <= Math.ceil(cy + ro); y++) {
      for (let x = Math.floor(cx - ro); x <= Math.ceil(cx + ro); x++) {
        const d2 = (x + 0.5 - cx) ** 2 + (y + 0.5 - cy) ** 2;
        if (d2 <= ro * ro && d2 >= ri * ri) this.set(x, y, c);
      }
    }
    return this;
  }
  /** Bresenham 直线（端点含） */
  line(x0: number, y0: number, x1: number, y1: number, c: Cell): this {
    let x = Math.round(x0), y = Math.round(y0);
    const xe = Math.round(x1), ye = Math.round(y1);
    const dx = Math.abs(xe - x), dy = -Math.abs(ye - y);
    const sx = x < xe ? 1 : -1, sy = y < ye ? 1 : -1;
    let err = dx + dy;
    for (;;) {
      this.set(x, y, c);
      if (x === xe && y === ye) break;
      const e2 = 2 * err;
      if (e2 >= dy) { err += dy; x += sx; }
      if (e2 <= dx) { err += dx; y += sy; }
    }
    return this;
  }
  /** 多边形（偶奇填充，按像素中心采样） */
  poly(pts: Array<[number, number]>, c: Cell): this {
    const ys = pts.map((p) => p[1]);
    const y0 = Math.floor(Math.min(...ys));
    const y1 = Math.ceil(Math.max(...ys));
    for (let y = y0; y <= y1; y++) {
      const yc = y + 0.5;
      const xs: number[] = [];
      for (let i = 0; i < pts.length; i++) {
        const [ax, ay] = pts[i];
        const [bx, by] = pts[(i + 1) % pts.length];
        if ((ay <= yc && by > yc) || (by <= yc && ay > yc)) xs.push(ax + ((yc - ay) / (by - ay)) * (bx - ax));
      }
      xs.sort((a, b) => a - b);
      for (let k = 0; k + 1 < xs.length; k += 2) {
        for (let x = Math.ceil(xs[k] - 0.5); x <= Math.floor(xs[k + 1] - 0.5); x++) this.set(x, y, c);
      }
    }
    return this;
  }
  roundRect(x: number, y: number, w: number, h: number, r: number, c: Cell): this {
    for (let j = 0; j < h; j++) {
      for (let i = 0; i < w; i++) {
        const cx = i < r ? r : i >= w - r ? w - r : -1;
        const cy = j < r ? r : j >= h - r ? h - r : -1;
        if (cx >= 0 && cy >= 0) {
          const dx = i + 0.5 - cx;
          const dy = j + 0.5 - cy;
          if (dx * dx + dy * dy > r * r) continue;
        }
        this.set(x + i, y + j, c);
      }
    }
    return this;
  }
  /** 把另一个图层叠上来（null 为透明） */
  blit(src: Px, ox = 0, oy = 0): this {
    for (let y = 0; y < src.h; y++) for (let x = 0; x < src.w; x++) {
      const c = src.cells[y * src.w + x];
      if (c) this.set(x + ox, y + oy, c);
    }
    return this;
  }
  /** 一键描边：在主体外圈（4 邻域）补一圈描边色，返回新图层（含原主体） */
  outlined(color: string): Px {
    const out = new Px(this.w, this.h);
    for (let y = 0; y < this.h; y++) for (let x = 0; x < this.w; x++) {
      if (this.get(x, y)) continue;
      if (this.get(x - 1, y) || this.get(x + 1, y) || this.get(x, y - 1) || this.get(x, y + 1)) out.set(x, y, color);
    }
    return out.blit(this);
  }
  /** 同色横向游程（渲染用） */
  runs(): PixelRun[] {
    const out: PixelRun[] = [];
    for (let y = 0; y < this.h; y++) {
      let x = 0;
      while (x < this.w) {
        const c = this.cells[y * this.w + x];
        if (!c) { x++; continue; }
        let n = 1;
        while (x + n < this.w && this.cells[y * this.w + x + n] === c) n++;
        out.push({ x, y, w: n, color: c });
        x += n;
      }
    }
    return out;
  }
}

/** 背景：上下两色（上为天空 / 墙面，下为地面）。dither=true 时在交界处加棋盘抖动（像素画经典过渡，
 *  首页邮票为了清爽默认不用） */
export function sky(p: Px, top: string, bottom: string, split: number, dither = false): void {
  p.rect(0, 0, p.w, split, top);
  p.rect(0, split, p.w, p.h - split, bottom);
  if (!dither) return;
  for (let x = 0; x < p.w; x += 2) p.set(x, split - 1, bottom);
  for (let x = 1; x < p.w; x += 2) p.set(x, split, top);
}

/** 斜向棒状物：从 (x0,y0) 到 (x1,y1) 的粗线，半宽 hw；c 为 null 时是「挖掉」。
 *  像素画里画铅笔 / 扳手 / 锤柄这类斜着的物件，比手排像素点省事得多 */
export function bar(p: Px, x0: number, y0: number, x1: number, y1: number, hw: number, c: Cell): void {
  const dx = x1 - x0;
  const dy = y1 - y0;
  const len = Math.hypot(dx, dy) || 1;
  const nx = (-dy / len) * hw;
  const ny = (dx / len) * hw;
  p.poly([[x0 + nx, y0 + ny], [x1 + nx, y1 + ny], [x1 - nx, y1 - ny], [x0 - nx, y0 - ny]], c);
}

/** 四角星（闪光点缀） */
export function sparkle(p: Px, cx: number, cy: number, c: string, arm = 2): void {
  p.set(cx, cy, c);
  for (let i = 1; i <= arm; i++) { p.set(cx - i, cy, c); p.set(cx + i, cy, c); p.set(cx, cy - i, c); p.set(cx, cy + i, c); }
}

/** 字符网格 → 图层（'.' 透明，其余字符查调色板） */
export function fromRows(rows: string[], palette: Record<string, string>): Px {
  const p = new Px(Math.max(...rows.map((r) => r.length)), rows.length);
  rows.forEach((row, y) => { for (let x = 0; x < row.length; x++) { const c = palette[row[x]]; if (c) p.set(x, y, c); } });
  return p;
}
