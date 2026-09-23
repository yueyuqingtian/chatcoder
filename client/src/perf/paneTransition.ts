/**
 * 几何过渡底座（plan-329-1647 S7）。
 *
 * ── 为什么不再用「CSS width 过渡 + 冻结内容根」──
 * 旧方案（App.tsx 的 beginPaneAnimation）在 180ms 内逐帧改面板宽度，同时把消息列内容根
 * 的宽度钉死以省掉逐帧折行。代价有两个，都是用户直接反馈的观感问题：
 *   · 过渡期间内容被裁切（宽度钉住、面板在变）；
 *   · 结束瞬间内容按终宽一次性重排 ⇒ 一次集中尖峰。
 *
 * ── 现在：View Transition（合成器快照动画）──
 * 布局变更**一次性到位**（flushSync 内完成），浏览器对同名元素（见 global.css 的
 * `view-transition-name`）做尺寸/位置插值——动画跑在合成器上，主线程逐帧零布局。
 * 过渡期间 `html.vt-pane` 会关掉 `.app-pane` 自身的 CSS 过渡（否则"新状态"是过渡中间值，
 * 快照对比会失准）。
 *
 * ── 回退 ──
 * 能力缺失（老 Chromium）或用户要求减少动效（prefers-reduced-motion / motionLevel 为
 * reduced|off）时，直接应用变更、不做快照动画——CSS width 过渡仍在（global.css:42），
 * 即退化为旧行为。
 */

import { flushSync } from "react-dom";
import { acquire, release } from "./bus";
import { runReconcile } from "./reconcile";
import { useUiStore } from "../store/ui";

interface ViewTransitionLike {
  finished: Promise<void>;
  updateCallbackDone?: Promise<void>;
}

function startViewTransitionSafe(cb: () => void): ViewTransitionLike | null {
  if (typeof document === "undefined") return null;
  const doc = document as Document & { startViewTransition?: (cb: () => void) => ViewTransitionLike };
  if (typeof doc.startViewTransition !== "function") return null;
  try {
    return doc.startViewTransition(cb);
  } catch {
    return null;
  }
}

/** 是否可用快照动画（能力 + 用户动效偏好）。 */
export function supportsPaneViewTransition(): boolean {
  if (typeof document === "undefined") return false;
  const doc = document as Document & { startViewTransition?: unknown };
  if (typeof doc.startViewTransition !== "function") return false;
  try {
    if (typeof window.matchMedia === "function"
      && window.matchMedia("(prefers-reduced-motion: reduce)").matches) return false;
    const lvl = useUiStore.getState().motionLevel;
    if (lvl === "reduced" || lvl === "off") return false;
  } catch { /* 读不到偏好时按可用处理 */ }
  return true;
}

/**
 * 应用一次「面板几何变更」（折叠/展开/全屏切换），并统一处理收尾：
 *   · 进入 panel-anim 性能敏感期（PerfBus）→ 运动期门控自动生效；
 *   · 优先走 View Transition（合成器动画，布局一次性到位）；
 *   · 结束后释放门控、派发 chatcoder:panel-drag-end、触发唯一收敛序列（RFL-6）。
 */
export function applyPaneUpdate(apply: () => void): void {
  acquire("panel-anim");
  let done = false;
  const finish = () => {
    if (done) return;
    done = true;
    release("panel-anim");
    // 与拖分隔条收尾同口径：先摘状态、再广播、最后统一收敛。
    window.dispatchEvent(new CustomEvent("chatcoder:panel-drag-end"));
    runReconcile();
  };

  if (!supportsPaneViewTransition()) {
    apply();
    finish();
    return;
  }

  const root = document.documentElement;
  root.classList.add("vt-pane");
  const cleanup = () => {
    root.classList.remove("vt-pane");
    finish();
  };
  const vt = startViewTransitionSafe(() => {
    // flushSync：让 React 的布局变更在 update 回调内**同步**落地，
    // 否则浏览器会在还没改完 DOM 时就采集"新状态"快照。
    flushSync(apply);
  });
  if (!vt) {
    cleanup();
    return;
  }
  vt.finished.then(cleanup, cleanup);
  // 兜底：极少数路径 finished 不触发（例如紧接着又发起一次 VT 把本次抢占）。
  window.setTimeout(cleanup, 500);
}
