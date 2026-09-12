/**
 * 可拖拽调整宽度的面板分隔条 (v3 — 零 React 重渲染方案)
 *
 * 性能策略:
 * - 拖拽中: 直接操作 DOM element.style.width, 不触发任何 React state 更新
 * - 拖拽结束: 一次性 commit 最终宽度到 store
 *
 * 这样每帧只有一次 style 写入, 没有 React reconciliation, 没有 localStorage I/O。
 */
import { useCallback, useRef } from "react";

interface Props {
  side: "left" | "right";
  /** 拖拽开始时的基准宽度(px) */
  baseWidth: number;
  /** 最小宽度(px) */
  minWidth: number;
  /** 最大宽度(px) */
  maxWidth: number;
  /** plan-95: 拖拽方向上其余区域需保留的最小空间(px)，动态上限 = 容器宽 - reservePx */
  reservePx?: number;
  /** 拖拽过程中直接操作的 DOM 元素 ref */
  panelEl: React.RefObject<HTMLElement | null>;
  /** 拖拽结束,提交最终宽度 */
  onCommit: (width: number) => void;
}

export function ResizeHandle({ side, baseWidth, minWidth, maxWidth, reservePx = 0, panelEl, onCommit }: Props) {
  const startXRef = useRef(0);
  const baseWRef = useRef(baseWidth);
  const draggingRef = useRef(false);
  const handleRef = useRef<HTMLDivElement>(null);
  // plan-95: 动态实际上限——静态 maxWidth 不考虑窗口可用空间，窄窗口下拖到
  // "拖不动"后 DOM 宽度仍超布局，溢出部分被裁剪（面板右缘图标不可见）。
  // 上限取 min(maxWidth, 容器宽 - reservePx)，到达上限后宽度与视觉完全静止。
  const effectiveMaxRef = useRef(maxWidth);

  // plan-246-1236 S6: 把鼠标相对手柄的 Y 写成 CSS 变量，供渐隐细线定位（不走 React state）。
  const setHandleY = useCallback((clientY: number) => {
    const el = handleRef.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    const y = Math.max(0, Math.min(rect.height, clientY - rect.top));
    el.style.setProperty("--handle-y", `${y}px`);
  }, []);

  const clampMax = useCallback(() => {
    const parent = panelEl.current?.parentElement;
    const avail = parent ? parent.clientWidth - reservePx : Number.POSITIVE_INFINITY;
    effectiveMaxRef.current = Math.max(minWidth, Math.min(maxWidth, avail));
  }, [maxWidth, minWidth, panelEl, reservePx]);

  const applyWidth = useCallback((w: number) => {
    const el = panelEl.current;
    if (!el) return;
    el.style.width = w + "px";
    el.style.flexBasis = w + "px";
  }, [panelEl]);

  const handleMouseMove = useCallback((e: MouseEvent) => {
    if (!draggingRef.current) return;
    e.preventDefault();
    setHandleY(e.clientY);

    const rawDelta = e.clientX - startXRef.current;
    const effective = side === "right" ? -rawDelta : rawDelta;
    const newWidth = Math.round(Math.max(minWidth, Math.min(effectiveMaxRef.current, baseWRef.current + effective)));

    // 直接操作 DOM — 零 React 重渲染
    applyWidth(newWidth);
  }, [side, minWidth, applyWidth, setHandleY]);

  const handleMouseUp = useCallback(() => {
    if (!draggingRef.current) return;
    draggingRef.current = false;
    handleRef.current?.classList.remove("is-dragging");

    document.removeEventListener("mousemove", handleMouseMove);
    document.removeEventListener("mouseup", handleMouseUp);
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
    document.body.classList.remove("panel-dragging");

    // 读取最终宽度,一次性提交（plan-95: 提交前按动态上限钳制，不持久化越界值）
    const el = panelEl.current;
    const finalWidth = Math.min(
      effectiveMaxRef.current,
      el ? parseInt(el.style.width, 10) || baseWRef.current : baseWRef.current,
    );
    onCommit(finalWidth);
  }, [handleMouseMove, panelEl, onCommit]);

  const handleMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    draggingRef.current = true;
    handleRef.current?.classList.add("is-dragging");
    startXRef.current = e.clientX;
    baseWRef.current = baseWidth;
    clampMax();

    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    document.body.classList.add("panel-dragging");

    document.addEventListener("mousemove", handleMouseMove);
    document.addEventListener("mouseup", handleMouseUp);
  }, [baseWidth, clampMax, handleMouseMove, handleMouseUp]);

  return (
    <div
      ref={handleRef}
      className={`resize-handle resize-handle-${side}`}
      onMouseDown={handleMouseDown}
      onMouseMove={(e) => setHandleY(e.clientY)}
      role="separator"
      aria-orientation="vertical"
    />
  );
}
