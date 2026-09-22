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

  /** 手柄矩形缓存（拖拽开始时取一次）。
   *
   *  为何必须缓存：拖拽中每次 mousemove 都调 getBoundingClientRect 会**强制同步布局**
   *  （浏览器必须先把此前写入的 width 脏布局全部重算才能给出准确矩形）。
   *  面板宽度写入 + 读取交替，每次 mousemove 都多付一次全量重排——
   *  这正是"拖左右侧面板时（尤其会话运行时）非常卡顿"的直接来源之一。
   *  手柄在拖拽期间不会移动（只有面板宽变），矩形完全可以复用。 */
  const rectRef = useRef<{ top: number; height: number } | null>(null);
  // rAF 句柄：一帧内多次 mousemove 只写一次宽度（高刷鼠标/慢拖时事件可达 200+Hz）
  const rafRef = useRef(0);
  const pendingWRef = useRef(0);

  // plan-246-1236 S6: 把鼠标相对手柄的 Y 写成 CSS 变量，供渐隐细线定位（不走 React state）。
  // 用缓存的矩形计算，不再每次调用 getBoundingClientRect（避免强制同步布局）。
  const setHandleY = useCallback((clientY: number) => {
    const el = handleRef.current;
    if (!el) return;
    const r = rectRef.current;
    const y = r ? Math.max(0, Math.min(r.height, clientY - r.top)) : clientY;
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

    // 直接操作 DOM — 零 React 重渲染。
    // 再用 rAF 合并：本帧内后续事件只更新目标值，实际写入每帧至多一次，
    // 减少"写宽度 → 重排"的频率（高刷鼠标下一次拖拽可省掉一半以上写入）。
    pendingWRef.current = newWidth;
    if (rafRef.current) return;
    rafRef.current = requestAnimationFrame(() => {
      rafRef.current = 0;
      if (!draggingRef.current) return; // 帧间已结束拖拽：不再写
      applyWidth(pendingWRef.current);
    });
  }, [side, minWidth, applyWidth, setHandleY]);

  const handleMouseUp = useCallback(() => {
    if (!draggingRef.current) return;
    draggingRef.current = false;
    handleRef.current?.classList.remove("is-dragging");

    // 取消未执行的那一帧，并**用最终目标值同步落一次**：
    // 否则最后一次移动可能停在上一帧的宽度上，提交值与视觉不一致。
    if (rafRef.current) { cancelAnimationFrame(rafRef.current); rafRef.current = 0; }
    applyWidth(pendingWRef.current);
    rectRef.current = null;

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
  }, [handleMouseMove, panelEl, onCommit, applyWidth]);

  const handleMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    draggingRef.current = true;
    handleRef.current?.classList.add("is-dragging");
    startXRef.current = e.clientX;
    baseWRef.current = baseWidth;
    pendingWRef.current = baseWidth;
    clampMax();
    // 拖拽开始取一次手柄矩形（此时无脏布局，成本最低），全程复用
    const r = handleRef.current?.getBoundingClientRect();
    rectRef.current = r ? { top: r.top, height: r.height } : null;
    setHandleY(e.clientY);

    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    document.body.classList.add("panel-dragging");

    document.addEventListener("mousemove", handleMouseMove);
    document.addEventListener("mouseup", handleMouseUp);
  }, [baseWidth, clampMax, handleMouseMove, handleMouseUp, setHandleY]);

  return (
    <div
      ref={handleRef}
      className={`resize-handle resize-handle-${side}`}
      onMouseDown={handleMouseDown}
      onMouseMove={(e) => { if (!draggingRef.current) setHandleY(e.clientY); }}
      role="separator"
      aria-orientation="vertical"
    />
  );
}
