/** IconButton —— 统一图标按钮（plan-282-1416）
 *
 * 收敛此前三套并行实现：`.sb-icon-btn`（侧栏/导航页）、`.titlebar-btn`（标题栏）、
 * `.icon-btn`（右面板/列表）。默认 28px 方（--ctl-h-sm）与图标按钮行高对齐，
 * 尺寸档与控件高度三档一致，hover 走底色而非描边。
 *
 * S1（plan-41-197）：`title` 不再落到原生属性，改由 ui/Tooltip 渲染统一圆角卡片浮层
 * （原生 title 是浏览器方形浮块且聚焦延迟明显）；`aria-label` 缺省时以 title 兜底。
 */
import type { ButtonHTMLAttributes, ReactNode } from "react";
import { Tooltip } from "./Tooltip";

type Size = "xs" | "sm" | "lg";

interface IconButtonProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, "children"> {
  icon: ReactNode;
  size?: Size;
  /** 激活态（选中/当前 tab） */
  active?: boolean;
  /** 悬停色调：默认中性，danger 走危险色 */
  tone?: "default" | "danger";
}

export function IconButton({ icon, size = "sm", active = false, tone = "default", className = "", title, ...props }: IconButtonProps) {
  const cls = [
    "ui-icon-btn",
    size !== "sm" ? `size-${size}` : "",
    active ? "is-active" : "",
    tone === "danger" ? "tone-danger" : "",
    className,
  ].filter(Boolean).join(" ");
  const label = props["aria-label"] ?? (typeof title === "string" ? title : undefined);
  const btn = (
    <button type="button" className={cls} aria-label={label} {...props}>
      {icon}
    </button>
  );
  return typeof title === "string" && title ? <Tooltip title={title} side="top">{btn}</Tooltip> : btn;
}
