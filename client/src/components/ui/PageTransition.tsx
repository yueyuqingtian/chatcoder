/** PageTransition —— 统一页面/标签切换过渡（plan-282-1421 · 第3项）
 *
 * 背景：此前页面切换是「靠 key 重挂载 + .view-enter 单次淡入」，settings ↔ workspace
 * 的整块切换与设置页内部 tab 切换完全没有过渡，观感是"瞬移"。
 *
 * 设计约束（必须遵守，否则会重新引入上一轮已根治的布局跳变）：
 *  - 过渡**只允许** transform / opacity —— 绝不碰 width/height/margin/padding；
 *  - 时长与曲线走既有令牌（--dur-3 / --ease-emphasized-decel），随 `data-motion`
 *    三档（full/reduced/off）与 `prefers-reduced-motion` 自动降级；
 *  - 用 `key` 触发"重新挂载即播放一次"，不做复杂状态机；
 *  - 默认不设固定高度，由调用方按需通过 className/style 声明（避免与滚动容器打架）。
 *
 * 用法：
 *   <PageTransition id={nav}>…</PageTransition>          // id 变化即播放一次入场
 *   <PageTransition id={tab} direction="left" fill>…</PageTransition>
 */
import type { CSSProperties, ReactNode } from "react";

interface PageTransitionProps {
  /** 变化即重放入场动画（通常是 nav key / tab id / sessionId） */
  id: string | number;
  /** 位移方向：up=自下而上（默认）；left=自左而右；none=仅淡入 */
  direction?: "up" | "left" | "none";
  /** 是否占满父级高度（面板内部切换需要；页面级切换保持默认 true 即可） */
  fill?: boolean;
  className?: string;
  style?: CSSProperties;
  children: ReactNode;
}

export function PageTransition({ id, direction = "up", fill = true, className = "", style, children }: PageTransitionProps) {
  const dirCls = direction === "left" ? "dir-left" : direction === "none" ? "dir-none" : "";
  return (
    <div
      key={id}
      className={`ui-page-transition ${dirCls}${fill ? "" : " no-fill"} ${className}`.trim()}
      style={{
        // 位移量随 --motion-dist 缩放（动画"关闭"档为 0，等价瞬时但仍达终态）
        ["--pt-dist" as string]: "calc(6px * var(--motion-dist, 1))",
        ...style,
      }}
    >
      {children}
    </div>
  );
}
