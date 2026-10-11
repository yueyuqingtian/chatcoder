/** Empty —— 统一空状态（plan-114-450）：嗒嗒 + 一句话 + 可选操作。
 * 取代十几套私有 *-empty 写法（sb-empty / sc-empty / mem-empty / ft-empty …）。
 * 嗒嗒用柔和单色版（sketch），跟随文字色，深浅主题都不突兀；size 决定嗒嗒大小与整体密度。
 * 动作：只有眨眼（全局 mascot.css），不额外加动画。 */
import type { ReactNode } from "react";
import { Dada, type DadaMood } from "../mascot/Dada";

export function Empty({ title, desc, action, mood = "calm", size = "md", className = "" }: {
  title: ReactNode;
  desc?: ReactNode;
  action?: ReactNode;
  mood?: DadaMood;
  /** sm：浮层/窄面板；md：卡片内；lg：整页空态 */
  size?: "sm" | "md" | "lg";
  className?: string;
}) {
  const px = size === "sm" ? 36 : size === "lg" ? 88 : 56;
  return (
    <div className={`ui-empty is-${size} ${className}`.trim()} role="status">
      <Dada variant="sketch" mood={mood} size={px} className="ui-empty-dada" />
      <div className="ui-empty-title">{title}</div>
      {desc ? <div className="ui-empty-desc">{desc}</div> : null}
      {action ? <div className="ui-empty-action">{action}</div> : null}
    </div>
  );
}
