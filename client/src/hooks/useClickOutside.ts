/** 点击目标元素外部时关闭弹层（plan-246-1236 S2）。
 * 捕获阶段监听 pointerdown，避免内部 stopPropagation 把关闭吞掉。 */
import { useEffect, type RefObject } from "react";

export function useClickOutside(
  ref: RefObject<HTMLElement | null>,
  enabled: boolean,
  onClose: () => void,
): void {
  useEffect(() => {
    if (!enabled) return;
    const handler = (e: PointerEvent) => {
      const el = ref.current;
      if (!el) return;
      if (el.contains(e.target as Node)) return;
      onClose();
    };
    document.addEventListener("pointerdown", handler, true);
    return () => document.removeEventListener("pointerdown", handler, true);
  }, [ref, enabled, onClose]);
}
