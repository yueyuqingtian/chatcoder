/** 内容列几何的运行时编排（plan-156-619 S3 · 需求 C1）。
 *
 * 职责边界：
 *   · **何时重算宽度**（几何纪元）——只有窗口缩放 / 拖面板 / 折叠展开面板 / 用户改内容宽度；
 *   · **何时只重算位置**——卡片占位变化、宿主尺寸抖动 ≤ 阈值；
 *   · 把结果写成 `.chat-panel` 上的 CSS 变量，供 `.mf-list` 与 `.composer` 消费。
 *
 * 为什么用 CSS 变量而不是 React state 下发：
 *   拖拽面板时宽度逐帧变化，走 state 会让整棵消息树每帧重渲染（既有 plan-329-1647 RFL-1 的教训）。
 *   变量只影响布局，React 不参与，虚拟列表也只在既有节流下重测。
 */
import { useCallback, useEffect, useRef, useState, type RefObject } from "react";
import { isBusy, subscribe as subscribePerfBus } from "../../perf/bus";
import { useUiStore } from "../../store/ui";
import { CARD_RESERVE_EXPANDED, contentColumnGeometry, COLUMN_PAD } from "./contentColumnGeometry";

/** 宿主尺寸变化小于该值视为抖动（滚动条出现、字体 hinting 取整），不递增几何纪元。 */
const HOST_SIZE_EPSILON = 8;

/** 读取滚动条 gutter 预留宽（与 `--scrollbar-gutter-w` 同源，输入框与消息列靠它对齐中轴）。 */
function gutterOf(el: HTMLElement): number {
  const v = parseFloat(getComputedStyle(el).getPropertyValue("--scrollbar-gutter-w"));
  return Number.isFinite(v) && v > 0 ? v : 12;
}

/** 内容列可用宽 = 宿主内容宽 − 滚动条 gutter。 */
function hostWidthOf(el: HTMLElement): number {
  const w = el.clientWidth - gutterOf(el);
  return Number.isFinite(w) && w > 0 ? w : 0;
}

export interface ContentColumnHandle {
  /** 当前内容列右缘相对宿主的剩余间隙（供卡片降级判定）。 */
  shortfall: number;
}

/**
 * @param hostRef 宿主要素（`.chat-panel`）；变量写到它上面，子元素只读。
 * @param cardReserve 卡片右侧占位：展开 336 / 胶囊与隐藏 0。
 */
export function useContentColumnGeometry(
  hostRef: RefObject<HTMLElement | null>,
  cardReserve = 0,
): ContentColumnHandle {
  const preferredW = useUiStore((s) => s.contentMaxWidth);

  /** 冻结宽度：只在几何纪元递增时重算（null = 尚未求解）。 */
  const frozenWidthRef = useRef<number | null>(null);
  /** 上一次观察到的宿主宽（用于 Epsilon 抖动过滤）。 */
  const lastHostRef = useRef(0);
  /** 上一次写入的 shortfall，变化才 setState，避免每帧重渲染。 */
  const lastShortfallRef = useRef(0);
  const [shortfall, setShortfall] = useState(0);

  /** 求解并写变量；`bumpWidth=false` 时复用冻结宽度，只重算位置。 */
  const apply = useCallback((bumpWidth: boolean) => {
    const host = hostRef.current;
    if (!host) return;
    const hostW = hostWidthOf(host);
    if (hostW <= 0) return;
    lastHostRef.current = hostW;

    const geo = contentColumnGeometry({
      hostW,
      preferredW,
      cardReserve,
      // 宽度是否重算由纪元决定：不重算时沿用上次结果，让「只改位置」成立
      pad: COLUMN_PAD,
    });
    const width = bumpWidth || frozenWidthRef.current == null ? geo.width : frozenWidthRef.current;
    frozenWidthRef.current = width;

    // 冻结宽度可能来自旧宿主宽，位置必须按「当前宿主宽 + 冻结宽度」重新求解
    const resolved = width === geo.width
      ? geo
      : contentColumnGeometry({ hostW, preferredW: width, cardReserve, pad: COLUMN_PAD });

    host.style.setProperty("--content-col-w", `${Math.round(resolved.width)}px`);
    host.style.setProperty("--content-col-left", `${Math.round(resolved.left)}px`);
    host.style.setProperty("--card-reserve", `${cardReserve}px`);

    /** plan-156-619 S7e：shortfall 的基准**固定为「展开卡片」**，与当前形态无关。
     *
     * 为什么不能直接用 resolved.shortfall：它按「当前 cardReserve」计算，
     * 而 cardReserve 又由卡片形态决定、形态又由 shortfall 决定 ——
     * 会形成 `shortfall → 形态 → cardReserve → shortfall` 的回路：
     * 卡片降级为胶囊后 shortfall 归零，下一帧又会想展开，来回抖动。
     * 固定基准后 shortfall 只依赖 hostW / 内容宽度偏好，是本 hook 的纯派生量。
     */
    const probe = contentColumnGeometry({
      hostW,
      preferredW: width,
      cardReserve: CARD_RESERVE_EXPANDED,
      pad: COLUMN_PAD,
    });
    const nextShortfall = Math.round(probe.shortfall);
    if (nextShortfall !== lastShortfallRef.current) {
      lastShortfallRef.current = nextShortfall;
      setShortfall(nextShortfall);
    }
  }, [hostRef, preferredW, cardReserve]);

  // 内容宽度偏好变化 = 用户显式调整，允许改宽度
  useEffect(() => { apply(true); }, [apply]);

  // 卡片占位变化：只改位置（宽度冻结）
  useEffect(() => { apply(false); }, [cardReserve, apply]);

  // 几何纪元：窗口缩放 / 拖面板收尾 / 面板过渡收尾 / 宿主尺寸突增
  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;

    let sawGeometryMotion = false;
    const bump = () => { apply(true); };

    const onWindowResize = () => bump();
    const onMotionStart = () => { sawGeometryMotion = true; };
    // 面板拖拽/过渡期间 `isBusy()` 为真：此时宽度本来就该跟着变，
    // 但逐帧写变量会让文字逐帧折行；收尾时一次性重算即可（拖拽中保持冻结，观感更稳）。
    const unsubscribeBus = subscribePerfBus((busy) => {
      if (busy) { sawGeometryMotion = true; return; }
      if (sawGeometryMotion) { sawGeometryMotion = false; bump(); }
    });

    const ro = typeof ResizeObserver !== "undefined"
      ? new ResizeObserver(() => {
          const next = hostWidthOf(host);
          const delta = Math.abs(next - lastHostRef.current);
          // 运动期只记账；静止期超过阈值的真实尺寸变化（字体加载、系统缩放、滚动条）才改宽度
          if (isBusy()) { sawGeometryMotion = true; return; }
          if (delta > HOST_SIZE_EPSILON) bump();
          else apply(false);
        })
      : null;
    ro?.observe(host);

    window.addEventListener("resize", onWindowResize);
    window.addEventListener("chatcoder:panel-drag-start", onMotionStart);
    window.addEventListener("chatcoder:pane-change-start", onMotionStart);
    // 首次挂载先求解一次宽度
    bump();

    return () => {
      window.removeEventListener("resize", onWindowResize);
      window.removeEventListener("chatcoder:panel-drag-start", onMotionStart);
      window.removeEventListener("chatcoder:pane-change-start", onMotionStart);
      unsubscribeBus();
      ro?.disconnect();
    };
  }, [apply, hostRef]);

  return { shortfall };
}
