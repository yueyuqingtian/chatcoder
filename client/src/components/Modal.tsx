import { useEffect } from "react";
import { IconChevronLeft, IconX } from "./icons";

interface ModalProps {
  open: boolean;
  onClose: () => void;
  title: React.ReactNode;
  subtitle?: React.ReactNode;
  actions?: React.ReactNode;
  children: React.ReactNode;
  width?: number;
  height?: number | string;
  showBack?: boolean;
}

export function Modal({ open, onClose, title, subtitle, actions, children, width = 720, height, showBack = false }: ModalProps) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);
  if (!open) return null;
  const winStyle: React.CSSProperties = { width: `min(${width}px, 92vw)` };
  if (height !== undefined) winStyle.height = typeof height === "number" ? `min(${height}px, 86vh)` : height;
  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-window" style={winStyle} onClick={(e) => e.stopPropagation()}>
        <header className="modal-header">
          <div className="modal-title-wrap">
            {showBack && <button className="modal-back" onClick={onClose} title="返回"><IconChevronLeft size={16} /></button>}
            <div className="modal-title">{title}</div>
            {subtitle && <div className="modal-subtitle">{subtitle}</div>}
          </div>
          <div className="modal-actions">
            {actions}
            {!showBack && <button className="modal-close" onClick={onClose} title="关闭 (Esc)"><IconX size={16} /></button>}
          </div>
        </header>
        <div className="modal-body">{children}</div>
      </div>
    </div>
  );
}
