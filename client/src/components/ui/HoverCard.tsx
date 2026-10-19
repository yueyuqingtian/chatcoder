/** HoverCard —— 悬停（或点击固定）弹出的富内容浮层（plan-20261019-toolcard-v2）。
 *
 * 为什么不用 Tooltip：Tooltip 的浮层不接收指针（遮挡时点击落到下层），放不了可点击的内容；
 * 工具卡里「已完成 N 项」这类折叠行，要悬停列出被折叠的条目、还要能点其中一项跳转，所以需要这一个。
 *
 * 交互约定：
 *  · 指针移入触发元素，停留 openDelay 后打开；移到浮层上不关闭（离开触发元素与浮层各 closeDelay 才关），
 *    所以浮层可以紧贴触发元素、也可以隔一小段距离；
 *  · 点击触发元素 = 固定：指针离开也不再关闭，直到点外部 / Esc / 再点一次；键盘也能用（聚焦即打开）；
 *  · 触摸设备不响应悬停，只响应点击固定。
 * 浮层由 ui/Floating 承载（portal + 固定定位），不受任何祖先 overflow 裁切。
 */
import {
  cloneElement, useEffect, useRef, useState,
  type FocusEvent as ReactFocusEvent, type PointerEvent as ReactPointerEvent, type ReactElement, type ReactNode, type RefObject,
} from "react";
import { Floating, type FloatingAlign, type FloatingSide } from "./Floating";

type Handler<E> = ((e: E) => void) | undefined;

function compose<E>(own: Handler<E>, ours: (e: E) => void) {
  return (e: E) => {
    own?.(e);
    ours(e);
  };
}

interface TriggerProps {
  onPointerEnter?: Handler<ReactPointerEvent<HTMLElement>>;
  onPointerLeave?: Handler<ReactPointerEvent<HTMLElement>>;
  onFocus?: Handler<ReactFocusEvent<HTMLElement>>;
  onBlur?: Handler<ReactFocusEvent<HTMLElement>>;
  onClick?: Handler<React.MouseEvent<HTMLElement>>;
  ref?: unknown;
}

export interface HoverCardProps {
  /** 触发元素（单个 React 元素，会被注入悬停 / 聚焦 / 点击处理与 ref） */
  trigger: ReactElement;
  /** 浮层内容 */
  children: ReactNode;
  side?: FloatingSide;
  align?: FloatingAlign;
  gap?: number;
  /** 左右弹出时的水平基准（默认触发元素自身） */
  edgeRef?: RefObject<HTMLElement | null>;
  openDelay?: number;
  closeDelay?: number;
  disabled?: boolean;
  className?: string;
  "aria-label"?: string;
}

export function HoverCard({
  trigger, children, side = "left", align = "start", gap = 8, edgeRef,
  openDelay = 120, closeDelay = 140, disabled = false, className = "", ...rest
}: HoverCardProps) {
  const anchorRef = useRef<HTMLElement | null>(null);
  const [open, setOpen] = useState(false);
  const pinned = useRef(false);
  const timer = useRef<number | null>(null);
  /** 计时到点读最新状态，避免闭包拿到旧值 */
  const openRef = useRef(open);
  openRef.current = open;

  const clear = () => {
    if (timer.current !== null) {
      window.clearTimeout(timer.current);
      timer.current = null;
    }
  };
  const close = () => {
    clear();
    pinned.current = false;
    setOpen(false);
  };
  const scheduleOpen = () => {
    clear();
    timer.current = window.setTimeout(() => { timer.current = null; setOpen(true); }, openDelay);
  };
  const scheduleClose = () => {
    if (pinned.current) return;
    clear();
    timer.current = window.setTimeout(() => { timer.current = null; setOpen(false); }, closeDelay);
  };

  useEffect(() => { if (disabled) close(); }, [disabled]);
  useEffect(() => clear, []);

  const child = trigger as ReactElement<TriggerProps>;
  const childRef = (child as unknown as { ref?: unknown }).ref;
  const setRef = (el: HTMLElement | null) => {
    anchorRef.current = el;
    if (typeof childRef === "function") (childRef as (v: HTMLElement | null) => void)(el);
    else if (childRef && typeof childRef === "object") (childRef as { current: HTMLElement | null }).current = el;
  };

  const handlers = {
    onPointerEnter: (e: ReactPointerEvent<HTMLElement>) => {
      if (disabled || e.pointerType === "touch") return;
      if (openRef.current) { clear(); return; }
      scheduleOpen();
    },
    onPointerLeave: () => scheduleClose(),
    onFocus: (e: ReactFocusEvent<HTMLElement>) => {
      // 只有键盘焦点才自动打开（鼠标点击聚焦不触发，点击另有「固定」语义）
      if (disabled || !e.currentTarget.matches(":focus-visible")) return;
      clear();
      setOpen(true);
    },
    onBlur: () => scheduleClose(),
    onClick: () => {
      if (disabled) return;
      clear();
      if (openRef.current && pinned.current) {
        close();
      } else {
        pinned.current = true;
        setOpen(true);
      }
    },
  };

  const cloned = cloneElement(child, {
    ref: setRef,
    "aria-expanded": open,
    "aria-haspopup": "dialog",
    onPointerEnter: compose(child.props.onPointerEnter, handlers.onPointerEnter),
    onPointerLeave: compose(child.props.onPointerLeave, handlers.onPointerLeave),
    onFocus: compose(child.props.onFocus, handlers.onFocus),
    onBlur: compose(child.props.onBlur, handlers.onBlur),
    onClick: compose(child.props.onClick, handlers.onClick),
  } as Partial<TriggerProps> & Record<string, unknown>);

  return (
    <>
      {cloned}
      <Floating
        open={open && !disabled}
        anchorRef={anchorRef}
        edgeRef={edgeRef}
        side={side}
        align={align}
        gap={gap}
        onClose={close}
        className={`ui-hovercard ${className}`.trim()}
        onMouseEnter={clear}
        onMouseLeave={scheduleClose}
        {...rest}
      >
        {children}
      </Floating>
    </>
  );
}
