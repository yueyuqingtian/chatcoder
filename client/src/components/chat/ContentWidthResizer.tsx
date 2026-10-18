/** 内容宽度边缘拖拽（plan-20261017-ux-overhaul 阶段 C；plan-156-623 性能收敛）。
 *
 * 交互：鼠标停在消息列左 / 右边缘满 0.5 秒，边缘淡入一根拖拽柄；按住拖动即对称调整
 * 消息与输入框的最大宽度，松手写回「设置 → 外观 → 内容展示宽度」（同一字段，两处双向同步）。
 * 为什么要停留：边缘是阅读时鼠标常经过的位置，悬停即出现会频繁闪烁打扰。
 *
 * 性能（plan-20261018-content-drag-perf：与左右面板拖拽走同一套运动期门控）：
 *  · 指针移动只做 rAF 合帧的几何判断，不触发 React 渲染；
 *  · 拖拽期间直接写根节点的 --content-max-w，松手才 setPrefs 一次；
 *  · 拖拽柄位置也在 rAF 里直接写 DOM（旧实现每帧 setHandle → ContentWidthResizer
 *    每帧重渲染，是「拖内容宽度时发涩」的来源之一）；
 *  · 拖左右面板 / 窗口缩放期间（PerfBus 忙碌）直接跳过命中判定，不读布局、不弹柄；
 *  · **拖拽期登记 PerfBus 的 panel-drag**（与 ResizeHandle 同一原因）：流式刷新、
 *    自动贴底、虚拟列表 overscan、输入框重测等下游门控全部靠它识别运动期，
 *    不登记就等于「拖内容宽度时消息流仍在刷新」，这是此前拖动发涩的根因；
 *  · 复用 body.panel-dragging 的 CSS 降级（折行 / 悬停命中 / 循环动画）、
 *    全屏遮罩与 blur/resize/按键丢失兜底，做法与 ResizeHandle 一一对应。
 */
import { useEffect, useRef, useState, type RefObject } from "react";
import { useUiStore } from "../../store/ui";
import { acquire, isBusy, release } from "../../perf/bus";
import { runReconcile } from "../../perf/reconcile";
import { CONTENT_WIDTH_PREVIEW_EVENT } from "./useContentColumnGeometry";
import { COLUMN_PAD } from "./contentColumnGeometry";

/** 触发区：消息列外框向外 EDGE_OUT，向内覆盖整段内边距（文字与外框之间的留白，约 32px）。
 *  只按外框 ±8px 判定时，鼠标停在文字边缘附近恰好落在区外，几乎触发不了。 */
const EDGE_OUT = 12;
/** 边缘停留多久出现拖拽柄。用户要求 0.5 秒：1 秒等待偏钝，拖宽是高频微调动作。 */
const DWELL_MS = 500;
const MIN_W = 560;
const MAX_W = 1600;

type Side = "left" | "right";

