/** 内容宽度边缘拖拽（plan-20261017-ux-overhaul 阶段 C）。
 *
 * 交互：鼠标停在消息列左 / 右边缘满 1 秒，边缘淡入一根拖拽柄；按住拖动即对称调整
 * 消息与输入框的最大宽度，松手写回「设置 → 外观 → 内容展示宽度」（同一字段，两处双向同步）。
 * 为什么要停留 1 秒：边缘是阅读时鼠标常经过的位置，悬停即出现会频繁闪烁打扰。
 *
 * 性能：指针移动只做 rAF 合帧的几何判断，不触发 React 渲染；拖拽期间直接写根节点的
 * --content-max-w，松手才 setPrefs 一次，避免每帧保存偏好与整树重渲染。
 */
import { useEffect, useRef, useState, type RefObject } from "react";
import { useUiStore } from "../../store/ui";

/** 触发区：消息列外框向外 EDGE_OUT，向内覆盖整段内边距（文字与外框之间的留白，约 32px）。
 *  只按外框 ±8px 判定时，鼠标停在文字边缘附近恰好落在区外，几乎触发不了。 */
const EDGE_OUT = 12;
const DWELL_MS = 1000;
const MIN_W = 560;
const MAX_W = 1600;

type Side = "left" | "right";

export function ContentWidthResizer({ hostRef }: { hostRef: RefObject<HTMLElement> }) {
  const [handle, setHandle] = useState<{ side: Side; x: number; top: number; height: number } | null>(null);
  const [dragging, setDragging] = useState(false);
  // 拖拽态同时放 ref：指针事件回调读 ref，避免旧闭包里的 dragging=false 在拖动中把柄隐藏
  const draggingRef = useRef(false);
  const dwell = useRef<{ side: Side; timer: number } | null>(null);
  const frame = useRef(0);

  /** 隐藏拖拽柄并清掉悬停计时：鼠标仍停在边缘时会重新计时，而不是永远停在「已触发」状态 */
  const hideHandle = () => {
    if (dwell.current) window.clearTimeout(dwell.current.timer);
    dwell.current = null;
    setHandle(null);
  };

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;

    // 边界缓存 500ms：流式输出时每帧读 getBoundingClientRect 会强制同步布局，
    // 而消息列左右边界只随窗口 / 面板宽度变化，短时间内复用即可
    let cache: { col: DOMRect | null; host: DOMRect; inset: number; at: number } | null = null;
    const rects = () => {
      const now = performance.now();
      if (!cache || now - cache.at > 500) {
        const list = host.querySelector<HTMLElement>(".chat-panel-flow .mf-list");
        cache = {
          col: list?.getBoundingClientRect() ?? null,
          host: host.getBoundingClientRect(),
          inset: list ? Math.max(12, parseFloat(getComputedStyle(list).paddingLeft) || 0) : 12,
          at: now,
        };
      }
      return cache;
    };
    const columnRect = () => rects().col;
    const clearDwell = () => {
      if (dwell.current) window.clearTimeout(dwell.current.timer);
      dwell.current = null;
    };

    const evaluate = (x: number, y: number) => {
      const { col, host: hostRect, inset } = rects();
      if (!col || col.width <= 0) { clearDwell(); setHandle(null); return; }
      // 铺满内容区时左右边缘贴着面板边框，不提供拖拽（与左右面板分隔条冲突）
      const side: Side | null =
        x >= col.left - EDGE_OUT && x <= col.left + inset && col.left - hostRect.left > EDGE_OUT * 2 ? "left"
          : x <= col.right + EDGE_OUT && x >= col.right - inset && hostRect.right - col.right > EDGE_OUT * 2 ? "right"
            : null;
      if (!side || y < col.top || y > hostRect.bottom) {
        clearDwell();
        setHandle((h) => (h ? null : h));
        return;
      }
      if (dwell.current?.side === side) return;
      clearDwell();
      dwell.current = {
        side,
        timer: window.setTimeout(() => {
          cache = null; // 出现拖拽柄前取一次最新边界
          const c = columnRect();
          if (!c) return;
          setHandle({
            side,
            x: (side === "left" ? c.left : c.right) - hostRect.left,
            top: Math.max(0, c.top - hostRect.top) + 12,
            height: Math.max(80, hostRect.height - Math.max(0, c.top - hostRect.top) - 24),
          });
        }, DWELL_MS),
      };
    };

    const onMove = (e: PointerEvent) => {
      if (draggingRef.current) return;
      cancelAnimationFrame(frame.current);
      const { clientX, clientY } = e;
      frame.current = requestAnimationFrame(() => evaluate(clientX, clientY));
    };
    const onLeave = () => { clearDwell(); };

    host.addEventListener("pointermove", onMove, { passive: true });
    host.addEventListener("pointerleave", onLeave);
    return () => {
      host.removeEventListener("pointermove", onMove);
      host.removeEventListener("pointerleave", onLeave);
      cancelAnimationFrame(frame.current);
      clearDwell();
    };
  }, [hostRef]);

  const onPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    const host = hostRef.current;
    const col = host?.querySelector<HTMLElement>(".chat-panel-flow .mf-list")?.getBoundingClientRect();
    if (!host || !col) return;
    e.preventDefault();
    draggingRef.current = true;
    setDragging(true);
    const center = col.left + col.width / 2;
    const hostRect = host.getBoundingClientRect();
    const maxByHost = Math.min(MAX_W, hostRect.width);
    const root = document.documentElement;
    let width = col.width;
    let raf = 0;

    const move = (ev: PointerEvent) => {
      width = Math.round(Math.min(maxByHost, Math.max(MIN_W, Math.abs(ev.clientX - center) * 2)));
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => {
        root.style.setProperty("--content-max-w", `${width}px`);
        setHandle((h) => h && { ...h, x: (h.side === "left" ? center - width / 2 : center + width / 2) - hostRect.left });
      });
    };
    const up = () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      window.removeEventListener("pointercancel", up);
      draggingRef.current = false;
      setDragging(false);
      hideHandle();
      // 设置页滑杆步长 50px：落点吸附到同一刻度，两处显示一致
      const snapped = Math.round(width / 50) * 50;
      useUiStore.getState().setPrefs({ contentMaxWidth: snapped >= maxByHost - 25 && maxByHost >= MAX_W ? MAX_W : snapped });
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    window.addEventListener("pointercancel", up);
  };

  if (!handle) return null;
  return (
    <div
      className={"cw-handle" + (dragging ? " is-dragging" : "")}
      style={{ left: handle.x, top: handle.top, height: handle.height }}
      role="separator"
      aria-orientation="vertical"
      aria-label="拖动调整内容宽度"
      onPointerDown={onPointerDown}
      onPointerLeave={() => { if (!draggingRef.current) hideHandle(); }}
    >
      <span className="cw-handle-grip" />
    </div>
  );
}
