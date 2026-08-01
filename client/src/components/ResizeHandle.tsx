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
  /** 拖拽过程中直接操作的 DOM 元素 ref */
  panelEl: React.RefObject<HTMLElement | null>;
  /** 拖拽结束,提交最终宽度 */
  onCommit: (width: number) => void;
}

export function ResizeHandle({ side, baseWidth, minWidth, maxWidth, panelEl, onCommit }: Props) {
  const startXRef = useRef(0);
  const baseWRef = useRef(baseWidth);
  const draggingRef = useRef(false);

  const handleMouseMove = useCallback((e: MouseEvent) => {
    if (!draggingRef.current) return;
    e.preventDefault();

    const rawDelta = e.clientX - startXRef.current;
    const effective = side === "right" ? -rawDelta : rawDelta;
    const newWidth = Math.round(Math.max(minWidth, Math.min(maxWidth, baseWRef.current + effective)));

    // 直接操作 DOM — 零 React 重渲染
    const el = panelEl.current;
    if (el) {
      el.style.width = newWidth + "px";
      el.style.flexBasis = newWidth + "px";
    }
  }, [side, minWidth, maxWidth, panelEl]);

  const handleMouseUp = useCallback(() => {
    if (!draggingRef.current) return;
    draggingRef.current = false;

    document.removeEventListener("mousemove", handleMouseMove);
    document.removeEventListener("mouseup", handleMouseUp);
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
    document.body.classList.remove("panel-dragging");

    // 读取最终宽度,一次性提交
    const el = panelEl.current;
    const finalWidth = el ? parseInt(el.style.width, 10) || baseWRef.current : baseWRef.current;
    onCommit(finalWidth);
  }, [handleMouseMove, panelEl, onCommit]);

  const handleMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    draggingRef.current = true;
    startXRef.current = e.clientX;
    baseWRef.current = baseWidth;

    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    document.body.classList.add("panel-dragging");

    document.addEventListener("mousemove", handleMouseMove);
    document.addEventListener("mouseup", handleMouseUp);
  }, [baseWidth, handleMouseMove, handleMouseUp]);

  return (
    <div
      className={`resize-handle resize-handle-${side}`}
      onMouseDown={handleMouseDown}
      role="separator"
      aria-orientation="vertical"
    />
  );
}
