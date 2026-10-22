/** IconButton —— 统一图标按钮（plan-282-1416）
 *
 * 收敛此前三套并行实现：`.sb-icon-btn`（侧栏/导航页）、`.titlebar-btn`（标题栏）、
 * `.icon-btn`（右面板/列表）。默认 28px 方（--ctl-h-sm）与图标按钮行高对齐，
 * 尺寸档与控件高度三档一致，hover 走底色而非描边。
 *
 * S1（plan-41-197）：`title` 不再落到原生属性，改由 ui/Tooltip 渲染统一圆角卡片浮层
 * （原生 title 是浏览器方形浮块且聚焦延迟明显）；`aria-label` 缺省时以 title 兜底。
 *
 * plan-20261017-frontend-polish：补 forwardRef。被 Menu / Popover 的 asChild 触发器包裹时，
 * Radix 必须拿到原生 button 才能锚定与触发；此前缺失会导致「三个点」点不开。
 *
 * plan-522-2209：新增 loading——进行中把图标换成同尺寸 spinner，并给图标一个固定尺寸槽，
 * 否则「箭头 → 旋转环」的替换会让按钮内容横向跳一下。
 */
import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from "react";
import { IconSpinner } from "../icons";
import { Tooltip } from "./Tooltip";

type Size = "xs" | "sm" | "lg";

interface IconButtonProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, "children"> {
  icon: ReactNode;
  size?: Size;
  /** 激活态（选中/当前 tab） */
  active?: boolean;
  /** 悬停色调：默认中性，danger 走危险色 */
  tone?: "default" | "danger";
  /** 进行中：图标槽换成 spinner，按钮尺寸不变（plan-522-2209） */
  loading?: boolean;
}

export const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(function IconButton(
  { icon, size = "sm", active = false, tone = "default", loading = false, className = "", title, ...props },
  ref,
) {
  const cls = [
    "ui-icon-btn",
    size !== "sm" ? `size-${size}` : "",
    active ? "is-active" : "",
    tone === "danger" ? "tone-danger" : "",
    loading ? "is-loading" : "",
    className,
  ].filter(Boolean).join(" ");
  const label = props["aria-label"] ?? (typeof title === "string" ? title : undefined);
  const btn = (
    <button ref={ref} type="button" className={cls} aria-label={label}
      aria-busy={loading || undefined} {...props}>
      <span className="ui-icon-btn-slot">{loading ? <IconSpinner size={14} /> : icon}</span>
    </button>
  );
  return typeof title === "string" && title ? <Tooltip title={title} side="top">{btn}</Tooltip> : btn;
});
