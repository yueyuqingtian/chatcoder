/**
 * 性能度量护栏（plan-329-1647 S12）。
 *
 * 目标：把「四项操作（拖分隔条 / 折叠展开 / 窗口边缘 resize / 拖标题栏）是否达标」
 * 从人工观感变成可读数字，并为「玻璃不是主要开销」这一判断留可核对的依据。
 *
 * 采集内容（全部零依赖的浏览器 API）：
 *   · 帧间隔分布（rAF 采样，输出 p50 / p95 / max）；
 *   · longtask 数量与总时长（PerformanceObserver('longtask')）；
 *   · 收敛序列耗时（reconcile 每次执行 start → end）。
 *
 * 开销控制：**默认不采集**。帧采样需显式 start()（常驻 rAF 有成本）；longtask 观察仅在
 * start() 后注册、stop() 时断开；recordReconcile 在未采集时只是一次函数调用 + 判断。
 *
 * 用法（dev 构建，DevTools 控制台）：
 *   __chatcoderPerf.start();      // 开始采集
 *   ...执行被测操作（例如拖分隔条 3~5 秒）...
 *   __chatcoderPerf.snapshot();   // 读取报告（帧间隔 / longtask / 收敛耗时）
 *   __chatcoderPerf.stop();       // 停止
 *   __chatcoderPerf.reset();      // 清空统计，便于下一场景
 *
 * 约定：不使用被项目规则禁止的调试端口命令；本模块纯渲染层，无 IPC、无依赖。
 */

export interface FrameStats {
  count: number;
  p50: number;
  p95: number;
  max: number;
}

export interface PerfSnapshot {
  /** 采集时长（ms；自上次 start/reset 起算） */
  elapsed: number;
  frames: FrameStats;
  longtasks: { count: number; totalMs: number; maxMs: number };
  reconcile: { count: number; lastMs: number; p95Ms: number; maxMs: number };
}

const frames: number[] = [];
const reconcileMs: number[] = [];

let longtaskCount = 0;
let longtaskTotal = 0;
let longtaskMax = 0;

let sampling = false;
let rafId = 0;
let lastFrameTs = 0;
let startedAt = 0;
let longtaskObserver: PerformanceObserver | null = null;

/** 四舍五入到 0.1ms（报告里不必给更高精度）。 */
function r1(v: number): number {
  return Math.round(v * 10) / 10;
}

function percentile(sorted: number[], p: number): number {
  if (sorted.length === 0) return 0;
  const idx = Math.min(sorted.length - 1, Math.max(0, Math.round((p / 100) * (sorted.length - 1))));
  return sorted[idx];
}

/** 帧采样循环（仅 start() 后运行）。 */
function frameLoop(ts: number): void {
  if (!sampling) return;
  if (lastFrameTs > 0) frames.push(ts - lastFrameTs);
  lastFrameTs = ts;
  rafId = requestAnimationFrame(frameLoop);
}

/** 开始采集：帧采样 + longtask observer。重复调用无副作用。 */
export function start(): void {
  if (sampling) return;
  sampling = true;
  startedAt = performance.now();
  lastFrameTs = 0;
  rafId = requestAnimationFrame(frameLoop);
  if (typeof PerformanceObserver !== "undefined") {
    try {
      longtaskObserver = new PerformanceObserver((list) => {
        for (const e of list.getEntries()) {
          longtaskCount += 1;
          longtaskTotal += e.duration;
          if (e.duration > longtaskMax) longtaskMax = e.duration;
        }
      });
      longtaskObserver.observe({ entryTypes: ["longtask"] });
    } catch {
      /* 浏览器不支持 longtask：本次仅缺该项统计，其余照常 */
    }
  }
}

/** 停止采集（保留已采集数据，便于 stop 后再 snapshot）。 */
export function stop(): void {
  sampling = false;
  if (rafId) { cancelAnimationFrame(rafId); rafId = 0; }
  try { longtaskObserver?.disconnect(); } catch { /* ignore */ }
  longtaskObserver = null;
}

/** 清空统计（不改变采集开关）。 */
export function reset(): void {
  frames.length = 0;
  reconcileMs.length = 0;
  longtaskCount = 0;
  longtaskTotal = 0;
  longtaskMax = 0;
  startedAt = performance.now();
  lastFrameTs = 0;
}

/** 由收敛序列调用：记录一次收敛耗时（未采集时近似零开销）。 */
export function recordReconcile(ms: number): void {
  if (!sampling) return;
  reconcileMs.push(ms);
}

/** 读取当前报告（不停止采集）。 */
export function snapshot(): PerfSnapshot {
  const f = [...frames].sort((a, b) => a - b);
  const r = [...reconcileMs].sort((a, b) => a - b);
  return {
    elapsed: startedAt ? Math.round(performance.now() - startedAt) : 0,
    frames: {
      count: f.length,
      p50: r1(percentile(f, 50)),
      p95: r1(percentile(f, 95)),
      max: r1(f.length ? f[f.length - 1] : 0),
    },
    longtasks: {
      count: longtaskCount,
      totalMs: Math.round(longtaskTotal),
      maxMs: Math.round(longtaskMax),
    },
    reconcile: {
      count: r.length,
      lastMs: r1(reconcileMs.length ? reconcileMs[reconcileMs.length - 1] : 0),
      p95Ms: r1(percentile(r, 95)),
      maxMs: r1(r.length ? r[r.length - 1] : 0),
    },
  };
}

/** 是否正在采集（诊断用）。 */
export function isSampling(): boolean {
  return sampling;
}

/** 挂到 window.__chatcoderPerf（仅 dev 构建由 main.tsx 调用；生产不挂载 ⇒ 零开销）。 */
export function install(): void {
  if (typeof window === "undefined") return;
  (window as unknown as { __chatcoderPerf?: unknown }).__chatcoderPerf = {
    start, stop, reset, snapshot,
    get sampling() { return sampling; },
    /** 便捷输出：直接打印一份快照（避免每次手写 console.log(snapshot())） */
    report() {
      const s = snapshot();
      // eslint-disable-next-line no-console
      console.table({
        "帧数": [s.frames.count],
        "帧间隔 p50(ms)": [s.frames.p50],
        "帧间隔 p95(ms)": [s.frames.p95],
        "帧间隔 max(ms)": [s.frames.max],
        "长任务数": [s.longtasks.count],
        "长任务总时长(ms)": [s.longtasks.totalMs],
        "收敛次数": [s.reconcile.count],
        "收敛 p95(ms)": [s.reconcile.p95Ms],
        "收敛 max(ms)": [s.reconcile.maxMs],
        "采集时长(s)": [Math.round(s.elapsed / 1000)],
      });
      return s;
    },
  };
}
