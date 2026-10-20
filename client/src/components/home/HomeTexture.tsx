/** 空态首页背景纹理（plan-91-393 → plan-114-448）。
 * 六个主题世界各一套纹理，互不重复，全部很轻（基础透明度 ≤0.14）：
 *  石墨=稿纸点阵 / 森林=散落叶片 / 篝火=余烬星点 / 煦风=风线 / 潮汐=潮纹 / 樱时=花瓣。
 * 两种墨色：--world-tex-a（中性墨）为主，--world-tex-b（强调色淡墨）点缀少量元素。
 * 鼠标附近纹理轻微变亮、位移；canvas 只在指针移动时重绘，降低动效时保持静态。 */
import { useEffect, useRef } from "react";
import { isBusy, subscribe } from "../../perf/bus";
import { useThemeStore, type World } from "../../store/theme";

const RADIUS = 180;

/** 固定种子伪随机：保证每帧元素位置稳定 */
function rng(seed: number) {
  let s = seed;
  return () => ((s = (s * 16807) % 2147483647) / 2147483647);
}

type Ctx = CanvasRenderingContext2D;
type Near = (x: number, y: number) => number;
interface DrawArgs { ctx: Ctx; w: number; h: number; near: Near; px: number; py: number; a: string; b: string; base: number }

const PAINTERS: Record<World, (d: DrawArgs) => void> = {
  // 石墨：稿纸点阵，每第 5 格一个稍大的点，像方格本
  graphite: ({ ctx, w, h, near, px, py, a, base }) => {
    const G = 20;
    ctx.fillStyle = a;
    for (let j = 0, y = G / 2; y < h; y += G, j++) for (let i = 0, x = G / 2; x < w; x += G, i++) {
      const k = near(x, y);
      const major = i % 5 === 0 && j % 5 === 0;
      ctx.globalAlpha = base * (major ? 1.6 : 1) + k * 0.35;
      ctx.beginPath();
      ctx.arc(x + (x - px) * k * 0.05, y + (y - py) * k * 0.05, (major ? 1.3 : 0.8) + k * 1.1, 0, Math.PI * 2);
      ctx.fill();
    }
  },
  // 森林：稀疏散落的小叶片（叶形 + 中脉），方向随机
  forest: ({ ctx, w, h, near, a, b, base }) => {
    const r = rng(11);
    const n = Math.round((w * h) / 5200);
    ctx.lineWidth = 1;
    for (let i = 0; i < n; i++) {
      const x = r() * w, y = r() * h, rot = r() * Math.PI * 2, s = 4 + r() * 4, accent = r() < 0.18;
      const k = near(x, y);
      ctx.save();
      ctx.translate(x, y); ctx.rotate(rot + k * 0.6);
      ctx.strokeStyle = accent ? b : a;
      ctx.globalAlpha = base * (accent ? 1.4 : 1) + k * 0.4;
      ctx.beginPath();
      ctx.moveTo(-s, 0); ctx.quadraticCurveTo(0, -s * 0.7, s, 0); ctx.quadraticCurveTo(0, s * 0.7, -s, 0);
      ctx.moveTo(-s * 0.7, 0); ctx.lineTo(s * 0.8, 0);
      ctx.stroke();
      ctx.restore();
    }
  },
  // 篝火：大小不一的余烬星点，少量带十字微光，靠下更密（像火星往上飘散）
  campfire: ({ ctx, w, h, near, px, py, a, b, base }) => {
    const r = rng(29);
    const n = Math.round((w * h) / 2600);
    for (let i = 0; i < n; i++) {
      const x = r() * w, y = Math.pow(r(), 0.7) * h, sz = r(), accent = r() < 0.25;
      const k = near(x, y);
      const dx = (x - px) * k * 0.05, dy = (y - py) * k * 0.05 - k * 4;
      ctx.globalAlpha = base * (0.6 + sz) + k * 0.4;
      ctx.fillStyle = accent ? b : a;
      ctx.beginPath(); ctx.arc(x + dx, y + dy, 0.6 + sz * 1.2 + k, 0, Math.PI * 2); ctx.fill();
      if (sz > 0.9) {
        ctx.strokeStyle = accent ? b : a; ctx.lineWidth = 0.8;
        ctx.beginPath();
        ctx.moveTo(x + dx - 3, y + dy); ctx.lineTo(x + dx + 3, y + dy);
        ctx.moveTo(x + dx, y + dy - 3); ctx.lineTo(x + dx, y + dy + 3);
        ctx.stroke();
      }
    }
  },
  // 煦风：长短不一的水平风线，末端微微上卷，错落排布
  breeze: ({ ctx, w, h, near, a, b, base }) => {
    const r = rng(47);
    ctx.lineWidth = 1; ctx.lineCap = "round";
    for (let y = 18; y < h; y += 26) {
      let x = -r() * 120;
      while (x < w) {
        const len = 40 + r() * 110, gap = 30 + r() * 90, accent = r() < 0.15;
        const k = near(x + len / 2, y);
        const lift = Math.sin((x + y) / 90) * 2 - k * 5;
        ctx.strokeStyle = accent ? b : a;
        ctx.globalAlpha = base * (accent ? 1.3 : 1) + k * 0.35;
        ctx.beginPath();
        ctx.moveTo(x, y);
        ctx.bezierCurveTo(x + len * 0.4, y + lift, x + len * 0.8, y - lift, x + len, y - 1.5);
        if (r() < 0.35) ctx.arc(x + len, y - 4, 2.5, Math.PI / 2, -Math.PI, true);
        ctx.stroke();
        x += len + gap;
      }
    }
  },
  // 潮汐：成组的同心潮纹弧（像沙滩上退潮留下的波痕）
  tide: ({ ctx, w, h, near, a, b, base }) => {
    const r = rng(83);
    const n = Math.round((w * h) / 26000);
    ctx.lineWidth = 1;
    for (let i = 0; i < n; i++) {
      const cx = r() * w, cy = r() * h, R = 14 + r() * 18, accent = r() < 0.2;
      const k = near(cx, cy);
      ctx.strokeStyle = accent ? b : a;
      for (let ring = 0; ring < 3; ring++) {
        ctx.globalAlpha = (base + k * 0.35) * (1 - ring * 0.25);
        ctx.beginPath();
        ctx.arc(cx, cy, R + ring * 6 + k * 4, Math.PI * 1.1, Math.PI * 1.9);
        ctx.stroke();
      }
    }
  },
  // 樱时：零星飘落的五瓣小花与单片花瓣
  sakura: ({ ctx, w, h, near, a, b, base }) => {
    const r = rng(101);
    const n = Math.round((w * h) / 9000);
    for (let i = 0; i < n; i++) {
      const x = r() * w, y = r() * h, rot = r() * Math.PI * 2, s = 2.6 + r() * 2.4, flower = r() < 0.3;
      const k = near(x, y);
      ctx.save();
      ctx.translate(x, y); ctx.rotate(rot + k * 0.8);
      ctx.fillStyle = flower ? b : a;
      ctx.globalAlpha = base * (flower ? 1.5 : 1) + k * 0.4;
      const petal = () => { ctx.beginPath(); ctx.ellipse(0, -s, s * 0.55, s, 0, 0, Math.PI * 2); ctx.fill(); };
      if (flower) for (let p = 0; p < 5; p++) { petal(); ctx.rotate((Math.PI * 2) / 5); }
      else petal();
      ctx.restore();
    }
  },
};

