/**
 * PerfBus —— 渲染层唯一的「性能敏感期」状态源（plan-329-1647 S3）。
 *
 * ── 为什么需要它 ──
 * 几何变化期间（拖标题栏改窗口位置、拖窗口边缘改尺寸、拖分隔条改面板宽度、面板折叠过渡）
 * 需要统一暂停一批重活：强制同步布局读取、位置补偿、流式推进、虚拟列表测量……
 * 但这些判断此前散落在 8~9 处，各自去读 `document.body.classList.contains("panel-dragging")`
 * 或 `window.__chatcoderWindowMotion`，而且**口径还不一致**（例如 TerminalPanel 与
 * ComposerCore 认「拖分隔条」和「窗口运动」，却不认「面板折叠过渡 / panel-animating」）。
 * 分散判断的代价是双重的：漏掉一处就会在运动期继续做重活；也无法统一观测与回归。
 *
 * ── 现在 ──
 * 三个来源全部经 acquire/release 登记：
 *   · `window-motion` —— 主进程下发的窗口几何运动（IPC window:motion → App.tsx）
 *   · `panel-drag`    —— 分隔条拖拽中（ResizeHandle）
 *   · `panel-anim`    —— 面板折叠/展开过渡中（App.tsx）
 * 对外只暴露 `isBusy()` / `reasons()` / `subscribe()`，调用方不再各自读 DOM。
 *
 * ── DOM 侧不变 ──
 * `body.panel-dragging`、`body.panel-animating` 与 `html[data-window-motion="1"]` 仍由各自的
 * 所有者写入：CSS 的运动期降级规则（暂停循环动画、禁用面板宽度过渡）依赖它们。
 * 本模块只**读取**它们作为兜底——万一某条路径忘记登记，也不会静默退回「静止态」做重活。
 *
 * ── 性能注意 ──
 * `isBusy()` 会被逐帧调用（rAF / ResizeObserver 回调里）。它只做 classList.contains 与
 * getAttribute 读取——这类读取**不会**触发强制同步布局（真正贵的是 clientWidth /
 * scrollHeight / getBoundingClientRect），所以逐帧调用是安全的。
 */

export type BusyReason = "window-motion" | "panel-drag" | "panel-anim";

const REASONS: readonly BusyReason[] = ["window-motion", "panel-drag", "panel-anim"];

/** 各来源的引用计数（同一来源可能被多处同时持有，例如左右面板过渡交叉）。 */
const counts: Record<BusyReason, number> = {
  "window-motion": 0,
  "panel-drag": 0,
  "panel-anim": 0,
};

/** 主进程窗口运动标记的内存镜像（App.tsx 收到 IPC 后同步写入）。 */
let windowMotionFlag = false;

type Listener = (busy: boolean, reasons: BusyReason[]) => void;
const listeners = new Set<Listener>();
let lastNotifiedBusy = false;

function domSaysBusy(reason: BusyReason): boolean {
  if (typeof document === "undefined") return false;
  try {
    if (reason === "window-motion") {
      return windowMotionFlag
        || Boolean((window as unknown as { __chatcoderWindowMotion?: boolean }).__chatcoderWindowMotion)
        || document.documentElement.getAttribute("data-window-motion") === "1";
    }
    if (reason === "panel-drag") return document.body.classList.contains("panel-dragging");
    return document.body.classList.contains("panel-animating");
  } catch {
    return false;
  }
}

/** 当前活跃原因（登记计数 > 0 或 DOM 兜底显示为真）。 */
export function reasons(): BusyReason[] {
  const out: BusyReason[] = [];
  for (const r of REASONS) {
    if (counts[r] > 0 || domSaysBusy(r)) out.push(r);
  }
  return out;
}

/** 是否处于任何性能敏感期（几何变化中）。 */
export function isBusy(): boolean {
  return reasons().length > 0;
}

/** 立即判断指定原因是否活跃（调用方语义更精确时使用）。 */
export function isReasonActive(reason: BusyReason): boolean {
  return counts[reason] > 0 || domSaysBusy(reason);
}

function notify(): void {
  const active = reasons();
  const busy = active.length > 0;
  if (busy === lastNotifiedBusy) return;
  lastNotifiedBusy = busy;
  for (const fn of Array.from(listeners)) {
    try { fn(busy, active); } catch { /* 单个监听器异常不影响其它 */ }
  }
}

/** 订阅「进入/离开性能敏感期」。返回取消订阅函数。 */
export function subscribe(fn: Listener): () => void {
  listeners.add(fn);
  return () => { listeners.delete(fn); };
}

/** 登记一个原因（成对 acquire/release）。 */
export function acquire(reason: BusyReason): void {
  counts[reason] += 1;
  notify();
}

/** 归还一个原因。计数不会被减到负数（多余 release 被忽略）。 */
export function release(reason: BusyReason): void {
  if (counts[reason] > 0) counts[reason] -= 1;
  notify();
}

/**
 * 主进程窗口运动标记（由 App.tsx 的 window:motion 处理器调用）。
 * 这是幂等的状态设置，不走计数——主进程只会在真实变化时下发一次。
 */
export function setWindowMotion(active: boolean): void {
  windowMotionFlag = active;
  notify();
}

/** 仅供自测/回归读取（dev 用），不参与业务逻辑。 */
export function debugState(): { counts: Record<BusyReason, number>; busy: boolean; domReasons: BusyReason[] } {
  return {
    counts: { ...counts },
    busy: isBusy(),
    domReasons: REASONS.filter(domSaysBusy),
  };
}
