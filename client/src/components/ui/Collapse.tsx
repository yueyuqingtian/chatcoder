/** Collapse —— 统一折叠容器（plan-282-1416）
 *
 * 用 grid-template-rows 0fr↔1fr 做真实高度过渡（见方案 §3.6），
 * 取代此前的 max-height 估算与「展开/折叠直接换图标」硬切。
 * 内容始终挂载（保留内部状态），折叠态不播放内部动画。
 */
import type { ReactNode } from "react";

interface CollapseProps {
  open: boolean;
  children: ReactNode;
  className?: string;
}

export function Collapse({ open, children, className = "" }: CollapseProps) {
  return (
    <div className={`ui-collapse ${className}`.trim()} data-open={open}>
      <div className="ui-collapse-inner">{children}</div>
    </div>
  );
}
