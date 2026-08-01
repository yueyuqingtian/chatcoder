interface ConfirmDialogProps {
  open: boolean;
  title: string;
  message: string;
  confirmLabel?: string;
  cancelLabel?: string;
  danger?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

export function ConfirmDialog({
  open, title, message, confirmLabel = "确认", cancelLabel = "取消", danger = false,
  onConfirm, onCancel,
}: ConfirmDialogProps) {
  if (!open) return null;

  return (
    <div className="confirm-overlay" onClick={onCancel}>
      <div className="confirm-dialog" onClick={(e) => e.stopPropagation()}>
        <div className="confirm-title">{title}</div>
        <div className="confirm-msg">{message}</div>
        <div className="confirm-actions">
          <button className="btn btn-ghost btn-sm" onClick={onCancel}>{cancelLabel}</button>
          <button className={`btn btn-sm ${danger ? "btn-danger" : "btn-primary"}`} onClick={onConfirm}>{confirmLabel}</button>
        </div>
      </div>
      <style>{`
        .confirm-overlay {
          position: fixed; inset: 0; z-index: 2000;
          background: var(--bg-overlay);
          display: flex; align-items: center; justify-content: center;
          animation: modal-fade var(--dur-normal) var(--ease-out);
          backdrop-filter: blur(6px);
          -webkit-backdrop-filter: blur(6px);
        }
        .confirm-dialog {
          background: var(--bg-muted);
          border: 1px solid var(--border-light);
          border-radius: var(--radius-xl);
          box-shadow: var(--shadow-xl);
          padding: 24px; max-width: 400px; width: 92%;
          animation: modal-pop var(--dur-normal) var(--ease-out);
        }
        .confirm-title { font-size: var(--fs-lg); font-weight: 600; margin-bottom: 8px; color: var(--text); letter-spacing: 0.2px; }
        .confirm-msg { font-size: var(--fs-sm); color: var(--text-secondary); line-height: 1.65; margin-bottom: 20px; }
        .confirm-actions { display: flex; gap: 8px; justify-content: flex-end; }
      `}</style>
    </div>
  );
}
