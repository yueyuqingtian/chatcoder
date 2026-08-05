import { useEffect } from "react";

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
            {showBack && <button className="modal-back" onClick={onClose} title="返回"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="15 18 9 12 15 6"/></svg></button>}
            <div className="modal-title">{title}</div>
            {subtitle && <div className="modal-subtitle">{subtitle}</div>}
          </div>
          <div className="modal-actions">
            {actions}
            {!showBack && <button className="modal-close" onClick={onClose} title="关闭 (Esc)"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg></button>}
          </div>
        </header>
        <div className="modal-body">{children}</div>
      </div>
      <style>{`
        .modal-overlay { position: fixed; inset: 0; z-index: 1000; background: var(--bg-overlay); display: flex; align-items: center; justify-content: center; animation: modal-fade var(--dur-normal) var(--ease-out); backdrop-filter: blur(6px); -webkit-backdrop-filter: blur(6px); }
        @keyframes modal-fade { from { opacity: 0; } to { opacity: 1; } }
        .modal-window { max-height: 86vh; display: flex; flex-direction: column; background: var(--bg-muted); border: 1px solid var(--border-light); border-radius: 16px; box-shadow: 0 18px 50px rgba(0,0,0,0.16), 0 2px 8px rgba(0,0,0,0.08); overflow: hidden; animation: modal-pop var(--dur-normal) var(--ease-out); }
        @keyframes modal-pop { from { transform: translateY(10px) scale(0.96); opacity: 0; } to { transform: translateY(0) scale(1); opacity: 1; } }
        .modal-header { display: flex; align-items: center; justify-content: space-between; padding: 16px 20px; border-bottom: 1px solid var(--border-light); background: var(--bg-muted); flex-shrink: 0; }
        .modal-title-wrap { display: flex; align-items: center; gap: 10px; min-width: 0; }
        .modal-back, .modal-close { display: flex; align-items: center; justify-content: center; width: 30px; height: 30px; padding: 0; border-radius: var(--radius-sm); border: none; background: transparent; color: var(--text-secondary); cursor: pointer; transition: background var(--dur-fast) var(--ease-out), color var(--dur-fast) var(--ease-out), transform var(--dur-normal) var(--ease-spring); }
        .modal-back:hover, .modal-close:hover { background: var(--bg-hover); color: var(--text); }
        .modal-close:hover { transform: rotate(90deg); }
        .modal-title { font-size: 15px; font-weight: 600; color: var(--text); letter-spacing: 0.2px; }
        .modal-subtitle { font-size: 12px; color: var(--text-muted); font-family: var(--font-mono); margin-left: 4px; }
        .modal-actions { display: flex; align-items: center; gap: 8px; flex-shrink: 0; }
        .modal-body { flex: 1; overflow: hidden; padding: 0; }
      `}</style>
    </div>
  );
}
