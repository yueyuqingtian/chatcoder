/** 角色色磁贴（plan-20261009-ui U3）：右侧面板启动卡 / 拓展页品类等处的「创意图标」。
 *
 *  与本软件其它图标同一语言——线性图标 + 角色色淡底，不引入拟物 / 渐变插画。
 *  闲置灰度，宿主（按钮 / 卡片）聚焦时恢复角色色并轻轻抬起，图标自带的部件微动效（望远镜倾斜、
 *  星芒弹出……）由宿主 hover 同步触发。tone 取自项目色板（--pc-*，明暗主题自适应）。 */
import type { ReactNode } from "react";

export type Tone =
  | "forest" | "green" | "lime" | "teal" | "cyan" | "blue" | "indigo"
  | "violet" | "pink" | "red" | "orange" | "amber" | "graphite";

export function ToneTile({ tone, icon, size = 40, lit = false, className }: {
  tone: Tone;
  icon: ReactNode;
  size?: number;
  /** 常亮（不走灰度闲置态），用于已选中 / 强调场景 */
  lit?: boolean;
  className?: string;
}) {
  return (
    <span
      className={"tone-tile" + (lit ? " is-lit" : "") + (className ? ` ${className}` : "")}
      style={{ ["--tone" as string]: `var(--pc-${tone})`, width: size, height: size }}
      aria-hidden
    >
      {icon}
    </span>
  );
}
