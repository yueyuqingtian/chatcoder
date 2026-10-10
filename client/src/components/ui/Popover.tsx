/** Popover —— 统一浮层（plan-20261009-ui U4）。
 *
 * 取代输入框、侧栏、胶囊里各自手写的「absolute 菜单 + 只有入场」：
 *  - 定位：相对最近的定位祖先（触发器外层 position: relative），placement 决定贴边方向与动画原点；
 *  - 碰撞：挂载后测一次，越出视口则沿主轴翻转（上 ↔ 下 / 右 ↔ 左），横向越界再夹回视口；
 *  - 关闭：点外部（pointerdown 捕获阶段）+ Esc；嵌套浮层渲染为父浮层的 DOM 后代，
 *    点击子浮层不算父浮层「外部」，Esc 只关闭最上层；
 *  - 进出场：usePresence 驱动 data-state，退场动画播完再卸载。 */
import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties, type ReactNode, type RefObject } from "react";
import { usePresence } from "./usePresence";

export type PopoverPlacement =
  | "top-start" | "top-end" | "top-center"
  | "bottom-start" | "bottom-end" | "bottom-center"
  | "right-start" | "right-end" | "left-start" | "left-end";

/** 打开中的浮层栈：Esc 只关闭栈顶 */
const openStack: Array<{ id: number; close: () => void }> = [];
let seq = 0;

function flipPlacement(p: PopoverPlacement, axis: "main" | "cross"): PopoverPlacement {
  const [side, align] = p.split("-") as [string, string];
  if (axis === "main") {
    const opposite: Record<string, string> = { top: "bottom", bottom: "top", left: "right", right: "left" };
    return `${opposite[side]}-${align}` as PopoverPlacement;
  }
  const alt: Record<string, string> = { start: "end", end: "start", center: "center" };
  return `${side}-${alt[align]}` as PopoverPlacement;
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
}

export function Popover({
  open, onClose, children, placement = "top-start", className = "", style,
  ignoreRefs, closeOnOutside = true, role = "dialog", onMouseEnter, onMouseLeave, ...rest
}: PopoverProps) {
  const presence = usePresence(open, 140);
  const ref = useRef<HTMLDivElement>(null);
  const [actual, setActual] = useState<PopoverPlacement>(placement);
  const [shiftX, setShiftX] = useState(0);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;

  // 打开时按首选方向重置，再做一次碰撞检测（只测一次，不随内容抖动）
  useLayoutEffect(() => {
    if (!open) return;
    setActual(placement);
    setShiftX(0);
  }, [open, placement]);

  useLayoutEffect(() => {
    if (!open || !presence.mounted) return;
    const el = ref.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const pad = 8;
    let next = actual;
    const side = actual.split("-")[0];
    if (side === "top" && r.top < pad) next = flipPlacement(next, "main");
    else if (side === "bottom" && r.bottom > vh - pad) next = flipPlacement(next, "main");
    else if (side === "right" && r.right > vw - pad) next = flipPlacement(next, "main");
    else if (side === "left" && r.left < pad) next = flipPlacement(next, "main");
    if (next !== actual) { setActual(next); return; }
    // 横向越界：沿交叉轴平移回视口（上下弹出的浮层）
    if (side === "top" || side === "bottom") {
      if (r.right > vw - pad) setShiftX(vw - pad - r.right);
      else if (r.left < pad) setShiftX(pad - r.left);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, presence.mounted, actual]);

  // 点外部 + Esc（Esc 只关栈顶）
  useEffect(() => {
    if (!open) return;
    const id = ++seq;
    openStack.push({ id, close: () => closeRef.current() });
    const onDown = (e: PointerEvent) => {
      if (!closeOnOutside) return;
      const t = e.target as Node | null;
      if (!t) return;
      if (ref.current?.contains(t)) return;
      if (ignoreRefs?.some((r) => r.current?.contains(t))) return;
      closeRef.current();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      const top = openStack[openStack.length - 1];
      if (top?.id !== id) return;
      e.stopPropagation();
      closeRef.current();
    };
    document.addEventListener("pointerdown", onDown, true);
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("pointerdown", onDown, true);
      document.removeEventListener("keydown", onKey, true);
      const i = openStack.findIndex((x) => x.id === id);
      if (i >= 0) openStack.splice(i, 1);
    };
  }, [open, closeOnOutside, ignoreRefs]);

  if (!presence.mounted) return null;
  return (
    <div
      ref={ref}
      className={`ui-pop mo-pop pl-${actual} ${className}`.trim()}
      data-state={presence.state}
      role={role}
      style={shiftX ? { ...style, ["--pop-shift-x" as string]: `${shiftX}px` } : style}
      onMouseEnter={onMouseEnter}
      onMouseLeave={onMouseLeave}
      {...rest}
    >
      {children}
    </div>
  );
}
