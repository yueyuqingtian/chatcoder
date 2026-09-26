/** Modal（plan-282-1416：内部迁移到 ui/Dialog 基座，对外 API 完全不变）
 *
 * 变更点：
 *  - 退出动画由 Radix Presence 驱动（旧实现 `if (!open) return null` 直接卸载，
 *    关闭时没有任何过渡）；
 *  - 焦点陷阱 / 关闭后焦点还原 / Esc 由 Radix 承担，移除手写 keydown 监听；
 *  - 尺寸与头部排版统一走 .ui-dialog-* 样式（圆角 14，与全站弹窗一致）。
 *
 * plan-41-231：新增 footer 透传——弹窗操作按钮统一走 footer 固定在右下角；
 * 头部 actions 只保留头部动作（如返回）。
 */
import { IconChevronLeft } from "./icons";
import { Dialog } from "./ui/Dialog";

interface ModalProps {
  open: boolean;
  onClose: () => void;
  title: React.ReactNode;
  subtitle?: React.ReactNode;
  /** 头部右侧操作区（关闭按钮之前）。plan-41-231：只放头部动作（如返回），
   *  取消 / 保存 这类提交类操作一律走 footer，保证全站弹窗按钮都在右下角。 */
  actions?: React.ReactNode;
  /** 底部操作区：固定弹窗右下角，不随正文滚动 */
  footer?: React.ReactNode;
  children: React.ReactNode;
  width?: number;
  height?: number | string;
  showBack?: boolean;
}

export function Modal({ open, onClose, title, subtitle, actions, footer, children, width = 720, showBack = false }: ModalProps) {
  return (
    <Dialog
      open={open}
      onClose={onClose}
      width={width}
      title={title}
      subtitle={subtitle}
      closable={!showBack}
      footer={footer}
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
