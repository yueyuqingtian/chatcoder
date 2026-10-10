/** Badge —— 统一徽标（plan-20261009-ui U4/U7）。
 *  胶囊圆角；高度三档 16 / 20 / 24（--badge-h-*）；tone 管语义色，variant 管填充方式。
 *  取代散落的 .cq-pill-tag / .sb-worktree-tag / .tc-group-count / 各页自造计数块。 */
import type { HTMLAttributes, ReactNode } from "react";

export type BadgeTone = "neutral" | "accent" | "success" | "warning" | "danger" | "info";

interface BadgeProps extends HTMLAttributes<HTMLSpanElement> {
  size?: "xs" | "sm" | "md";
  tone?: BadgeTone;
  variant?: "soft" | "outline" | "solid";
  icon?: ReactNode;
  /** 数字计数（等宽数字，xs 下自动收为圆形） */
  count?: boolean;
}

export function Badge({ size = "sm", tone = "neutral", variant = "soft", icon, count = false, className = "", children, ...rest }: BadgeProps) {
  const cls = ["ui-badge", `bd-${size}`, `bd-${tone}`, `bd-${variant}`, count ? "bd-count" : "", className].filter(Boolean).join(" ");
  return (
    <span className={cls} {...rest}>
      {icon}
      {children}
    </span>
  );
}
