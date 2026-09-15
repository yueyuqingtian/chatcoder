/** ChatCollapse（plan-248-1258 M1）：消息流内联展开块（工具输出 / 思考全文 / 工具簇明细）
 *  的统一动画容器。
 *
 * 设计动机：此前这些块一律 `{expanded && <X/>}` 条件渲染，挂载即呈现，
 * 展开/折叠是瞬跳（生硬）。本组件把「是否渲染」与「是否展开」解耦：
 *
 * - 展开：先挂载内容（mounted=true），下一帧由 data-open 触发 grid-template-rows
 *   0fr→1fr 的真实高度过渡（复用 motion.css 的 collapse 机制，杜绝 max-height 猜值）；
 * - 折叠：data-open 先置 false 播完过渡，transitionend 后再卸载内容——
 *   既保留懒加载收益（如 InlineDiff 折叠时不发请求），又拿到平滑收起动画；
 * - 减少动态效果档（--motion-scale=0）下时长为 0，transitionend 立即触发，
 *   等价瞬时切换，无残留。
 */
import { useEffect, useState } from "react";

interface InertProps {
  /** React 18 的 DOM 类型未内置 inert，用字符串透传（关闭态禁止内部元素被聚焦/点击） */
  inert?: string;
}

export function ChatCollapse({
  open,
  className,
  children,
}: {
  open: boolean;
  className?: string;
  children: React.ReactNode;
}) {
  const [mounted, setMounted] = useState(open);

  // 展开时立即挂载，让内容参与随后的高度过渡
  useEffect(() => {
    if (open) setMounted(true);
  }, [open]);

  return (
    <div
      className={"tc-collapse" + (className ? " " + className : "")}
      data-open={open ? "true" : "false"}
      aria-hidden={!open}
      onTransitionEnd={(e) => {
        // 仅响应高度行的过渡结束（opacity/transform 会先结束，避免提前卸载闪一下）
        if (e.propertyName === "grid-template-rows" && !open) setMounted(false);
      }}
    >
      <div
        className="tc-collapse-inner"
        style={{ pointerEvents: open ? undefined : "none" }}
        {...(!open ? ({ inert: "" } as InertProps) : {})}
      >
        {mounted ? children : null}
      </div>
    </div>
  );
}
