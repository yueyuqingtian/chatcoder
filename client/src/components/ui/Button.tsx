/** Button - 统一按钮组件 */
import type { ButtonHTMLAttributes, ReactNode } from "react";

type Variant = "primary" | "ghost" | "outline" | "danger" | "subtle";
type Size = "xs" | "sm" | "md";

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: Size;
  icon?: ReactNode;
}

const variants: Record<Variant, string> = {
  primary: "btn-primary",
  ghost: "btn-ghost",
  outline: "btn-outline",
  danger: "btn-danger",
  subtle: "btn-subtle",
};

const sizes: Record<Size, string> = {
  xs: "btn-xs",
  sm: "btn-sm",
  md: "btn-md",
};

export function Button({ variant = "ghost", size = "sm", icon, children, className = "", ...props }: ButtonProps) {
  const cls = ["btn", variants[variant], sizes[size], className].filter(Boolean).join(" ");
  return (
    <button className={cls} {...props}>
      {icon}
      {children}
    </button>
  );
}
