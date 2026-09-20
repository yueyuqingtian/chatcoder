/** Dialog —— 统一弹窗基座（Radix Dialog，plan-282-1416）
 *
 * 集中承载此前 5 套并行弹窗实现（Modal / ConfirmDialog / FormDialog /
 * RollbackConfirmModal / WhatsNewModal）的共同职责：
 *  - Esc 关闭、点击遮罩关闭、焦点陷阱与关闭后焦点还原（Radix 原生）；
 *  - **真实退场动画**（由 Presence 驱动，data-state=closed 时播放后卸载）——
 *    旧实现是 `if (!open) return null` 直接卸载，没有任何关闭过渡；
 *  - 宽度、标题/副标题、头部操作区、底部 footer 统一排版。
 *
 * 页签顺序：Content 内 head → body → footer，body 独立滚动（scrollbar-gutter: stable）。
 */
import * as RadixDialog from "@radix-ui/react-dialog";
import type { ReactNode } from "react";
import { IconX } from "../icons";

interface DialogProps {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  subtitle?: ReactNode;
  /** 头部右侧操作区（关闭按钮之前） */
  actions?: ReactNode;
  /** 底部操作区（表单提交/确认按钮） */
  footer?: ReactNode;
  /** 最大宽度（px），默认 720 */
  width?: number;
  /** 是否显示右上角关闭按钮，默认 true */
  closable?: boolean;
  /** 点击遮罩关闭，默认 true */
  dismissOnOverlay?: boolean;
  bodyClassName?: string;
  className?: string;
  children: ReactNode;
}

export function Dialog({
  open,
  onClose,
  title,
  subtitle,
  actions,
  footer,
  width = 720,
  closable = true,
  dismissOnOverlay = true,
  bodyClassName = "",
  className = "",
  children,
}: DialogProps) {
  return (
    <RadixDialog.Root open={open} onOpenChange={(v) => { if (!v) onClose(); }}>
      <RadixDialog.Portal>
        <RadixDialog.Overlay className="ui-dialog-overlay" />
        <RadixDialog.Content
          className={`ui-dialog-content ${className}`.trim()}
          style={{ ["--ui-dialog-w" as string]: `${width}px` }}
          onPointerDownOutside={(e) => { if (!dismissOnOverlay) e.preventDefault(); }}
          onInteractOutside={(e) => { if (!dismissOnOverlay) e.preventDefault(); }}
        >
          <div className="ui-dialog-head">
            <div className="ui-dialog-title-wrap">
              <RadixDialog.Title className="ui-dialog-title">{title}</RadixDialog.Title>
              {subtitle ? <RadixDialog.Description className="ui-dialog-subtitle">{subtitle}</RadixDialog.Description> : null}
            </div>
            <div className="ui-dialog-actions">
              {actions}
              {closable && (
                <button type="button" className="ui-icon-btn" title="关闭 (Esc)" aria-label="关闭" onClick={onClose}>
                  <IconX size={15} />
                </button>
              )}
            </div>
          </div>
          <div className={`ui-dialog-body ${bodyClassName}`.trim()}>{children}</div>
          {footer ? <div className="ui-dialog-footer">{footer}</div> : null}
        </RadixDialog.Content>
      </RadixDialog.Portal>
    </RadixDialog.Root>
  );
}
