/** ContextMenu - 统一上下文菜单（三个点触发） */
import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";

export interface MenuItem {
  label: string;
  onClick: () => void;
  danger?: boolean;
  icon?: ReactNode;
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