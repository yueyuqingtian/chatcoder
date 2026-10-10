/** 项目图标（plan-20261009-ui U1/U3）：Phosphor duotone 子集，线条与底色层都取项目色；
 *  底色层透明度随宿主状态变化（闲置浅、悬停/选中深，见 styles/icons.css .proj-ic）。 */
import { memo } from "react";
import { PROJECT_ICON_DEFS } from "./projectIconData";
import { projectColorVar, projectIconKey } from "./projectMeta";

const DEF_BY_KEY = new Map(PROJECT_ICON_DEFS.map((d) => [d.key, d]));

export const ProjectIcon = memo(function ProjectIcon({ icon, color, size = 16, tile = false, tileSize, className }: {
  icon?: string | null;
  color?: string | null;
  size?: number;
  /** 块状（项目色淡底的圆角方块），用于名称输入框左侧 / 选择器预览 */
  tile?: boolean;
  tileSize?: number;
  className?: string;
}) {
  const def = DEF_BY_KEY.get(projectIconKey(icon)) ?? PROJECT_ICON_DEFS[0];
  const box = tile ? (tileSize ?? Math.round(size * 1.75)) : size;
  return (
    <span
      className={"proj-ic" + (tile ? " is-tile" : "") + (className ? ` ${className}` : "")}
      style={{ ["--proj-c" as string]: projectColorVar(color), width: box, height: box }}
      aria-hidden
    >
      <svg width={size} height={size} viewBox="0 0 256 256" fill="currentColor">
        <g className="ph-duo" dangerouslySetInnerHTML={{ __html: def.duo }} />
        <g dangerouslySetInnerHTML={{ __html: def.line }} />
      </svg>
    </span>
  );
});
