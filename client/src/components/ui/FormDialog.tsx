/** FormDialog - 基于 Modal 的表单弹窗，用于新建/编辑场景 */
import type { ReactNode } from "react";
import { Dialog } from "./Dialog";
import { Button } from "./Button";

interface FormDialogProps {
  open: boolean;
  onClose: () => void;
  title: string;
  subtitle?: string;
  onSubmit?: () => void;
  submitLabel?: string;
  submitDisabled?: boolean;
  width?: number;
  children: ReactNode;
}

export function FormDialog({ open, onClose, title, subtitle, onSubmit, submitLabel = "创建", submitDisabled, width = 520, children }: FormDialogProps) {
  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={title}
      subtitle={subtitle}
      width={width}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>取消</Button>
          {onSubmit && <Button variant="primary" onClick={onSubmit} disabled={submitDisabled}>{submitLabel}</Button>}
        </>
      }
    >
      <div className="form-dialog-body">{children}</div>
    </Dialog>
  );
}