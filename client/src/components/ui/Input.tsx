/** Input / Textarea —— 统一输入控件（plan-282-1416 设计语言 v7）
 *
 * 设计契约：
 *  - 尺寸三档：sm(28) / md(32，默认) / lg(36)，与 Slider/Select/Button 同档同高；
 *  - 五态统一：默认 / 悬停（只变边框色）/ 聚焦（中性色，focus.css 输出）/ 禁用 / 错误；
 *  - 圆角 --r-md(8px)，与按钮一致（同层级必须同值，见方案 §3.1）；
 *  - 有 prefix/suffix/clearable 时输出 wrap 容器（视觉边界由 wrap 承载，内层 input 无边框）；
 *  - 纯 className 追加仍可用作页面级微调（不破坏既有调用习惯）。
 *
 * 兼容：未迁移页面里的 `className="ui-input"` 仍由 components.css 的裸样式兜底。
 *
 * plan-41-199：补 forwardRef —— 侧栏重命名依赖 DOM 引用做「被动失焦保活 / 恢复焦点全选」，
 * React 18 下普通函数组件接不了 ref；ref 透传到内层原生 input，既有调用行为不变。
 */
import { forwardRef, useState, type InputHTMLAttributes, type ReactNode, type TextareaHTMLAttributes } from "react";
import { IconX } from "../icons";

export type ControlSize = "sm" | "md" | "lg";

interface InputProps extends Omit<InputHTMLAttributes<HTMLInputElement>, "size" | "prefix"> {
  /** 尺寸档位（与 Select/Slider/Button 对齐） */
  size?: ControlSize;
  /** 校验失败态：加红边框 + aria-invalid */
  invalid?: boolean;
  /** 前置内容（图标或短文本） */
  prefix?: ReactNode;
  /** 后置内容（单位或图标） */
  suffix?: ReactNode;
  /** 有值时显示清除按钮（需配合受控 value + onChange） */
  clearable?: boolean;
  /** 外层容器类名（仅在有 prefix/suffix/clearable 时存在） */
  wrapClassName?: string;
}

export const Input = forwardRef<HTMLInputElement, InputProps>(function Input({
  size = "md",
  invalid = false,
  prefix,
  suffix,
  clearable = false,
  wrapClassName = "",
  className = "",
  value,
  disabled,
  ...props
}, ref) {
  const [hoverClear, setHoverClear] = useState(false);
  const hasValue = value !== undefined && value !== null && String(value).length > 0;
  const showClear = clearable && hasValue && !disabled && typeof props.onChange === "function";

  const input = (
    <input
      ref={ref}
      className={`ui-input ${className}`.trim()}
      value={value}
      disabled={disabled}
      aria-invalid={invalid || undefined}
      {...props}
    />
  );

  // 无装饰时直接渲染裸 input —— 调用方通常已有自己的 flex 布局
  if (!prefix && !suffix && !clearable) return input;

  const wrapCls = [
    "ui-input-wrap",
    size !== "md" ? `size-${size}` : "",
    invalid ? "is-invalid" : "",
    disabled ? "is-disabled" : "",
    wrapClassName,
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <div className={wrapCls} onMouseEnter={() => setHoverClear(true)} onMouseLeave={() => setHoverClear(false)}>
      {prefix && <span className="ui-input-affix">{prefix}</span>}
      {input}
      {showClear && hoverClear && (
        <button
          type="button"
          className="ui-input-affix"
          title="清除"
          aria-label="清除"
          onClick={() => {
            // 复用调用方的 onChange，保证受控语义一致
            const ev = { target: { value: "" } } as unknown as React.ChangeEvent<HTMLInputElement>;
            props.onChange?.(ev);
          }}
        >
          <IconX size={12} />
        </button>
      )}
      {suffix && <span className="ui-input-affix">{suffix}</span>}
    </div>
  );
});

interface TextareaProps extends TextareaHTMLAttributes<HTMLTextAreaElement> {
  invalid?: boolean;
  /** 随内容自动增高（到 maxRows 后转为内部滚动） */
  autoGrow?: boolean;
  /** autoGrow 的最大行数，默认 10 */
  maxRows?: number;
}

export function Textarea({
  invalid = false,
  autoGrow = false,
  maxRows = 10,
  className = "",
  rows = 3,
  value,
  onChange,
  ...props
}: TextareaProps) {
  /** autoGrow 用 scrollHeight 测量：先把高度复位再取，避免只增不减 */
  const handleInput = (e: React.FormEvent<HTMLTextAreaElement>) => {
    const el = e.currentTarget;
    el.style.height = "auto";
    const lineHeight = parseFloat(getComputedStyle(el).lineHeight) || 20;
    const max = lineHeight * maxRows + 16;
    el.style.height = `${Math.min(el.scrollHeight, max)}px`;
    el.style.overflowY = el.scrollHeight > max ? "auto" : "hidden";
  };

  return (
    <textarea
      className={`ui-textarea${autoGrow ? " auto-grow" : ""} ${className}`.trim()}
      rows={autoGrow ? 1 : rows}
      value={value}
      aria-invalid={invalid || undefined}
      onChange={onChange}
      onInput={autoGrow ? handleInput : undefined}
      {...props}
    />
  );
}
