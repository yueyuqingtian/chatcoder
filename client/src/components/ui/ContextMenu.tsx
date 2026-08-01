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

  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
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