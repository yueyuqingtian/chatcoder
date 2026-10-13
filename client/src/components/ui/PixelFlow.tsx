/** PixelFlow —— 最高档的“渐变底色 + 像素点阵”流动轨道（plan-128-518，参考 Claude 的努力程度滑块）。
 *
 *  由两层组成，共用同一条向左行进的“相位波”，所以是同步流动的，而不是两个各跑各的效果：
 *   ① 底色层：强调色半透明渐变，右端（拇指处）最浓，向左逐渐变淡直至透明；
 *      浓度沿轨道叠加一道缓慢左行的明暗波，读作“光在底色里流过”；
 *   ② 点阵层：整齐的方形像素点（有序抖动 + 少量扰动，不是随机噪点），右密左疏；
 *      点阵纹理以与底色波相同的速度整体向左漂移，亮度也乘同一道波。
 *
 *  细节取舍：点比底色更浅（强调色向白色混合）→ 在深/浅轨道上都有对比；alpha 上限克制；
 *  全部按设备像素取整绘制，边缘锐利；30fps；仅最高档挂载期间运行；
 *  `--motion-scale` 为 0 / prefers-reduced-motion 时只画静态一帧。 */
import { useEffect, useRef } from "react";

/** 4×4 Bayer 矩阵（阈值 0~15） */
const BAYER = [
  [0, 8, 2, 10],
  [12, 4, 14, 6],
  [3, 11, 1, 9],
  [15, 7, 13, 5],
];

/** 波速：每秒行进的轨道宽度比例（底色波与点阵漂移共用，保证同步） */
const WAVE_SPEED = 0.2;
/** 波长（轨道宽度的倍数的倒数）：相位 = pos * WAVE_FREQ + t * WAVE_SPEED * WAVE_FREQ */
const WAVE_FREQ = 1.4;

function hash(x: number, y: number): number {
  let h = (x * 374761393 + y * 668265263) | 0;
  h = (h ^ (h >>> 13)) * 1274126177;
  return ((h ^ (h >>> 16)) >>> 0) / 4294967295;
}

const smoothstep = (a: number, b: number, v: number) => {
  const t = Math.min(1, Math.max(0, (v - a) / (b - a)));
  return t * t * (3 - 2 * t);
};

/** 把 CSS 变量颜色（含 oklch / color-mix）解析成 [r,g,b]：借 1×1 canvas 做色彩转换 */
function resolveRgb(cssVar: string, fallback: [number, number, number]): [number, number, number] {
  try {
    const probe = document.createElement("span");
    probe.style.color = `var(${cssVar})`;
    probe.style.display = "none";
    document.body.appendChild(probe);
    const computed = getComputedStyle(probe).color;
    probe.remove();
    const c = document.createElement("canvas");
    c.width = c.height = 1;
    const g = c.getContext("2d", { willReadFrequently: true });
    if (!g) return fallback;
    g.fillStyle = computed;
    g.fillRect(0, 0, 1, 1);
    const d = g.getImageData(0, 0, 1, 1).data;
    return [d[0], d[1], d[2]];
  } catch {
    return fallback;
  }
}

const mix = (a: number[], b: number[], k: number) =>
  a.map((v, i) => Math.round(v * (1 - k) + b[i] * k));

