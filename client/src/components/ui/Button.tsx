/** Button —— 全局统一按钮（plan-41-197 S0 / 设计语言 v8）
 *
 * 契约：
 *  - variant 只负责配色，size 只负责尺寸（高度/横向内边距/字号全部由 --btn-* 令牌驱动）；
 *  - 圆角统一 8px（--r-md），与输入框/下拉同档；
 *  - 支持 icon / iconOnly / loading / block；页面不再自造按钮类
 *    （旧的 .perm-tab / .ts-mini-btn / .usage-seg-btn 等逐步迁移到这里）。
 */
import type { ButtonHTMLAttributes, ReactNode } from "react";

type Variant = "primary" | "secondary" | "outline" | "ghost" | "subtle" | "danger" | "danger-ghost";
type Size = "xs" | "sm" | "md" | "lg";

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: Size;
  /** 前置图标（loading 时被转圈替代） */
  icon?: ReactNode;
  /** 仅图标：正方形按钮，宽度跟随尺寸档（children 不渲染） */
  iconOnly?: boolean;
  /** 加载态：显示转圈并阻止交互 */
  loading?: boolean;
  /** 占满父级宽度 */
  block?: boolean;
}

const variants: Record<Variant, string> = {
  primary: "btn-primary",
  secondary: "btn-secondary",
  outline: "btn-outline",
  ghost: "btn-ghost",
  subtle: "btn-subtle",
  danger: "btn-danger",
  "danger-ghost": "btn-danger-ghost",
};

const sizes: Record<Size, string> = {
  xs: "btn-xs",
  sm: "btn-sm",
  md: "btn-md",
  lg: "btn-lg",
};

export function Button({
  variant = "ghost",
  size = "sm",
  icon,
  iconOnly = false,
  loading = false,
  block = false,
  children,
  className = "",
  disabled,
  ...props
}: ButtonProps) {
  const cls = [
    "btn",
    variants[variant],
    sizes[size],
    iconOnly ? "btn-icon-only" : "",
    block ? "btn-block" : "",
    loading ? "is-loading" : "",
    className,
  ]
    .filter(Boolean)
    .join(" ");
  return (
    <button className={cls} disabled={disabled || loading} {...props}>
      {loading ? <span className="btn-spinner" aria-hidden /> : icon}
      {!iconOnly && children}
    </button>
  );
}
