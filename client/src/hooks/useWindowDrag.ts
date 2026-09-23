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
 *
 * ── 本轮修复：点标题栏按钮却触发窗口缩放/全屏（用户反馈"点右侧面板折叠钮，界面变成全屏"）──
 * 两个真实缺陷，都会让"点按钮"被当成"操作窗口"：
 *   ① **陈旧起点**：旧实现在交互元素上按下时直接 return，**不更新 startRef**。
 *      于是之后在按钮内按下并轻微移动（>4px）时，位移是相对"上一次在空白区按下"的位置
 *      计算的 —— 必然超过阈值 ⇒ 误进入窗口拖动。若此刻正处于伪最大化，
 *      主进程的 startWindowDrag 会把它当成"拖出最大化"，把窗口还原到**光标附近**
 *      （光标在标题栏右上角 ⇒ 窗口跑到右上角）。
 *   ② **dblclick 的 target 会被提升**：浏览器在两次点击的 target 不同时，
 *      把 dblclick 派发到二者的**共同祖先**。而点"折叠/展开"按钮会改变标题栏布局，
 *      第二次点击可能落在按钮外的空白处 ⇒ dblclick 落到标题栏本身 ⇒
 *      isDragTarget 判定通过 ⇒ 误触发伪最大化（小窗变全屏）。
 *      修法：不再依赖原生 dblclick，改为**自己按 pointerup 判定**双击——
 *      只看两次"确实落在可拖拽区"的点击，落在按钮上的那一次直接打断双击链。
 */
import { useCallback, useEffect, useRef } from "react";

/** 进入拖动的位移阈值（px）：低于此值一律视为点击/抖动，不启动窗口拖动 */
const DRAG_THRESHOLD = 4;
/** 双击判定的最大间隔 / 两次点击的最大位移（与系统双击阈值同量级） */
const DOUBLE_CLICK_MS = 400;
const DOUBLE_CLICK_SLOP = 6;

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
  /** 本次按下的起点：**每次 pointerdown 都刷新**——杜绝"陈旧起点"导致的误拖动 */
  const startRef = useRef({ x: 0, y: 0 });
  /** 本次按下是否落在可拖拽区（false = 落在按钮等交互元素上，全程不参与拖动/双击） */
  const armRef = useRef(false);
  /** 本次按下是否真的进入了拖动（决定 pointerup 时是否算"点击"） */
  const draggedRef = useRef(false);
  /** 上一次"有效点击"（起点在可拖拽区、无位移）：用于自研双击判定 */
  const lastClickRef = useRef<{ t: number; x: number; y: number } | null>(null);
  /** rAF 句柄：把一帧内的多次 pointermove 合并为一次 IPC（见 onPointerMove 注释） */
  const rafRef = useRef<number | null>(null);

  const stop = useCallback(() => {
    if (!draggingRef.current) return;
    draggingRef.current = false;
    // 丢弃尚未执行的那一帧上报，避免拖动结束后又补一次移动
    if (rafRef.current !== null) {
      cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    }
    document.body.classList.remove("window-dragging");
    window.chatcoderAPI?.endWindowDrag?.();
  }, []);

  /** 指针抬起/取消/窗口失焦都要收尾，避免"鼠标已松但窗口还跟着跑" */
  useEffect(() => {
    const onUp = () => { stop(); armRef.current = false; };
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
      if (e.button !== 0) {
        armRef.current = false;
        lastClickRef.current = null;
        return;
      }
      // 起点无条件刷新：即便这次按在按钮上，也不会把"上次在空白区的按下位置"
      // 当成拖动基准（那正是"点按钮却把窗口拖走/还原到右上角"的根因）。
      startRef.current = { x: e.clientX, y: e.clientY };
      draggedRef.current = false;
      draggingRef.current = false;
      armRef.current = isDragTarget(e);
      if (!armRef.current) {
        // 落在按钮/输入框/菜单上：打断双击链——否则"点按钮 + 点旁边空白"
        // 会被浏览器提升成标题栏上的 dblclick，误触发伪最大化（小窗变全屏）。
        lastClickRef.current = null;
      }
    },
    [],
  );

  /** 挂在同一个容器的 onPointerMove 上：超过阈值才真正进入拖动 */
  const onPointerMove = useCallback(
    (e: React.PointerEvent) => {
      // 按下始于交互元素 ⇒ 本次指针会话完全不参与窗口拖动
      if (!armRef.current) return;
      if (e.buttons === 0) return; // 指针已松开（快速拖拽后抬起）
      if (!draggingRef.current) {
        const dx = Math.abs(e.clientX - startRef.current.x);
        const dy = Math.abs(e.clientY - startRef.current.y);
        if (dx < DRAG_THRESHOLD && dy < DRAG_THRESHOLD) return;
        draggingRef.current = true;
        draggedRef.current = true;
        document.body.classList.add("window-dragging");
        // 锁定事件流：指针移出窗口/移过子元素也不丢 pointermove
        try { (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId); } catch { /* ignore */ }
        window.chatcoderAPI?.startWindowDrag?.();
      }
      // 移动由主进程按显示器刷新率读取光标推进。渲染层不再逐帧发 IPC，
      // 否则帧率会被进程往返卡住，而不是被刷新率卡住。
    },
    [],
  );

  /** 挂在同一个容器的 onPointerUp 上（配合全局兜底）——
   *  同时承担**自研双击判定**：只有"起点在可拖拽区、几乎无位移"的点击才计数，
   *  因此点按钮永远不会被算成双击（原生 dblclick 的 target 会被提升到共同祖先，
   *  无法区分"点按钮 + 点空白"与"连点两次空白"）。 */
  const onPointerUp = useCallback(
    (e: React.PointerEvent) => {
      const armed = armRef.current;
      const dragged = draggedRef.current;
      const slopOk = Math.abs(e.clientX - startRef.current.x) < DOUBLE_CLICK_SLOP
        && Math.abs(e.clientY - startRef.current.y) < DOUBLE_CLICK_SLOP;
      stop();
      armRef.current = false;
      draggedRef.current = false;
      if (!armed || dragged || !slopOk) {
        lastClickRef.current = null;
        return;
      }
      const now = Date.now();
      const prev = lastClickRef.current;
      if (prev && now - prev.t <= DOUBLE_CLICK_MS
        && Math.abs(e.clientX - prev.x) < DOUBLE_CLICK_SLOP
        && Math.abs(e.clientY - prev.y) < DOUBLE_CLICK_SLOP) {
        lastClickRef.current = null;
        window.chatcoderAPI?.toggleMaximize?.();
      } else {
        lastClickRef.current = { t: now, x: e.clientX, y: e.clientY };
      }
    },
    [stop],
  );

  return {
    onPointerDown,
    onPointerMove,
    onPointerUp,
    cancelDrag: stop,
  };
}
