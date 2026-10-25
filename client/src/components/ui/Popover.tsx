/** Popover —— 统一浮层（plan-20261009-ui U4）。
 *
 * 取代输入框、侧栏、胶囊里各自手写的「absolute 菜单 + 只有入场」：
 *  - 定位：相对最近的定位祖先（触发器外层 position: relative），placement 决定贴边方向与动画原点；
 *  - 碰撞：挂载后测一次，越出「可见范围」则沿主轴翻转（上 ↔ 下 / 右 ↔ 左，仅当对侧空间更大），
 *    交叉轴再平移回可见范围。可见范围 = 视口 ∩ 会裁切它的祖先（plan-20261025-popclip：窄窗口下
 *    中间卡片的 overflow:hidden 会把越过卡片边缘的浮层直接裁掉，只看视口判断不出来）；
 *  - 关闭：点外部（pointerdown 捕获阶段）+ Esc；嵌套浮层渲染为父浮层的 DOM 后代，
 *    点击子浮层不算父浮层「外部」，Esc 只关闭最上层；
 *  - 进出场：usePresence 驱动 data-state，退场动画播完再卸载。 */
import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties, type KeyboardEventHandler, type ReactNode, type RefObject } from "react";
import { usePresence } from "./usePresence";
import { isTopLayer, pushLayer, removeLayer } from "../../utils/layerStack";

export type PopoverPlacement =
  | "top-start" | "top-end" | "top-center"
  | "bottom-start" | "bottom-end" | "bottom-center"
  | "right-start" | "right-end" | "left-start" | "left-end";

/** 打开中的浮层栈：Esc 只关闭栈顶。
 *  plan-562-2318 B6：栈与 id 序列已收敛到 `utils/layerStack`，与 Floating 共用一份——
 *  此前两个组件各持一份，同时打开时一次 Esc 会把两层都关掉。 */

function flipPlacement(p: PopoverPlacement, axis: "main" | "cross"): PopoverPlacement {
  const [side, align] = p.split("-") as [string, string];
  if (axis === "main") {
    const opposite: Record<string, string> = { top: "bottom", bottom: "top", left: "right", right: "left" };
    return `${opposite[side]}-${align}` as PopoverPlacement;
  }
  const alt: Record<string, string> = { start: "end", end: "start", center: "center" };
  return `${side}-${alt[align]}` as PopoverPlacement;
}

interface Bounds { left: number; top: number; right: number; bottom: number }

/** 浮层真正看得见的范围：视口与所有会裁切它的祖先（overflow 非 visible）的交集。
 *  只沿包含块链向上找——绝对定位元素不受「包含块之下」的 overflow 祖先裁切；遇到 fixed 祖先即止（其上的祖先裁不到它）。 */
function visibleBounds(el: HTMLElement): Bounds {
  const b: Bounds = { left: 0, top: 0, right: window.innerWidth, bottom: window.innerHeight };
  let node = el.offsetParent as HTMLElement | null;
  while (node && node !== document.body && node !== document.documentElement) {
    const cs = getComputedStyle(node);
    if (cs.overflowX !== "visible" || cs.overflowY !== "visible") {
      const r = node.getBoundingClientRect();
      if (cs.overflowX !== "visible") { b.left = Math.max(b.left, r.left); b.right = Math.min(b.right, r.right); }
      if (cs.overflowY !== "visible") { b.top = Math.max(b.top, r.top); b.bottom = Math.min(b.bottom, r.bottom); }
    }
    if (cs.position === "fixed") break;
    node = (cs.position === "absolute" ? node.offsetParent : node.parentElement) as HTMLElement | null;
  }
  return b;
}

export interface PopoverProps {
  open: boolean;
  onClose: () => void;
  children: ReactNode;
  placement?: PopoverPlacement;
  className?: string;
  style?: CSSProperties;
  /** 不视为「外部」的元素（通常是触发按钮——避免点按钮时先关后开） */
  ignoreRefs?: Array<RefObject<HTMLElement | null>>;
  /** 点外部关闭（默认 true） */
  closeOnOutside?: boolean;
  /** 无障碍角色（菜单用 menu，选择器用 listbox / dialog） */
  role?: string;
  "aria-label"?: string;
  onMouseEnter?: () => void;
  onMouseLeave?: () => void;
  /** plan-156-621：键盘支持——菜单打开后可用方向键在选项间移动 */
  onKeyDown?: KeyboardEventHandler<HTMLDivElement>;
  tabIndex?: number;
}

