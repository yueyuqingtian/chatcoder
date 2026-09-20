/** IconButton —— 统一图标按钮（plan-282-1416）
 *
 * 收敛此前三套并行实现：`.sb-icon-btn`（侧栏/导航页）、`.titlebar-btn`（标题栏）、
 * `.icon-btn`（右面板/列表）。默认 28px 方（--ctl-h-sm）与图标按钮行高对齐，
 * 尺寸档与控件高度三档一致，hover 走底色而非描边。
 */
import type { ButtonHTMLAttributes, ReactNode } from "react";

type Size = "xs" | "sm" | "lg";

interface IconButtonProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, "children"> {
  icon: ReactNode;
  size?: Size;
  /** 激活态（选中/当前 tab） */
  active?: boolean;
  /** 悬停色调：默认中性，danger 走危险色 */
  tone?: "default" | "danger";
}

export function IconButton({ icon, size = "sm", active = false, tone = "default", className = "", ...props }: IconButtonProps) {
  const cls = [
    "ui-icon-btn",
    size !== "sm" ? `size-${size}` : "",
    active ? "is-active" : "",
    tone === "danger" ? "tone-danger" : "",
    className,
  ].filter(Boolean).join(" ");
  return (
    <button type="button" className={cls} {...props}>
      {icon}
    </button>
  );
}
