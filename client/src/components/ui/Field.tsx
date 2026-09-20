/** Field —— 表单字段容器（label + 控件 + 提示/错误）
 *
 * 统一 17 处手写 label 的排版（label 12/500、hint 12/--text-3、error 12/destructive），
 * 并保证「有/无 hint」「有/无 error」时控件位置不跳动（提示行只在存在时占位）。
 */
import { useId, type ReactNode } from "react";

interface FieldProps {
  label?: ReactNode;
  /** 辅助说明（与 error 互斥显示，error 优先） */
  hint?: ReactNode;
  error?: ReactNode;
  required?: boolean;
  /** 自定义控件 id；缺省时自动生成并注入到唯一的子元素上 */
  htmlFor?: string;
  className?: string;
  children: ReactNode;
}

export function Field({ label, hint, error, required, htmlFor, className = "", children }: FieldProps) {
  const autoId = useId();
  const controlId = htmlFor ?? autoId;
  return (
    <div className={`ui-field ${className}`.trim()}>
      {label && (
        <label className="ui-field-label" htmlFor={controlId}>
          {label}
          {required && <span className="ui-field-req">*</span>}
        </label>
      )}
      <div className="ui-field-control">{children}</div>
      {error ? <div className="ui-field-error">{error}</div> : hint ? <div className="ui-field-hint">{hint}</div> : null}
    </div>
  );
}
