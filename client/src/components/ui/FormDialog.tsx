/** FormDialog - 基于 Modal 的表单弹窗，用于新建/编辑场景 */
import type { ReactNode } from "react";
import { Modal } from "../Modal";
import { Button } from "./Button";

interface FormDialogProps {
  open: boolean;
  onClose: () => void;
  title: string;
  subtitle?: string;
  onSubmit?: () => void;
  submitLabel?: string;
  children: ReactNode;
}

export function FormDialog({ open, onClose, title, subtitle, onSubmit, submitLabel = "创建", children }: FormDialogProps) {
  return (
    <Modal open={open} onClose={onClose} title={title} subtitle={subtitle} width={520}>
      <div className="form-dialog-body">{children}</div>
      <div className="form-dialog-actions">
        <Button variant="ghost" onClick={onClose}>取消</Button>
        {onSubmit && <Button variant="primary" onClick={onSubmit}>{submitLabel}</Button>}
      </div>
    </Modal>
  );
}