export function ContentWidthResizer({ hostRef }: { hostRef: RefObject<HTMLElement> }) {
  const [handle, setHandle] = useState<{ side: Side; top: number; height: number } | null>(null);
  const [dragging, setDragging] = useState(false);
  // 拖拽态同时放 ref：指针事件回调读 ref，避免旧闭包里的 dragging=false 在拖动中把柄隐藏
  const draggingRef = useRef(false);
  const handleVisibleRef = useRef(false);
  const dwell = useRef<{ side: Side; timer: number } | null>(null);
  const frame = useRef(0);
  /** 进行中拖拽的收尾函数（组件卸载时兜底调用，不提交宽度）。 */
  const endDragRef = useRef<((commit: boolean) => void) | null>(null);
  /** 挂载标记：卸载兜底会走同一收尾函数，此时不得再 setState。 */
  const aliveRef = useRef(true);

  /** 隐藏拖拽柄并清掉悬停计时：鼠标仍停在边缘时会重新计时，而不是永远停在「已触发」状态 */
  const hideHandle = () => {
    if (dwell.current) window.clearTimeout(dwell.current.timer);
    dwell.current = null;
    handleVisibleRef.current = false;
    if (aliveRef.current) setHandle(null);
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
      if (!col || col.width <= 0) {
        clearDwell();
        if (handleVisibleRef.current) hideHandle();
        return;
      }
      // 铺满内容区时左右边缘贴着面板边框，不提供拖拽（与左右面板分隔条冲突）
      const side: Side | null =
        x >= col.left - EDGE_OUT && x <= col.left + inset && col.left - hostRect.left > EDGE_OUT * 2 ? "left"
          : x <= col.right + EDGE_OUT && x >= col.right - inset && hostRect.right - col.right > EDGE_OUT * 2 ? "right"
            : null;
      if (!side || y < col.top || y > hostRect.bottom) {
        clearDwell();
        if (handleVisibleRef.current) hideHandle();
        return;
      }
      if (dwell.current?.side === side) return;
      clearDwell();
      dwell.current = {
        side,
        timer: window.setTimeout(() => {
          cache = null; // 出现拖拽柄前取一次最新边界（此时未在拖拽，允许读布局）
          const c = columnRect();
          if (!c) return;
          handleVisibleRef.current = true;
          // 只算纵向范围；横向位置由 CSS 变量 --content-col-left / -w 驱动，
          // 拖拽期内容列一变宽，柄就跟着走——不需要每帧读布局回写 left。
          setHandle({
            side,
            top: Math.max(0, c.top - hostRect.top) + 12,
            height: Math.max(80, hostRect.height - Math.max(0, c.top - hostRect.top) - 24),
          });
        }, DWELL_MS),
      };
    };

    const onMove = (e: PointerEvent) => {
      if (draggingRef.current) return;
      // 拖面板 / 窗口缩放期间不评估：此时读边界会与面板宽度写入叠加成强制布局，
      // 而且指针本来就不在消息列边缘，弹柄也没有意义。
      if (isBusy()) {
        clearDwell();
        cancelAnimationFrame(frame.current);
        return;
      }
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

  /** 拖拽中组件卸载（切换会话 / 面板折叠 / 退出缩放）：不提交中途宽度，
   *  只把遮罩、总线登记与 window 监听清干净——否则残留的 move 闭包会继续改写宽度。 */
  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
      endDragRef.current?.(false);
    };
  }, []);

  const onPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    const host = hostRef.current;
    const col = host?.querySelector<HTMLElement>(".chat-panel-flow .mf-list")?.getBoundingClientRect();
    if (!host || !col || !handle) return;
    // 右键 / 中键不进入拖拽：旧实现未判按钮，右键按下也会锁住指针并改写宽度。
    if (e.button !== 0) return;
    e.preventDefault();
    /* 防御：上一次拖拽若因异常路径没收尾（pointerup 丢失、组件热更新），
       残留的遮罩与 body 类会挡住整个界面。再次按下时先按「不提交」收尾旧的。 */
    endDragRef.current?.(false);
    draggingRef.current = true;
    setDragging(true);
    // 上限与 CSS 同口径：可用宽 = 宿主内宽 − 滚动条 gutter，再扣左右内距。
    // 旧实现用 `host.offsetWidth - host.clientWidth` 取"滚动条"，但 .chat-panel 自身不滚动，
    // 该值恒为 0，于是能拖到比可用宽更宽、右侧被 overflow:hidden 裁掉。
    const gutter = Math.max(0,
      parseFloat(getComputedStyle(host).getPropertyValue("--scrollbar-gutter-w")) || 12);
    const avail = Math.max(0, host.clientWidth - gutter);
    const maxByHost = Math.min(MAX_W, Math.max(MIN_W, avail - COLUMN_PAD * 2));
    const root = document.documentElement;
    // 中轴固定为按下时的列中心：若每帧按列的新位置重取中心，而列又因右侧卡片按需左移随宽度移动，
    // 「宽度 → 位置 → 中心 → 宽度」会形成正反馈，拖动中宽度发散、左右抖动（plan-156-620 审查发现）
    const center = col.left + col.width / 2;
    let width = col.width;
    let raf = 0;
    let finished = false;

    /* ── 运动期门控：与 ResizeHandle 完全同序（先登记 PerfBus，再派发拖拽开始）──
       MessageFlow 的锚点捕获会检查 isBusy()，顺序反了就捕获不到干净锚点。 */
    acquire("panel-drag");
    document.body.classList.add("panel-dragging");
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    // 全屏遮罩：指针扫到右面板 webview/iframe 上方松手时宿主收不到 pointerup，
    // 会残留拖拽态并继续改写宽度（与 ResizeHandle.addShield 同一事故）。
    const shield = document.createElement("div");
    shield.className = "panel-drag-shield";
    document.body.appendChild(shield);
    // 减少动态效果时与面板拖拽同口径降档：隔帧写宽度，优先保住帧率。
    const reduceMotion = typeof window.matchMedia === "function"
      && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    root.dataset.panelDragMode = reduceMotion ? "balanced" : "realtime";
    let skipFrame = false;
    window.dispatchEvent(new CustomEvent("chatcoder:panel-drag-start"));

    const move = (ev: PointerEvent) => {
      // mouseup 兜底：在 guest / 窗口外松手时事件可能丢失，后续 pointermove 的 buttons
      // 恒反映真实按键状态——检测到左键已松开即按正常路径收尾。
      if ((ev.buttons & 1) === 0) { finish(true); return; }
      ev.preventDefault();
      // 亚像素：与几何层一致，避免慢速拖拽时宽度在整数像素间来回跳
      const raw = Math.abs(ev.clientX - center) * 2;
      width = Math.round(Math.min(maxByHost, Math.max(MIN_W, raw)) * 10) / 10;
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => {
        raf = 0;
        if (!draggingRef.current) return;
        if (reduceMotion) {
          skipFrame = !skipFrame;
          if (skipFrame) return;
        }
        root.style.setProperty("--content-max-w", `${width}px`);
        // 主面板内内容列几何已由 CSS 变量驱动：几何层在同一调用里写 --content-col-max，
        // 宽度与位置由浏览器在布局阶段一并算出，拖拽柄随同一条 calc 跟随列边缘。
        window.dispatchEvent(new CustomEvent(CONTENT_WIDTH_PREVIEW_EVENT, { detail: { width } }));
      });
    };

    /** 收尾。commit=false 只用于组件卸载：不写偏好，避免把中途值持久化。 */
    const finish = (commit: boolean) => {
      if (finished) return;
      finished = true;
      cancelAnimationFrame(raf);
      raf = 0;
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
      window.removeEventListener("blur", onUp);
      window.removeEventListener("resize", onUp);
      shield.remove();
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      endDragRef.current = null;
      draggingRef.current = false;
      if (aliveRef.current) setDragging(false);
      // 设置页滑杆步长 50px：落点吸附到同一刻度，两处显示一致
      const snapped = Math.round(width / 50) * 50;
      const finalW = snapped >= maxByHost - 25 && maxByHost >= MAX_W ? MAX_W : snapped;
      if (commit) {
        // 吸附值与偏好相同时 setPrefs 不会触发重算——先把预览对齐到落点，避免停在未吸附的宽度
        window.dispatchEvent(new CustomEvent(CONTENT_WIDTH_PREVIEW_EVENT, { detail: { width: finalW } }));
      }
      // 顺序与 ResizeHandle 一致：摘 body 类 → 归还总线 → 派发结束 → 收敛 → 落库
      document.body.classList.remove("panel-dragging");
      release("panel-drag");
      hideHandle();
      window.dispatchEvent(new CustomEvent("chatcoder:panel-drag-end"));
      // 虚拟器视口尺寸 / 滚动锚点 / 输入框重测等在运动期只记账，这里统一收敛一次
      runReconcile();
      if (commit) useUiStore.getState().setPrefs({ contentMaxWidth: finalW });
    };
    const onUp = () => finish(true);
    endDragRef.current = finish;

    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
    // 拖拽中失焦 / 系统 resize（Alt+Tab、DPI 变化）时 pointerup 可能永不到达
    window.addEventListener("blur", onUp);
    window.addEventListener("resize", onUp);
  };

  /** plan-156-621：键盘调整内容宽度——此前 role=separator 无 tabIndex/方向键，键盘用户完全无法调整。 */
  const onKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    e.preventDefault();
    const host = hostRef.current;
    const col = host?.querySelector<HTMLElement>(".chat-panel-flow .mf-list")?.getBoundingClientRect();
    if (!col) return;
    const step = 50; // 与鼠标拖拽 / 设置页滑杆同一吸附刻度
    const cur = Math.round(col.width / 50) * 50;
    const next = Math.max(MIN_W, Math.min(MAX_W, cur + (e.key === "ArrowRight" ? step : -step)));
    window.dispatchEvent(new CustomEvent(CONTENT_WIDTH_PREVIEW_EVENT, { detail: { width: next } }));
    useUiStore.getState().setPrefs({ contentMaxWidth: next });
  };

  if (!handle) return null;
  return (
    <div
      className={"cw-handle" + (dragging ? " is-dragging" : "")}
      // 横向位置由内容列的同一套 CSS 变量决定（见 composer.css 的 .cw-handle 规则）：
      // 拖拽期内容列一变宽，柄与列边缘同帧移动，不需要每帧读布局回写 left。
      data-side={handle.side}
      style={{ top: handle.top, height: handle.height }}
      role="separator"
      aria-orientation="vertical"
      aria-label="拖动调整内容宽度（方向键微调）"
      tabIndex={0}
      onKeyDown={onKeyDown}
      onPointerDown={onPointerDown}
      onPointerLeave={() => { if (!draggingRef.current) hideHandle(); }}
    >
      <span className="cw-handle-grip" />
    </div>
  );
}
