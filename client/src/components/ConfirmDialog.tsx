/** ConfirmDialog（plan-282-1416：内部迁移到 ui/Dialog，对外 API 不变）
 *  统一确认框的排版与退场动画；危险操作走 btn-danger。 */
import { Dialog } from "./ui/Dialog";

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
  return (
    <Dialog
      open={open}
      onClose={onCancel}
      width={400}
      title={title}
      footer={
        <>
          <button className="btn btn-ghost btn-sm" onClick={onCancel}>{cancelLabel}</button>
          <button className={`btn btn-sm ${danger ? "btn-danger" : "btn-primary"}`} onClick={onConfirm}>{confirmLabel}</button>
        </>
      }
    >
      <div className="ui-dialog-message">{message}</div>
    </Dialog>
  );
}