export function Popover({
  open, onClose, children, placement = "top-start", className = "", style,
  ignoreRefs, closeOnOutside = true, role = "dialog", onMouseEnter, onMouseLeave, ...rest
}: PopoverProps) {
  const presence = usePresence(open, 140);
  const ref = useRef<HTMLDivElement>(null);
  const [actual, setActual] = useState<PopoverPlacement>(placement);
  const [shift, setShift] = useState({ x: 0, y: 0 });
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  // plan-562-2318 B7：`ignoreRefs` 只走 ref，不进 effect 依赖（调用方普遍传内联数组）
  const ignoreRefsRef = useRef(ignoreRefs);
  ignoreRefsRef.current = ignoreRefs;

  // 打开时按首选方向重置，再做一次碰撞检测（只测一次，不随内容抖动）
  useLayoutEffect(() => {
    if (!open) return;
    setActual(placement);
    setShift({ x: 0, y: 0 });
  }, [open, placement]);

  useLayoutEffect(() => {
    if (!open || !presence.mounted) return;
    const el = ref.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    const anchor = (el.offsetParent as HTMLElement | null)?.getBoundingClientRect() ?? r;
    const b = visibleBounds(el);
    const pad = 8;
    const side = actual.split("-")[0];
    // 主轴：首选方向放不下、且对侧空间更大才翻转——两侧都放不下时留在空间大的一侧（也避免来回翻转）
    const flip =
      (side === "top" && r.top < b.top + pad && b.bottom - anchor.bottom > anchor.top - b.top)
      || (side === "bottom" && r.bottom > b.bottom - pad && anchor.top - b.top > b.bottom - anchor.bottom)
      || (side === "right" && r.right > b.right - pad && anchor.left - b.left > b.right - anchor.right)
      || (side === "left" && r.left < b.left + pad && b.right - anchor.right > anchor.left - b.left);
    if (flip) { setActual(flipPlacement(actual, "main")); return; }
    // 交叉轴平移回可见范围；左右弹出的二级浮层两侧都放不下时，退而求其次横向压回范围内（盖住一部分父浮层也比被裁掉强）
    const fit = (lo: number, hi: number, min: number, max: number): number => {
      if (hi > max) return Math.max(max - hi, min - lo);
      if (lo < min) return min - lo;
      return 0;
    };
    const x = fit(r.left, r.right, b.left + pad, b.right - pad);
    const y = side === "left" || side === "right" ? fit(r.top, r.bottom, b.top + pad, b.bottom - pad) : 0;
    if (x !== 0 || y !== 0) setShift({ x, y });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, presence.mounted, actual]);

  // 点外部 + Esc（Esc 只关栈顶）
  useEffect(() => {
    if (!open) return;
    // plan-562-2318 B7：`ignoreRefs` 不进依赖——调用方多传内联数组（`[btnRef]`），
    // 父组件每次渲染都会换新引用，effect 就会反复注销/重注册文档监听，并把本浮层
    // 重新压到栈顶（叠加 B6 会让 Esc 关错层）。这里改读 ref 里的最新值。
    const id = pushLayer(() => closeRef.current());
    const onDown = (e: PointerEvent) => {
      if (!closeOnOutside) return;
      const t = e.target as Node | null;
      if (!t) return;
      if (ref.current?.contains(t)) return;
      if (ignoreRefsRef.current?.some((r) => r.current?.contains(t))) return;
      closeRef.current();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      // plan-562-2318 B6：栈顶判定走共享模块——与 Floating 同一份栈
      if (!isTopLayer(id)) return;
      e.stopPropagation();
      closeRef.current();
    };
    document.addEventListener("pointerdown", onDown, true);
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("pointerdown", onDown, true);
      document.removeEventListener("keydown", onKey, true);
      removeLayer(id);
    };
  }, [open, closeOnOutside]);

  if (!presence.mounted) return null;
  return (
    <div
      ref={ref}
      className={`ui-pop mo-pop pl-${actual} ${className}`.trim()}
      data-state={presence.state}
      role={role}
      style={shift.x || shift.y
        ? { ...style, ["--pop-shift-x" as string]: `${shift.x}px`, ["--pop-shift-y" as string]: `${shift.y}px` }
        : style}
      onMouseEnter={onMouseEnter}
      onMouseLeave={onMouseLeave}
      {...rest}
    >
      {children}
    </div>
  );
}
