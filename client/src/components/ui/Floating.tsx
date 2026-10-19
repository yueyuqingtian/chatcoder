/** Floating —— 锚定到任意元素的浮层（portal + 固定定位，plan-20261019-toolcard-v2）。
 *
 * 为什么不直接用 Popover：Popover 是相对最近定位祖先的 absolute 浮层，放进 `overflow: auto / hidden` 的容器
 * （工具卡的滚动体、右侧面板里的列表……）会被裁掉——上一轮「⋯ 菜单」就踩过这个坑。
 * 这里把浮层挂到 body，按锚点的实测矩形定位，不受任何祖先 overflow / transform / contain 影响。
 *
 *  · 定位：首选方向 side（left / right / top / bottom）+ 对齐 align（start / center / end）；
 *    首选方向放不下时翻到对侧，仍放不下夹进视口。`edgeRef` 让左右弹出时以另一个元素的边缘为基准——
 *    例如锚点是工具卡里的一行，浮层却要贴着整张卡片的左缘。
 *  · 跟随：滚动 / 缩放时按锚点最新位置重定位（rAF 合帧），锚点被卸载则关闭。
 *    **不**因滚动关闭——消息流流式输出时会持续自动滚动，一滚就关会让浮层根本没法用。
 *  · 关闭：点外部（pointerdown 捕获）、Esc（只关栈顶）；锚点自身不算外部（由调用方切换开关）。
 *  · 进出场：usePresence + .mo-pop，退场动画播完再卸载。
 *
 * 位移只走 transform（外层 left/top 恒为 0）：浮层宽度由内容与 max-width 决定，与落点无关，
 * 避免「测量 ↔ 定位」反馈环（见 Tooltip 同款说明）。
 */
import {
  useCallback, useEffect, useLayoutEffect, useRef, useState,
  type KeyboardEventHandler, type MouseEventHandler, type ReactNode, type RefObject,
} from "react";
import { createPortal } from "react-dom";
import { usePresence } from "./usePresence";

export type FloatingSide = "left" | "right" | "top" | "bottom";
export type FloatingAlign = "start" | "center" | "end";

/** 浮层与视口边缘的最小留白（px） */
const EDGE = 8;

interface Placement { left: number; top: number; side: FloatingSide }

const OPPOSITE: Record<FloatingSide, FloatingSide> = { left: "right", right: "left", top: "bottom", bottom: "top" };

const clamp = (v: number, min: number, max: number) => Math.min(Math.max(v, min), max);

/** 纯函数：按锚点 / 基准边缘 / 浮层尺寸算落点（便于推理，不读 DOM）。 */
export function placeFloating(
  side: FloatingSide, align: FloatingAlign, gap: number,
  anchor: DOMRect, edge: DOMRect, w: number, h: number, vw: number, vh: number,
): Placement {
  const fits = (s: FloatingSide): boolean => {
    if (s === "left") return edge.left - gap - w >= EDGE;
    if (s === "right") return edge.right + gap + w <= vw - EDGE;
    if (s === "top") return anchor.top - gap - h >= EDGE;
    return anchor.bottom + gap + h <= vh - EDGE;
  };
  let s = side;
  if (!fits(s) && fits(OPPOSITE[s])) s = OPPOSITE[s];
  let left: number;
  let top: number;
  if (s === "left" || s === "right") {
    left = s === "left" ? edge.left - gap - w : edge.right + gap;
    top = align === "start" ? anchor.top : align === "end" ? anchor.bottom - h : anchor.top + (anchor.height - h) / 2;
  } else {
    top = s === "top" ? anchor.top - gap - h : anchor.bottom + gap;
    left = align === "start" ? anchor.left : align === "end" ? anchor.right - w : anchor.left + (anchor.width - w) / 2;
  }
  return {
    left: clamp(left, EDGE, Math.max(EDGE, vw - EDGE - w)),
    top: clamp(top, EDGE, Math.max(EDGE, vh - EDGE - h)),
    side: s,
  };
}

/** 打开中的浮层栈：Esc 只关闭栈顶 */
const openStack: Array<{ id: number; close: () => void }> = [];
let seq = 0;

export interface FloatingProps {
  open: boolean;
  /** 锚点元素：浮层相对它定位 */
  anchorRef: RefObject<HTMLElement | null>;
  /** 左右弹出时的水平基准（默认同锚点） */
  edgeRef?: RefObject<HTMLElement | null>;
  onClose?: () => void;
  side?: FloatingSide;
  align?: FloatingAlign;
  /** 与锚点 / 基准边缘的间距（px） */
  gap?: number;
  className?: string;
  role?: string;
  "aria-label"?: string;
  /** 点外部关闭（默认 true） */
  closeOnOutside?: boolean;
  /** 额外不视为「外部」的元素（锚点本身总是被忽略） */
  ignoreRefs?: Array<RefObject<HTMLElement | null>>;
  /** 允许 Esc 关闭（默认 true） */
  closeOnEscape?: boolean;
  onMouseEnter?: MouseEventHandler<HTMLDivElement>;
  onMouseLeave?: MouseEventHandler<HTMLDivElement>;
  onKeyDown?: KeyboardEventHandler<HTMLDivElement>;
  children: ReactNode;
}

