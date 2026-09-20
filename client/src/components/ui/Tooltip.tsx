/** Tooltip —— 统一轻量提示（Radix Tooltip，plan-282-1416）
 *
 * 用于标题栏/侧栏图标按钮等「仅图标」控件：以自有浮层替代原生 title，
 * 延迟 400ms 出现，键盘聚焦同样可见（Radix 原生支持）。
 */
import * as RadixTooltip from "@radix-ui/react-tooltip";
import type { ReactNode } from "react";

interface TooltipProps {
  content: ReactNode;
  children: ReactNode;
  side?: "top" | "right" | "bottom" | "left";
  delay?: number;
  disabled?: boolean;
}

/** 顶层需要一次 Provider（放在 App 内），此处提供独立 Provider 以便局部使用不报错 */
export function TooltipProvider({ children }: { children: ReactNode }) {
  return <RadixTooltip.Provider delayDuration={400} skipDelayDuration={200}>{children}</RadixTooltip.Provider>;
}

export function Tooltip({ content, children, side = "bottom", delay = 400, disabled = false }: TooltipProps) {
  if (disabled || !content) return <>{children}</>;
  return (
    <RadixTooltip.Root delayDuration={delay}>
      <RadixTooltip.Trigger asChild>{children}</RadixTooltip.Trigger>
      <RadixTooltip.Portal>
        <RadixTooltip.Content className="ui-tooltip" side={side} sideOffset={6}>
          {content}
        </RadixTooltip.Content>
      </RadixTooltip.Portal>
    </RadixTooltip.Root>
  );
}
