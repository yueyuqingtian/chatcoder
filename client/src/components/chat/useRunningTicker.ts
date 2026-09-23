/**
 * 共享运行态 ticker（plan-329-1647 S8c / FlowEngine）。
 *
 * ── 问题 ──
 * 运行期间有三处各自独立的秒级 `setInterval`：
 *   · ToolTree（1200ms）——工具行的耗时文本；
 *   · TurnGroup 的 WorkTimer（1000ms）——当前 turn 的已用时；
 *   · SubagentPanel（1000ms）——子代理运行时长。
 * 每个定时器**各自**唤醒并**各自**触发子树重渲染。会话运行期间它们与流式刷新、几何动画
 * 抢同一份帧预算，而且不少时候只是为了让「12s → 13s」这一个数字变一下。
 *
 * ── 现在 ──
 * 单例 `setInterval` + 订阅表：所有需要秒级节拍的组件共用一个定时器（天然对齐、同时唤醒）。
 * 两条使用路径：
 *   · `subscribeTick(fn)` —— 不触发 React 重渲染，适合"把时间文本直接写进 DOM"的调用方；
 *   · `useRunningTicker(enabled)` —— 返回节拍计数，适合时间文本走 React 内容的调用方
 *     （组件通常在 enabled=false 时自动退订，定时器随之停止，静止期零唤醒）。
 */

import { useEffect, useState } from "react";

type Listener = (now: number) => void;

const listeners = new Set<Listener>();
let timer = 0;
const TICK_MS = 1000;

function startTimer(): void {
  if (timer) return;
  timer = window.setInterval(() => {
    const now = Date.now();
    for (const fn of Array.from(listeners)) {
      try { fn(now); } catch { /* 单个订阅者异常不影响其它 */ }
    }
  }, TICK_MS);
}

function stopTimer(): void {
  if (!timer) return;
  window.clearInterval(timer);
  timer = 0;
}

/** 订阅共享节拍（不触发 React 重渲染）。返回取消订阅函数。 */
export function subscribeTick(fn: Listener): () => void {
  listeners.add(fn);
  startTimer();
  return () => {
    listeners.delete(fn);
    if (listeners.size === 0) stopTimer();
  };
}

/** 共享节拍的 React 版本：enabled=false 时不订阅，定时器自动停摆（静止期零唤醒）。 */
export function useRunningTicker(enabled: boolean): number {
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (!enabled) return;
    return subscribeTick(() => setTick((v) => v + 1));
  }, [enabled]);
  return tick;
}