export function Floating({
  open, anchorRef, edgeRef, onClose, side = "bottom", align = "start", gap = 8,
  className = "", role = "dialog", closeOnOutside = true, ignoreRefs, closeOnEscape = true,
  onMouseEnter, onMouseLeave, onKeyDown, children, ...rest
}: FloatingProps) {
  const presence = usePresence(open, 140);
  const ref = useRef<HTMLDivElement>(null);
  const [place, setPlace] = useState<Placement | null>(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;

  /** 取锚点与浮层的最新矩形重新落位；锚点已卸载返回 false。 */
  const reposition = useCallback((): boolean => {
    const el = ref.current;
    const anchor = anchorRef.current;
    if (!el || !anchor || !anchor.isConnected) return false;
    const a = anchor.getBoundingClientRect();
    const e = edgeRef?.current?.getBoundingClientRect() ?? a;
    const next = placeFloating(side, align, gap, a, e, el.offsetWidth, el.offsetHeight, window.innerWidth, window.innerHeight);
    setPlace((prev) => (
      prev && prev.left === next.left && prev.top === next.top && prev.side === next.side ? prev : next
    ));
    return true;
  }, [anchorRef, edgeRef, side, align, gap]);

  // 打开 / 内容尺寸变化后落位（layout 阶段，首帧即在正确位置）
  useLayoutEffect(() => {
    if (!open || !presence.mounted) return;
    reposition();
  }, [open, presence.mounted, reposition, children]);

  // 打开期间：跟随锚点移动；浮层自身尺寸变化（列表筛选）也重新落位
  useEffect(() => {
    if (!open) return;
    let raf = 0;
    const schedule = () => {
      if (raf) return;
      raf = requestAnimationFrame(() => {
        raf = 0;
        if (!reposition()) closeRef.current?.();
      });
    };
    window.addEventListener("scroll", schedule, true);
    window.addEventListener("resize", schedule);
    const ro = typeof ResizeObserver !== "undefined" && ref.current ? new ResizeObserver(schedule) : null;
    if (ro && ref.current) ro.observe(ref.current);
    return () => {
      if (raf) cancelAnimationFrame(raf);
      window.removeEventListener("scroll", schedule, true);
      window.removeEventListener("resize", schedule);
      ro?.disconnect();
    };
  }, [open, presence.mounted, reposition]);

  // 点外部 + Esc（Esc 只关栈顶）
  useEffect(() => {
    if (!open) return;
    const id = ++seq;
    openStack.push({ id, close: () => closeRef.current?.() });
    const onDown = (e: PointerEvent) => {
      if (!closeOnOutside) return;
      const t = e.target as Node | null;
      if (!t) return;
      if (ref.current?.contains(t)) return;
      if (anchorRef.current?.contains(t)) return;
      if (ignoreRefs?.some((r) => r.current?.contains(t))) return;
      closeRef.current?.();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || !closeOnEscape) return;
      const top = openStack[openStack.length - 1];
      if (top?.id !== id) return;
      e.stopPropagation();
      closeRef.current?.();
    };
    document.addEventListener("pointerdown", onDown, true);
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("pointerdown", onDown, true);
      document.removeEventListener("keydown", onKey, true);
      const i = openStack.findIndex((x) => x.id === id);
      if (i >= 0) openStack.splice(i, 1);
    };
  }, [open, closeOnOutside, closeOnEscape, anchorRef, ignoreRefs]);

  // 重新打开时先清掉旧落点，避免短暂停在上一次的位置
  useEffect(() => {
    if (!open) return;
    setPlace(null);
  }, [open]);

  if (!presence.mounted) return null;
  return createPortal(
    <div
      className="ui-floating-anchor"
      style={{
        transform: place ? `translate3d(${place.left}px, ${place.top}px, 0)` : undefined,
        visibility: place ? "visible" : "hidden",
      }}
    >
      <div
        ref={ref}
        className={`ui-floating mo-pop ${className}`.trim()}
        data-state={presence.state}
        data-side={place?.side ?? side}
        role={role}
        onMouseEnter={onMouseEnter}
        onMouseLeave={onMouseLeave}
        onKeyDown={onKeyDown}
        {...rest}
      >
        {children}
      </div>
    </div>,
    document.body,
  );
}
