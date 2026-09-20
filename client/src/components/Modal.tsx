/** Modal（plan-282-1416：内部迁移到 ui/Dialog 基座，对外 API 完全不变）
 *
 * 变更点：
 *  - 退出动画由 Radix Presence 驱动（旧实现 `if (!open) return null` 直接卸载，
 *    关闭时没有任何过渡）；
 *  - 焦点陷阱 / 关闭后焦点还原 / Esc 由 Radix 承担，移除手写 keydown 监听；
 *  - 尺寸与头部排版统一走 .ui-dialog-* 样式（圆角 14，与全站弹窗一致）。
 */
import { IconChevronLeft } from "./icons";
import { Dialog } from "./ui/Dialog";

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

export function Modal({ open, onClose, title, subtitle, actions, children, width = 720, showBack = false }: ModalProps) {
  return (
    <Dialog
      open={open}
      onClose={onClose}
      width={width}
      title={title}
      subtitle={subtitle}
      closable={!showBack}
      actions={
        <>
          {showBack && (
            <button type="button" className="ui-icon-btn" title="返回" aria-label="返回" onClick={onClose}>
              <IconChevronLeft size={16} />
            </button>
          )}
          {actions}
        </>
      }
    >
      {children}
    </Dialog>
  );
}
