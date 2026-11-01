/**
 * 唯一收敛序列（plan-329-1647 S6 / RFL-6）。
 *
 * ── 问题 ──
 * 几何运动（拖分隔条 / 拖窗口 / 面板折叠过渡）结束时，有 8 处各自排队做收尾：
 *   ① 解冻右面板内容根
 *   ② 提交虚拟列表视口尺寸
 *   ③ 还原滚动锚点 / 贴底
 *   ④ 虚拟列表重新测量
 *   ⑤ 终端 fit（xterm 全缓冲重排）
 *   ⑥ 浏览器面板框架尺寸
 *   ⑦ 输入框高度 remeasure
 *   ⑧ 流式缓冲追平
 * 它们分散使用 rAF / 双 rAF / 16ms setTimeout，于是同一次"松手"被摊到好几个帧上——
 * 用户感受就是"松手后要顿一下才归位"。
 *
 * ── 现在 ──
 * 各消费者把收尾函数**注册**到本模块（带显式顺序 order），运动结束时由
 * `ResizeHandle`（拖拽收尾）或 `PerfBus` 订阅者调用一次 `runReconcile()`：
 * plan-20261101-performance：任务按 order 保序，以 4ms 预算分帧；几何提交后留出布局帧。
 *
 * ── 约束 ──
 * · 任务必须**幂等**且尽量快：同步任务不能被抢占，预算在任务之间生效。
 * · 单个任务抛异常不会影响其它任务（逐个 try/catch）。
 * · 重复调用 `runReconcile()` 会合并为一次（取消上一帧的排队）。
 */

import { recordReconcile } from "./metrics";
import { isBusy, subscribe } from "./bus";

export interface ReconcileEntry {
  order: number;
  label: string;
  fn: () => void;
}

const entries = new Map<string, ReconcileEntry>();
let raf = 0;
let generation = 0;
const FRAME_BUDGET_MS = 4;

/** plan-20261101-performance：新运动使旧布局任务失效，不能在下一次拖拽中继续追平。 */
export function cancelReconcile(): void {
  generation += 1;
  if (raf) cancelAnimationFrame(raf);
  raf = 0;
}

subscribe((busy) => { if (busy) cancelReconcile(); });

/** 注册一个收尾任务。返回注销函数（组件卸载时调用）。 */
export function registerReconcileTask(
  id: string,
  order: number,
  label: string,
  fn: () => void,
): () => void {
  entries.set(id, { order, label, fn });
  return () => {
    // 仅当仍是自己注册的那一项时才删除（避免后注册者被前者的清理误删）
    const cur = entries.get(id);
    if (cur && cur.fn === fn) entries.delete(id);
  };
}

/** 该任务是否已注册（诊断用）。 */
export function hasReconcileTask(id: string): boolean {
  return entries.has(id);
}

/**
 * 触发一次收敛：按顺序分帧执行已注册任务，运动开始时取消旧代次。
 * 重复调用合并为一次。
 */
export function runReconcile(): void {
  if (typeof requestAnimationFrame !== "function") return;
  cancelReconcile();
  const token = generation;
  const list = Array.from(entries.entries()).sort((a, b) => a[1].order - b[1].order);
  let cursor = 0;
  const step = () => {
    raf = 0;
    if (token !== generation || isBusy()) return;
    const t0 = performance.now();
    while (cursor < list.length) {
      const [id, e] = list[cursor++];
      // 已卸载或被新实例覆盖的任务不得执行旧闭包。
      if (entries.get(id) !== e) continue;
      try {
        e.fn();
      } catch {
        /* 单个任务异常不影响其它任务 */
      }
      if (token !== generation) return;
      // 几何提交后留一帧给 ResizeObserver，再做锚点和重型面板恢复。
      const next = list[cursor]?.[1];
      if (next && (performance.now() - t0 >= FRAME_BUDGET_MS
        || (e.order < RECONCILE_ORDER.scrollAnchor && next.order >= RECONCILE_ORDER.scrollAnchor))) break;
    }
    recordReconcile(performance.now() - t0);
    if (cursor < list.length) raf = requestAnimationFrame(step);
  };
  raf = requestAnimationFrame(step);
}

/** 是否有一次收敛已排队（诊断用）。 */
export function isReconcilePending(): boolean {
  return raf !== 0;
}

/** 收敛任务的顺序约定（供注册方引用，避免各处写魔法数字）。 */
export const RECONCILE_ORDER = {
  /** ① 解冻右面板内容根（ResizeHandle 自己执行，不注册） */
  unfreezePane: 10,
  /** ② 虚拟列表视口尺寸提交 */
  virtualizerRect: 20,
  /** ③ 滚动锚点还原 / 贴底（plan-31-151 S4：改为双帧后执行，等虚拟器按新 rect 重算完成） */
  scrollAnchor: 30,
  /** ④ 虚拟列表重新测量（plan-31-151 S4：已移除——directDomUpdates 模式下 RO 自动触发重测，
   *   手动调 virtualizer.measure() 会与 RO 竞争导致重叠） */
  virtualizerMeasure: 40,
  /** ⑤ 终端 fit */
  terminalFit: 50,
  /** ⑥ 浏览器面板框架尺寸 */
  browserFrameSize: 60,
  /** ⑦ 输入框 remeasure */
  composerRemeasure: 70,
  /** ⑧ 流式缓冲追平 */
  streamFlush: 80,
} as const;
