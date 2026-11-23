/** ContextMenu - 统一上下文菜单（三个点触发） */
import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { createPortal } from "react-dom";
import { Button } from "./Button";
import { Popover } from "./Popover";

export interface MenuItem {
  label: string;
  onClick: () => void;
  danger?: boolean;
  icon?: ReactNode;
}

/** plan-771-3177：虚拟列表行可能在滚动时卸载，菜单锚定窗口坐标。 */
export function PointContextMenu({ point, items, onClose }: {
  point: { x: number; y: number } | null;
  items: MenuItem[];
  onClose: () => void;
}) {
  const position = useRef({ x: 0, y: 0 });
  const menuItems = useRef<MenuItem[]>([]);
  const root = useRef<HTMLDivElement>(null);
  if (point) { position.current = point; menuItems.current = items; }
  const open = point != null;
  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const frame = requestAnimationFrame(() => root.current?.querySelector<HTMLElement>('[role="menuitem"]')?.focus());
    return () => {
      cancelAnimationFrame(frame);
      // 已经切到弹窗或行内输入时，交由新控件决定焦点。
      if (root.current?.contains(document.activeElement) && previous?.isConnected) previous.focus();
    };
  }, [open]);
  return createPortal(
    <div ref={root} className="point-menu-anchor" style={{ left: position.current.x, top: position.current.y }}>
      <Popover open={open} onClose={onClose} placement="bottom-start" className="point-menu" role="menu"
        onKeyDown={(event) => {
          if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
          const buttons = Array.from(event.currentTarget.querySelectorAll<HTMLElement>('[role="menuitem"]'));
          if (!buttons.length) return;
          event.preventDefault();
          const current = buttons.indexOf(document.activeElement as HTMLElement);
          buttons[(current + (event.key === "ArrowDown" ? 1 : -1) + buttons.length) % buttons.length].focus();
        }}>
        {menuItems.current.map((item) => (
          <Button key={item.label} role="menuitem" size="sm" variant={item.danger ? "danger" : "ghost"}
            icon={item.icon} onClick={() => { onClose(); item.onClick(); }}>
            {item.label}
          </Button>
        ))}
      </Popover>
    </div>,
    document.body,
  );
}

export function ContextMenu({ items, children, align = "right" }: {
  items: MenuItem[];
  children: ReactNode;
  align?: "left" | "right";
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  // 浮层规范（CLAUDE.md §2）：点击外部与 Esc 均关闭
  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", handler);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", handler);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <div className="ctx-menu-trigger" ref={ref}>
      <span onClick={(e) => { e.stopPropagation(); setOpen(!open); }}>{children}</span>
      {open && (
        <div className={"ctx-menu" + (align === "left" ? " align-left" : "")} onClick={() => setOpen(false)}>
          {items.map((item, i) => (
            <div
              key={i}
              className={"ctx-menu-item" + (item.danger ? " danger" : "")}
              onClick={(e) => { e.stopPropagation(); item.onClick(); setOpen(false); }}
            >
              {item.icon}
              <span>{item.label}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