export function PixelFlow({ className = "" }: { className?: string }) {
  const ref = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    let raf = 0;
    let W = 0, H = 0;
    let P = 3, S = 2;
    let cols = 0, rows = 0, offX = 0, offY = 0;
    let baseRgb = "124,92,255";   // 底色层
    let dotRgb = "190,175,255";   // 点阵层（更浅）
    let last = 0;
    const t0 = performance.now();

    const readColor = () => {
      const a = resolveRgb("--accent", [124, 92, 255]);
      baseRgb = mix(a, [60, 50, 110], 0.1).join(",");        // 略压暗，底色更沉
      dotRgb = mix(a, [255, 255, 255], 0.62).join(",");      // 点阵向白色混合，形成层次
    };

    const resize = () => {
      const r = canvas.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      W = Math.max(1, Math.floor(r.width * dpr));
      H = Math.max(1, Math.floor(r.height * dpr));
      canvas.width = W;
      canvas.height = H;
      P = Math.max(3, Math.round(3 * dpr));
      S = Math.min(P - 1, Math.max(2, Math.round(2 * dpr)));
      const mx = Math.round(P * 1.2);
      const my = Math.round(P * 0.9);
      cols = Math.max(1, Math.floor((W - 2 * mx + (P - S)) / P));
      rows = Math.max(1, Math.floor((H - 2 * my + (P - S)) / P));
      offX = W - mx - (cols * P - (P - S));
      offY = Math.floor((H - (rows * P - (P - S))) / 2);
    };

    /** 相位波：随 t 增大，等相位点向 pos 变小的方向移动 = 向左流 */
    const wave = (pos: number, t: number) => Math.sin((pos + t * WAVE_SPEED) * WAVE_FREQ * Math.PI * 2);

    const draw = (now: number, animate: boolean) => {
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.clearRect(0, 0, W, H);
      const t = animate ? (now - t0) / 1000 : 0;

      // ① 底色层：右浓左淡的渐变，叠一道左行明暗波
      const grad = ctx.createLinearGradient(0, 0, W, 0);
      const STOPS = 28;
      for (let i = 0; i <= STOPS; i++) {
        const p = i / STOPS;
        const body = 0.62 * Math.pow(smoothstep(0.02, 1, p), 1.35);     // 基础浓度曲线
        const flow = 1 + 0.26 * wave(p, t) * smoothstep(0.1, 0.6, p);   // 左端波幅收敛，避免淡处闪动
        grad.addColorStop(p, `rgba(${baseRgb},${Math.min(0.78, body * flow).toFixed(3)})`);
      }
      ctx.fillStyle = grad;
      ctx.fillRect(0, 0, W, H);

      // ② 点阵层：纹理左漂速度 = 波速（列/秒），亮度乘同一道波
      const shift = animate ? t * WAVE_SPEED * cols : 0;
      const whole = Math.floor(shift);
      const frac = shift - whole;
      for (let c = 0; c < cols; c++) {
        const pos = c / Math.max(1, cols - 1);
        const density = smoothstep(0.2, 0.98, pos) * 1.04;
        const lum = 0.2 + 0.62 * Math.pow(pos, 1.15);
        const flow = 1 + 0.22 * wave(pos, t);
        const x = offX + c * P - Math.round(frac * P);
        const fc = c + whole;                                  // 流上的列号：纹理随它向左漂
        for (let r = 0; r < rows; r++) {
          const th = 0.84 * ((BAYER[r & 3][fc & 3] + 0.5) / 16) + 0.16 * hash(fc, r);
          if (density <= th) continue;
          const jitter = 0.9 + 0.1 * hash(fc + 53, r + 7);
          const a = Math.min(0.78, lum * flow * jitter);
          ctx.fillStyle = `rgba(${dotRgb},${a.toFixed(3)})`;
          ctx.fillRect(x, offY + r * P, S, S);
        }
      }
    };

    const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    const scale = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--motion-scale"));
    const animate = !reduced && !(Number.isFinite(scale) && scale <= 0);

    const loop = (now: number) => {
      raf = requestAnimationFrame(loop);
      if (now - last < 33) return;
      last = now;
      draw(now, true);
    };

    readColor();
    resize();
    if (animate) raf = requestAnimationFrame(loop);
    else draw(t0, false);

    const ro = new ResizeObserver(() => { resize(); if (!animate) draw(t0, false); });
    ro.observe(canvas);
    return () => { cancelAnimationFrame(raf); ro.disconnect(); };
  }, []);

  return <canvas ref={ref} className={`pixel-flow ${className}`.trim()} aria-hidden />;
}
