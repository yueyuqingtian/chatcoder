/** 任务数量徽标（plan-73-323 阶段2 · 方案 3.3）。
 *
 * 运行任务 ≥2 时显示在宠物脚边，底色随聚合状态变化（失败=红 / 等待=琥珀 / 运行=主题色）；
 * 只有 1 个任务时不显示，避免噪音。徽标占固定槽位（不随动画位移）。
 * 用 forwardRef 暴露根节点：父组件需要它的矩形做命中检测（徽标应与宠物同属可交互区）。
 */
import { forwardRef } from "react";
import type { PetAggregate } from "./useTaskCards";

interface Props {
  count: number;
  status: PetAggregate;
}

export const TaskBadge = forwardRef<HTMLSpanElement, Props>(function TaskBadge({ count, status }, ref) {
  if (count < 2) return null;
  return (
    <span ref={ref} className={`pet-badge pet-badge-${status}`} aria-hidden="true">
      {count > 99 ? "99+" : count}
    </span>
  );
});
