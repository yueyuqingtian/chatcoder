/** usePresence —— 自绘浮层 / 列表条目的进出场状态机（plan-20261009-ui U5）。
 *
 * 为什么需要：此前自绘浮层（输入框菜单、胶囊浮层、侧栏菜单）都是 `{open && <div/>}`，
 * 关闭即卸载，只有入场没有退场——关闭时「啪」地消失，与 Radix 浮层的退场观感割裂。
 *
 * 用法：
 *   const p = usePresence(open);
 *   {p.mounted && <div className="mo-pop" data-state={p.state} />}
 * 关闭时先把 data-state 置为 closed 播放退场，再在 exitMs 后卸载；
 * 「动画效果：关闭」档（--motion-scale = 0）或系统减少动态效果时立即卸载。
 */
import { useEffect, useRef, useState } from "react";

export function motionDisabled(): boolean {
  if (typeof document === "undefined") return true;
  const root = document.documentElement;
  if (root.getAttribute("data-motion") === "off") return true;
  return typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

export function usePresence(open: boolean, exitMs = 140): { mounted: boolean; state: "open" | "closed" } {
  const [mounted, setMounted] = useState(open);
  const [state, setState] = useState<"open" | "closed">(open ? "open" : "closed");
  const timerRef = useRef<number | null>(null);

  useEffect(() => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    if (open) {
      setMounted(true);
      setState("open");
      return;
    }
    setState("closed");
    if (motionDisabled()) {
      setMounted(false);
      return;
    }
    // 减弱档时长 ×0.6：退场等待同比缩短（读不到系数时按原值）
    const scale = Number(getComputedStyle(document.documentElement).getPropertyValue("--motion-scale")) || 1;
    timerRef.current = window.setTimeout(() => {
      timerRef.current = null;
      setMounted(false);
    }, Math.max(0, exitMs * scale));
  }, [open, exitMs]);

  useEffect(() => () => {
    if (timerRef.current !== null) window.clearTimeout(timerRef.current);
  }, []);

  return { mounted, state };
}
