/** Tooltip —— 全站唯一的提示浮层（plan-282-1530 统一）
 *
 * 此前有两套：Radix 包装（带箭头；悬停内容可移入，移入即常驻遮挡下层、聚焦即弹出）与
 * 存量 title 接管（无箭头）。现统一为一套样式与一套行为：
 *  - 样式：圆角卡片、无箭头（.ui-tooltip）；
 *  - 时序：指针停留或键盘聚焦达到 --tooltip-delay 才展示——划过与聚焦都不立即弹出；
 *  - 点击：指针按下即收起，直到指针离开该元素之前不再展示（点击后要操作的是下层内容）；
 *  - 穿透：浮层 pointer-events:none，遮挡下层内容时指针与点击照常落到下层；
 *  - 收起：指针离开触发元素、Esc、滚动、窗口缩放。
 *
 * 两个入口共用同一个状态源与同一个宿主（TooltipHost）：
 *  - <Tooltip content> 包裹元素，挂事件处理；
 *  - NativeTitleTooltip 接管全站存量 title 属性，同样的时序与浮层。
 */
import {
  cloneElement,
  isValidElement,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type FocusEvent as ReactFocusEvent,
  type PointerEvent as ReactPointerEvent,
  type ReactElement,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";

type Side = "top" | "right" | "bottom" | "left";

interface Rect { left: number; top: number; right: number; bottom: number; }

interface TipState {
  text: ReactNode;
  side: Side;
  /** 触发元素：用于判定归属（只有它能收起/更新本提示） */
  el: Element;
  /** 展示瞬间的触发元素矩形（滚动 / 缩放时直接收起，不做重定位） */
  anchor: Rect;
}

/** 浮层与触发元素的间距、距视口边缘的留白（px） */
const GAP = 8;
const EDGE = 8;

/* ── 统一状态源：同一时刻只有一条提示 ─────────────────────── */

let tipState: TipState | null = null;
const tipListeners = new Set<() => void>();

function emitTip() {
  for (const l of Array.from(tipListeners)) l();
}

function subscribeTip(listener: () => void) {
  tipListeners.add(listener);
  return () => { tipListeners.delete(listener); };
}

function getTipSnapshot(): TipState | null {
  return tipState;
}

function rectOf(el: Element): Rect {
  const r = el.getBoundingClientRect();
  return { left: r.left, top: r.top, right: r.right, bottom: r.bottom };
}

/** 展示提示：文本与触发元素绑定 */
function showTip(el: Element, text: ReactNode, side: Side) {
  tipState = { text, side, el, anchor: rectOf(el) };
  emitTip();
}

/** 文本在展示期间变化：就地更新，不重新计时 */
function updateTipText(el: Element, text: ReactNode) {
  if (!tipState || tipState.el !== el || tipState.text === text) return;
  tipState = { ...tipState, text };
  emitTip();
}

/** 收起提示；传入元素时仅当它是当前触发元素才收起（不误关别的提示） */
function hideTip(el?: Element) {
  if (!tipState) return;
  if (el && tipState.el !== el) return;
  tipState = null;
  emitTip();
}

/** 统一延迟令牌 --tooltip-delay（支持 ms / s 单位，单源控制） */
function readDelay(): number {
  if (typeof window === "undefined") return 300;
  const raw = getComputedStyle(document.documentElement).getPropertyValue("--tooltip-delay").trim();
  const value = Number.parseFloat(raw);
  if (!Number.isFinite(value)) return 300;
  return raw.endsWith("s") && !raw.endsWith("ms") ? value * 1000 : value;
}

function clamp(v: number, min: number, max: number): number {
  return Math.min(Math.max(v, min), max);
}

/** 落点：优先放在首选方向；首选方向放不下且另一侧更宽裕时翻转；水平、垂直都夹取进视口 */
function placeTip(side: Side, a: Rect, w: number, h: number): { left: number; top: number } {
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const cx = a.left + (a.right - a.left) / 2;
  const cy = a.top + (a.bottom - a.top) / 2;
  const spaceBelow = vh - EDGE - (a.bottom + GAP);
  const spaceAbove = a.top - GAP - EDGE;
  let placed = side;
  if (side === "bottom" && h > spaceBelow && spaceAbove > spaceBelow) placed = "top";
  if (side === "top" && h > spaceAbove && spaceBelow > spaceAbove) placed = "bottom";
  let left: number;
  let top: number;
  if (placed === "top") { left = cx - w / 2; top = a.top - GAP - h; }
  else if (placed === "bottom") { left = cx - w / 2; top = a.bottom + GAP; }
  else if (placed === "left") { left = a.left - GAP - w; top = cy - h / 2; }
  else { left = a.right + GAP; top = cy - h / 2; }
  return {
    left: clamp(left, EDGE, Math.max(EDGE, vw - EDGE - w)),
    top: clamp(top, EDGE, Math.max(EDGE, vh - EDGE - h)),
  };
}

/* ── 宿主：全站唯一的浮层渲染点（TooltipProvider 挂载一次） ───── */

export function TooltipHost() {
  const tip = useSyncExternalStore(subscribeTip, getTipSnapshot, getTipSnapshot);
  const innerRef = useRef<HTMLDivElement | null>(null);
  /** 浮层实测尺寸：先测、再定位；落点只读尺寸，不反过来改尺寸（避免测量 ↔ 定位反馈环） */
  const [box, setBox] = useState<{ w: number; h: number } | null>(null);
  const boxRef = useRef<{ w: number; h: number } | null>(null);

  useLayoutEffect(() => {
    const el = innerRef.current;
    if (!tip || !el) {
      boxRef.current = null;
      setBox(null);
      return;
    }
    const measure = () => {
      const w = el.offsetWidth;
      const h = el.offsetHeight;
      const prev = boxRef.current;
      if (prev && prev.w === w && prev.h === h) return;
      boxRef.current = { w, h };
      setBox({ w, h });
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [tip]);

  const pos = useMemo(() => {
    if (!tip) return null;
    return placeTip(tip.side, tip.anchor, box?.w ?? 0, box?.h ?? 0);
  }, [tip, box]);

  // 展示期间：滚动、缩放、Esc 一律收起（浮层不跟随移动，避免停在错误位置）
  useEffect(() => {
    if (!tip) return;
    const close = () => hideTip();
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") hideTip(); };
    window.addEventListener("scroll", close, true);
    window.addEventListener("resize", close);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("scroll", close, true);
      window.removeEventListener("resize", close);
      window.removeEventListener("keydown", onKey);
    };
  }, [tip]);

  if (!tip || !pos) return null;
  // Portal 到 body：不受祖先 transform / overflow 裁切；位移只走 transform（见 components.css）
  return createPortal(
    <div className="ui-tooltip-anchor" style={{ transform: `translate3d(${pos.left}px, ${pos.top}px, 0)` }}>
      <div ref={innerRef} className="ui-tooltip" role="tooltip" style={{ visibility: box ? "visible" : "hidden" }}>
        {tip.text}
      </div>
    </div>,
    document.body,
  );
}

/* ── <Tooltip>：包裹元素，挂事件处理 ─────────────────────── */

interface TooltipProps {
  /** 浮层内容；与 `title` 二选一（同时给出时以 content 为准） */
  content?: ReactNode;
  /** 便捷属性：纯文本提示（迁移存量 title 用） */
  title?: string | number | null;
  children: ReactNode;
  side?: Side;
  /** 停留（或聚焦）后展示的延迟（ms）；默认取令牌 --tooltip-delay */
  delay?: number;
  disabled?: boolean;
}

type Handler<E> = ((e: E) => void) | undefined;

function compose<E>(own: Handler<E>, ours: (e: E) => void) {
  return (e: E) => {
    own?.(e);
    ours(e);
  };
}

function TooltipTrigger({ text, enabled, side, delay, children }: {
  text: ReactNode;
  enabled: boolean;
  side: Side;
  delay?: number;
  children: ReactNode;
}) {
  const timer = useRef<number | null>(null);
  /** 点击后为真：直到指针离开该元素之前不再展示（点击后要操作的是下层内容） */
  const suppressed = useRef(false);
  /** 当前持有提示的触发元素（进入时记录，离开 / 卸载时释放） */
  const elRef = useRef<Element | null>(null);
  /** 计时到点时读取最新值，避免闭包拿到旧文本 / 旧开关 */
  const latest = useRef({ text, enabled, side, delay });
  latest.current = { text, enabled, side, delay };

  const clear = () => {
    if (timer.current !== null) {
      window.clearTimeout(timer.current);
      timer.current = null;
    }
  };

  // 卸载：取消计时，并收起本元素的提示
  useEffect(() => () => {
    if (timer.current !== null) window.clearTimeout(timer.current);
    if (elRef.current) hideTip(elRef.current);
  }, []);

  // 内容或开关变化：展示中的提示就地更新；关闭时立即收起
  useEffect(() => {
    const el = elRef.current;
    if (!el) return;
    if (!enabled || text === null) {
      clear();
      hideTip(el);
    } else {
      updateTipText(el, text);
    }
  }, [text, enabled]);

  /** 进入 / 聚焦：计时，到点仍在且未被点击压制才展示 */
  const schedule = (el: Element) => {
    if (!latest.current.enabled || latest.current.text === null) return;
    clear();
    elRef.current = el;
    timer.current = window.setTimeout(() => {
      timer.current = null;
      const now = latest.current;
      if (suppressed.current || !now.enabled || now.text === null || !el.isConnected) return;
      showTip(el, now.text, now.side);
    }, latest.current.delay ?? readDelay());
  };

  const leave = (el: Element) => {
    clear();
    suppressed.current = false;
    elRef.current = null;
    hideTip(el);
  };

  const handlers = {
    onPointerEnter: (e: ReactPointerEvent<Element>) => {
      if (e.pointerType === "touch") return;
      suppressed.current = false;
      schedule(e.currentTarget);
    },
    onPointerLeave: (e: ReactPointerEvent<Element>) => leave(e.currentTarget),
    onPointerDown: (e: ReactPointerEvent<Element>) => {
      suppressed.current = true;
      clear();
      hideTip(e.currentTarget);
    },
    // 只有键盘焦点才提示（鼠标点击聚焦不提示）；且同样等待延迟，聚焦不会立即弹出
    onFocus: (e: ReactFocusEvent<Element>) => {
      if (suppressed.current || !e.currentTarget.matches(":focus-visible")) return;
      schedule(e.currentTarget);
    },
    onBlur: (e: ReactFocusEvent<Element>) => leave(e.currentTarget),
  };

  if (!isValidElement(children)) {
    return <span {...handlers}>{children}</span>;
  }
  const child = children as ReactElement<{
    onPointerEnter?: Handler<ReactPointerEvent<Element>>;
    onPointerLeave?: Handler<ReactPointerEvent<Element>>;
    onPointerDown?: Handler<ReactPointerEvent<Element>>;
    onFocus?: Handler<ReactFocusEvent<Element>>;
    onBlur?: Handler<ReactFocusEvent<Element>>;
  }>;
  return cloneElement(child, {
    onPointerEnter: compose(child.props.onPointerEnter, handlers.onPointerEnter),
    onPointerLeave: compose(child.props.onPointerLeave, handlers.onPointerLeave),
    onPointerDown: compose(child.props.onPointerDown, handlers.onPointerDown),
    onFocus: compose(child.props.onFocus, handlers.onFocus),
    onBlur: compose(child.props.onBlur, handlers.onBlur),
  });
}

export function Tooltip({ content, title, children, side = "bottom", delay, disabled = false }: TooltipProps) {
  const text = content ?? (title === null || title === undefined || title === "" ? null : String(title));
  // 始终渲染同一个触发器：disabled 切换只开关提示，不改变子树结构（否则子元素会重挂载、丢失焦点）
  return (
    <TooltipTrigger text={text} enabled={!disabled && text !== null && text !== ""} side={side} delay={delay}>
      {children}
    </TooltipTrigger>
  );
}

/** 顶层挂载点（App 内一次）：提供统一浮层宿主 */
export function TooltipProvider({ children }: { children: ReactNode }) {
  return (
    <>
      {children}
      <TooltipHost />
    </>
  );
}

/* ── 存量原生 title 的统一接管 ───────────────────────────── */

/** 暂存原生 title 的 dataset 键（接管期间移除原生属性，离开时还原） */
const STASH_KEY = "tipStash";
/** 巡检间隔（ms）：元素被虚拟化回收 / 重挂载时按指针位置重新接管 */
const POLL_MS = 200;

/** 可被接管的元素：图标按钮的提示属性可能挂在 svg 根上，因此接受任意 SVG / HTML 元素 */
type TipOwner = HTMLElement | SVGElement;

/** 从事件目标向上找最近的「带原生 title 或已被接管」的元素 */
function findOwner(target: EventTarget | null): TipOwner | null {
  const start = target instanceof Element ? target : target instanceof Node ? target.parentElement : null;
  let el: Element | null = start;
  while (el && el !== document.documentElement) {
    if ((el instanceof HTMLElement || el instanceof SVGElement)
      && (el.dataset[STASH_KEY] !== undefined || el.hasAttribute("title"))) return el;
    el = el.parentElement;
  }
  return null;
}

/** 接管全站原生 title：指针进入时暂存并移除原生属性，改由统一浮层展示；离开时还原。
 *  与 <Tooltip> 共用同一宿主与时序；键盘聚焦与无障碍名称不受影响。新代码仍建议直接用 <Tooltip>。 */
export function NativeTitleTooltip() {
  useEffect(() => {
    /** 当前接管的元素（其 title 已被暂存移除） */
    let owner: TipOwner | null = null;
    /** 刚被点击的元素：离开它之前不再展示 */
    let clicked: TipOwner | null = null;
    /** 本模块刚还原过 title 的元素：守卫忽略这次自身写入，否则指针仍在元素上时会立刻重新接管 */
    let selfWritten: TipOwner | null = null;
    let pointer: { x: number; y: number } | null = null;
    let timer: number | null = null;
    let poll: number | null = null;
    let guard: MutationObserver | null = null;

    const cancel = () => {
      if (timer !== null) {
        window.clearTimeout(timer);
        timer = null;
      }
    };

    const stopPoll = () => {
      if (poll !== null) {
        window.clearInterval(poll);
        poll = null;
      }
    };

    /** 还原被暂存的原生 title（元素可能已卸载，故做存在性判断） */
    const restore = () => {
      const el = owner;
      if (el && el.dataset[STASH_KEY] !== undefined) {
        selfWritten = el;
        el.setAttribute("title", el.dataset[STASH_KEY] ?? "");
        delete el.dataset[STASH_KEY];
      }
      owner = null;
    };

    const hide = () => {
      cancel();
      stopPoll();
      restore();
      hideTip();
    };

    const startPoll = () => {
      if (poll !== null) return;
      poll = window.setInterval(() => {
        if (!owner) { stopPoll(); return; }
        if (owner.isConnected) return;
        const pt = pointer;
        const next = pt ? findOwner(document.elementFromPoint(pt.x, pt.y)) : null;
        hide();
        if (next && next !== clicked) attach(next);
      }, POLL_MS);
    };

    /** 接管：暂存并移除原生 title，计时后展示 */
    const attach = (el: TipOwner) => {
      const text = (el.dataset[STASH_KEY] ?? el.getAttribute("title") ?? "").trim();
      if (!text) return;
      el.dataset[STASH_KEY] = text;
      el.removeAttribute("title");
      owner = el;
      cancel();
      timer = window.setTimeout(() => {
        timer = null;
        if (owner === el && el.isConnected) showTip(el, text, "bottom");
      }, readDelay());
      startPoll();
    };

    /** 守卫：React 重渲染把 title 写回、或指针已停在元素上时 title 才被写入，都按接管处理 */
    const startGuard = () => {
      guard?.disconnect();
      guard = new MutationObserver((records) => {
        for (const r of records) {
          const el = r.target;
          if (!(el instanceof HTMLElement || el instanceof SVGElement)) continue;
          if (el === selfWritten) { selfWritten = null; continue; }
          const fresh = el.getAttribute("title");
          if (fresh == null) continue;
          const text = fresh.trim();
          if (el === owner) {
            el.removeAttribute("title");
            if (!text) continue;
            el.dataset[STASH_KEY] = text;
            updateTipText(el, text);
            continue;
          }
          if (owner || el === clicked || !text) continue;
          const pt = pointer;
          if (!pt || pt.x < 0 || pt.y < 0 || pt.x > window.innerWidth || pt.y > window.innerHeight) continue;
          const under = document.elementFromPoint(pt.x, pt.y);
          if (under && (under === el || el.contains(under))) attach(el);
        }
      });
      guard.observe(document.documentElement, { subtree: true, attributes: true, attributeFilter: ["title"] });
    };

    const onOver = (ev: PointerEvent) => {
      pointer = { x: ev.clientX, y: ev.clientY };
      const el = findOwner(ev.target);
      if (el && el === owner) return; // 元素内移动：沿用当前接管
      hide();
      if (el && el !== clicked) attach(el);
    };

    const onOut = (ev: PointerEvent) => {
      if (clicked && ev.target instanceof Node && clicked.contains(ev.target)) {
        const next = ev.relatedTarget;
        if (!(next instanceof Node && clicked.contains(next))) clicked = null;
      }
      if (!owner) return;
      const next = ev.relatedTarget;
      if (next instanceof Node && owner.contains(next)) return; // 移入子元素不算离开
      hide();
    };

    const onMove = (ev: PointerEvent) => {
      pointer = { x: ev.clientX, y: ev.clientY };
    };

    const onDown = (ev: PointerEvent) => {
      clicked = findOwner(ev.target);
      hide();
    };

    const onKey = (ev: KeyboardEvent) => {
      if (ev.key === "Escape") hide();
    };

    startGuard();
    document.addEventListener("pointerover", onOver, true);
    document.addEventListener("pointerout", onOut, true);
    document.addEventListener("pointermove", onMove, true);
    document.addEventListener("pointerdown", onDown, true);
    document.addEventListener("keydown", onKey);
    window.addEventListener("scroll", hide, true);
    window.addEventListener("resize", hide);
    return () => {
      document.removeEventListener("pointerover", onOver, true);
      document.removeEventListener("pointerout", onOut, true);
      document.removeEventListener("pointermove", onMove, true);
      document.removeEventListener("pointerdown", onDown, true);
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("scroll", hide, true);
      window.removeEventListener("resize", hide);
      guard?.disconnect();
      guard = null;
      stopPoll();
      hide();
    };
  }, []);

  return null;
}