export function HomeTexture() {
  const ref = useRef<HTMLCanvasElement>(null);
  const world = useThemeStore((s) => s.world);
  const theme = useThemeStore((s) => s.theme);

  useEffect(() => {
    const cv = ref.current;
    if (!cv) return;
    const ctx = cv.getContext("2d");
    if (!ctx) return;
    const paint = PAINTERS[world] ?? PAINTERS.graphite;
    const reduce = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    let px = -9999, py = -9999, raf = 0;
    /** 运动期收到的尺寸变化（只记账，结束后重画一次） */
    let resizePending = false;
    /** 补画兜底：PerfBus 的订阅只在「忙碌翻转」时通知，若某条路径只写了 DOM 类
     *  而未登记（bus 的 DOM 兜底路径），订阅收不到事件。这里用一次廉价的 rAF 自检
     *  保证待画标记一定被消费——isBusy() 只读 classList/属性，不触发强制布局。 */
    let flushRaf = 0;
    const flushWhenIdle = () => {
      if (flushRaf) return;
      flushRaf = requestAnimationFrame(() => {
        flushRaf = 0;
        if (!resizePending) return;
        if (isBusy()) { flushWhenIdle(); return; }
        resize();
      });
    };
    const css = getComputedStyle(document.documentElement);
    // 纹理墨色：中性墨为主，强调色只做点缀；透明度在 canvas 内另行控制
    const a = css.getPropertyValue("--text-2").trim() || "#888";
    const b = css.getPropertyValue("--accent").trim() || a;

    const near: Near = (x, y) => {
      const d = Math.hypot(x - px, y - py);
      return d > RADIUS ? 0 : 1 - (d / RADIUS) ** 2;
    };

    function draw() {
      const w = cv!.clientWidth, h = cv!.clientHeight;
      ctx!.clearRect(0, 0, w, h);
      paint({ ctx: ctx!, w, h, near, px, py, a, b, base: theme === "dark" ? 0.13 : 0.1 });
      ctx!.globalAlpha = 1;
    }

    const resize = () => {
      // plan-20261020-retry-cred-perf D12：运动期（拖分隔条 / 拖窗口 / 面板过渡）跳过
      // 「重建画布 + 重画上千个图元」，只记一笔；运动结束时由 PerfBus 订阅补一次，
      // 保证终态尺寸与纹理一定画到位，不丢最后一帧。
      if (isBusy()) { resizePending = true; flushWhenIdle(); return; }
      resizePending = false;
      const r = cv.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      cv.width = r.width * dpr; cv.height = r.height * dpr;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      draw();
    };

    const host = cv.parentElement!;
    const onMove = (e: PointerEvent) => {
      if (reduce) return;
      const r = cv.getBoundingClientRect();
      px = e.clientX - r.left; py = e.clientY - r.top;
      if (!raf) raf = requestAnimationFrame(() => { raf = 0; draw(); });
    };
    const onLeave = () => { px = py = -9999; draw(); };
    const ro = new ResizeObserver(resize);
    ro.observe(cv);
    // PerfBus 只在「忙碌 → 空闲」翻转时通知：正好是补画最后一帧的时机
    const offBusy = subscribe((busy) => { if (!busy && resizePending) resize(); });
    host.addEventListener("pointermove", onMove);
    host.addEventListener("pointerleave", onLeave);
    return () => {
      ro.disconnect(); cancelAnimationFrame(raf); cancelAnimationFrame(flushRaf); offBusy();
      host.removeEventListener("pointermove", onMove);
      host.removeEventListener("pointerleave", onLeave);
    };
  }, [world, theme]);

  return (
    <canvas
      ref={ref}
      aria-hidden
      className="pointer-events-none absolute inset-0 -z-10 h-full w-full"
      style={{ maskImage: "radial-gradient(80% 70% at 50% 40%, #000 30%, transparent 100%)" }}
    />
  );
}
