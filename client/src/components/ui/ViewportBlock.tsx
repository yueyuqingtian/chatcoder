import { memo, useEffect, useRef, useState, type ReactNode } from "react";
import { isBusy, subscribe } from "../../perf/bus";

type Watch = (visible: boolean) => void;
const observers = new WeakMap<Element, { observer: IntersectionObserver; callbacks: Map<Element, Watch> }>();

/** plan-20261101-performance：轮内块共用外层滚动容器和一个观察器，避免嵌套滚动。 */
export const ViewportBlock = memo(function ViewportBlock({ children, estimate = 160, inline = false, tableColumns }: {
  children: ReactNode; estimate?: number; inline?: boolean; tableColumns?: number;
}) {
  const ref = useRef<HTMLElement | null>(null);
  const [visible, setVisible] = useState(false);
  const height = useRef(estimate);
  const wanted = useRef(false);
  const [pinned, setPinned] = useState(false);
  useEffect(() => {
    const el = ref.current;
    const root = el?.closest(".message-flow");
    if (!el || !root || typeof IntersectionObserver === "undefined") { setVisible(true); return; }
    let shared = observers.get(root);
    if (!shared) {
      const callbacks = new Map<Element, Watch>();
      const observer = new IntersectionObserver((entries) => {
        for (const entry of entries) callbacks.get(entry.target)?.(entry.isIntersecting);
      }, { root, rootMargin: "900px 0px" });
      shared = { observer, callbacks };
      observers.set(root, shared);
    }
    const sync = () => { if (!isBusy()) setVisible(wanted.current); };
    shared.callbacks.set(el, (value) => { wanted.current = value; sync(); });
    shared.observer.observe(el);
    const off = subscribe((busy) => { if (!busy) sync(); });
    const selection = () => {
      const range = document.getSelection();
      setPinned(Boolean(range && !range.isCollapsed && range.rangeCount && range.getRangeAt(0).intersectsNode(el)));
    };
    document.addEventListener("selectionchange", selection);
    return () => {
      shared.callbacks.delete(el);
      shared.observer.unobserve(el);
      if (!shared.callbacks.size) { shared.observer.disconnect(); observers.delete(root); }
      off();
      document.removeEventListener("selectionchange", selection);
    };
  }, []);
  const mounted = visible || pinned;
  useEffect(() => {
    const el = ref.current;
    if (!el || !mounted) return;
    const ro = new ResizeObserver(([entry]) => {
      const size = entry?.borderBoxSize?.[0]?.blockSize ?? entry?.contentRect.height;
      if (size && size > 0) height.current = size;
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [mounted]);
  const Tag = tableColumns ? "tbody" : inline ? "span" : "div";
  return <Tag ref={(el) => { ref.current = el; }} className="viewport-block"
    style={mounted ? undefined : { height: height.current }} data-mounted={mounted || undefined}>
    {mounted ? children : tableColumns ? <tr aria-hidden="true"><td colSpan={tableColumns} style={{ height: height.current, padding: 0, border: 0 }} /></tr> : null}
  </Tag>;
});
