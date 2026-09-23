/**
 * 几何过渡底座（plan-31-152 S5-1 重写：单一权威动画路径）。
 *
 * ── 为什么放弃 View Transition（用户反馈"右侧面板从上往下铺、中间先缩再铺"）──
 * plan-329-1647 引入的 `document.startViewTransition` 快照动画在折叠/展开场景下有三处
 * 不可控行为，正是「动画方向/时序异常」的直接来源：
 *   · root 快照：整页快照做交叉淡化，与命名元素快照叠加后观感是"先整体动一下再局部铺开"；
 *   · 折叠态 `.app-pane-right.collapsed` 带 `visibility:hidden` —— 按规范该元素**不生成旧快照**，
 *     于是展开时只有新快照（无起始几何可比对），默认动画退化为淡入/缩放，不是横向展开；
 *   · 快照尺寸插值只缩放旧画面，与真实布局（文本折行、中列宽度）不同步 ⇒ 中间列先缩、
 *     右面板随后"铺"出来，看起来是两段动画。
 *
 * ── 现在：单一 CSS 过渡（唯一权威路径）──
 * 布局变更一次性提交，横向展开/收窄由 `.app-pane` 自身的
 * `transition: width, flex-basis` 完成（见 global.css）。中间列是 flex 自动跟随，
 * 与左右面板**同一帧起止**，只有一条横向动画，没有快照、没有上下波动。
 * 过渡期间仍登记 PerfBus 的 `panel-anim`（供消息流/终端等跳过逐帧重活）。
 *
 * ── 收尾 ──
 * 时长取 `--dur-pane` 的实际计算值（无法计算时回退 200ms），到点后：
 * 释放 panel-anim → 广播 `chatcoder:panel-drag-end` → 触发唯一收敛序列（runReconcile）。
 * `prefers-reduced-motion` / `motionLevel` 为 reduced|off 时立即提交终态并收尾（无动画路径）。
 */

import { acquire, release } from "./bus";
import { runReconcile } from "./reconcile";
import { useUiStore } from "../store/ui";

/** 面板过渡时长（毫秒）：读 `--dur-pane` 的计算值；解析失败回退 200ms。
 *
 *  注意：未注册的自定义属性经 getComputedStyle 返回的是**原始 token 串**
 *  （例如 `calc(180ms * var(--motion-scale))`），因此这里用正则抓其中的 ms 数值。
 *  取不到时再退到 `--dur-med` / 200ms，保证收尾一定发生。 */
function paneTransitionMs(): number {
  if (typeof window === "undefined") return 0;
  try {
    const cs = getComputedStyle(document.documentElement);
    for (const name of ["--dur-pane", "--dur-med", "--dur-3"]) {
      const raw = cs.getPropertyValue(name).trim();
      if (!raw) continue;
      const ms = /([\d.]+)ms/.exec(raw);
      if (ms) return Math.max(0, Math.round(parseFloat(ms[1])));
      const s = /([\d.]+)s/.exec(raw);
      if (s) return Math.max(0, Math.round(parseFloat(s[1]) * 1000));
    }
  } catch { /* 读不到样式时用回退值 */ }
  return 200;
}

/** 是否使用过渡动画（能力 + 用户动效偏好）。
 *  名字保留以兼容既有引用；语义已由"是否支持快照过渡"收窄为"是否播放面板过渡"。 */
export function supportsPaneViewTransition(): boolean {
  if (typeof window === "undefined" || typeof document === "undefined") return false;
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
 *   · 布局一次性提交，横向过渡交给 `.app-pane` 的 CSS transition；
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

  apply();

  if (!supportsPaneViewTransition()) {
    finish(); // 无动画路径：直接达终态并收敛
    return;
  }
  // 过渡结束的收尾。用一次定时器即可：CSS 过渡时长是确定的（--dur-pane），
  // 不需要 transitionend（多条属性会触发多次，反而要额外去重）。
  // 额外 +40ms 余量，确保浏览器已完成最后一次布局。
  window.setTimeout(finish, paneTransitionMs() + 40);
}
