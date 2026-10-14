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
  /** plan-20261014-fix4：执行中——两个按钮禁用、确认按钮换为 busyLabel，关闭（Esc / 点外部）不生效 */
  busy?: boolean;
  busyLabel?: string;
  onConfirm: () => void;
  onCancel: () => void;
}

export function ConfirmDialog({
  open, title, message, confirmLabel = "确认", cancelLabel = "取消", danger = false,
  busy = false, busyLabel, onConfirm, onCancel,
}: ConfirmDialogProps) {
  return (
    <Dialog
      open={open}
      onClose={busy ? () => {} : onCancel}
      width={400}
      title={title}
      footer={
        <>
          {/* plan-114-450：取消按钮用 secondary（有底色），与右侧主按钮同高同形，只在配色上区分主次 */}
          <button className="btn btn-secondary btn-sm" onClick={onCancel} disabled={busy}>{cancelLabel}</button>
          <button className={`btn btn-sm ${danger ? "btn-danger" : "btn-primary"}`} onClick={onConfirm} disabled={busy}>
            {busy ? (busyLabel ?? confirmLabel) : confirmLabel}
          </button>
        </>
      }
    >
      <div className="ui-dialog-message">{message}</div>
    </Dialog>
  );
}
