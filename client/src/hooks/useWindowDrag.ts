/**
 * plan-26-126 P6：自研窗口拖拽 + 双击伪最大化。
 *
 * 背景（为何不再用 `-webkit-app-region: drag`）：
 *   原生 drag 区域由**系统**处理拖拽与双击，DOM 完全收不到 dblclick 事件；
 *   双击会直接触发系统原生最大化，而原生最大化会重建 DWM 图层，
 *   导致透明/亚克力标记丢失（玻璃"点一下全屏就没了"，且退出后不保证恢复）。
 *
 * 历史实现的问题（本轮修正）：
 *   旧版在 mousedown 后固定延迟 180ms 再调主进程 `mainWindow.startDrag(...)`。
 *   但 **BrowserWindow 上没有 startDrag 方法**（只有 WebContents 的，语义是拖文件），
 *   调用即抛 TypeError 并被 catch 静默吞掉 ⇒ 用户反馈"非全屏模式下顶部拖不动窗口"。
 *
 * 现方案（与双击判定天然共存，且不依赖系统拖动循环）：
 *   * pointerdown 时**不立刻**发起拖拽，而是等指针移动超过 DRAG_THRESHOLD(4px)
 *     —— 这样单击/双击/轻微抖动都不会误触发拖动；
 *   * 一旦超过阈值即进入拖动：pointermove 逐帧上报（主进程 setPosition），
 *     pointerup / pointercancel 结束；
 *   * 拖动期间用 setPointerCapture 锁定事件流，指针移出窗口也不丢帧；
 *   * 双击由 dblclick 事件处理（拖动从未启动，因为双击的两次点击位移都很小）。
 *
 * 安全性：落在交互元素上的按下（按钮/输入框/菜单/链接等）一律不参与，
 *   因此不会出现"点按钮却把窗口拖走"的问题；子控件无需自己 stopPropagation。
 */
import { useCallback, useEffect, useRef } from "react";

/** 进入拖动的位移阈值（px）：低于此值一律视为点击/抖动，不启动窗口拖动 */
const DRAG_THRESHOLD = 4;

const INTERACTIVE_SELECTOR =
  "button, input, textarea, select, a, [role='button'], [role='menuitem'], .context-menu, .titlebar-menu, .resize-handle";

/** 是否是"可拖拽区域"内的有效按下 */
function isDragTarget(e: React.PointerEvent | React.MouseEvent) {
  if (e.button !== 0) return false; // 仅左键
  const el = e.target as HTMLElement | null;
  if (el && el.closest(INTERACTIVE_SELECTOR)) return false;
  return true;
}

export function useWindowDrag() {
  const draggingRef = useRef(false);
  const startRef = useRef({ x: 0, y: 0 });

  const stop = useCallback(() => {
    if (!draggingRef.current) return;
    draggingRef.current = false;
    document.body.classList.remove("window-dragging");
    window.chatcoderAPI?.endWindowDrag?.();
  }, []);

  /** 指针抬起/取消/窗口失焦都要收尾，避免"鼠标已松但窗口还跟着跑" */
  useEffect(() => {
    const onUp = () => stop();
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
    window.addEventListener("blur", onUp);
    return () => {
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
      window.removeEventListener("blur", onUp);
    };
  }, [stop]);

  /** 挂在可拖拽容器的 onPointerDown 上 */
  const onPointerDown = useCallback(
    (e: React.PointerEvent) => {
      if (!isDragTarget(e)) return;
      startRef.current = { x: e.clientX, y: e.clientY };
      draggingRef.current = false;
    },
    [],
  );

  /** 挂在同一个容器的 onPointerMove 上：超过阈值才真正进入拖动 */
  const onPointerMove = useCallback(
    (e: React.PointerEvent) => {
      if (e.buttons === 0) return; // 指针已松开（快速拖拽后抬起）
      if (!draggingRef.current) {
        const dx = Math.abs(e.clientX - startRef.current.x);
        const dy = Math.abs(e.clientY - startRef.current.y);
        if (dx < DRAG_THRESHOLD && dy < DRAG_THRESHOLD) return;
        draggingRef.current = true;
        document.body.classList.add("window-dragging");
        // 锁定事件流：指针移出窗口/移过子元素也不丢 pointermove
        try { (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId); } catch { /* ignore */ }
        window.chatcoderAPI?.startWindowDrag?.();
      }
      window.chatcoderAPI?.moveWindowDrag?.();
    },
    [],
  );

  /** 挂在同一个容器的 onPointerUp 上（配合全局兜底） */
  const onPointerUp = useCallback(() => stop(), [stop]);

  /** 挂在同一个容器的 onDoubleClick 上 → 伪最大化（主进程走伪最大化，保玻璃） */
  const onDoubleClick = useCallback(
    (e: React.MouseEvent) => {
      if (!isDragTarget(e)) return;
      window.chatcoderAPI?.toggleMaximize?.();
    },
    [],
  );

  return {
    onPointerDown,
    onPointerMove,
    onPointerUp,
    onDoubleClick,
    cancelDrag: stop,
  };
}
