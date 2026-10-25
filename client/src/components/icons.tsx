/**
 * 线条 SVG 图标系统(参考图风格)
 *
 * 模仿 Feather / Lucide 风格:
 *  - 24x24 viewBox, 默认 1.75px stroke, round 端点
 *  - 仅线条,无填充,统一 `currentColor`
 *  - 由父容器 color 控制颜色
 *  - 导出兼容 Feather Icons API(签名 `<Icon size color className />`)
 *
 * plan-114-452（v5 同语言）：方形容器类图标（rect）圆角整体放大（1→1.6 / 1.5→2 / 2→3.2），
 *  描边与填充层同步，与嗒嗒「圆润、无尖角」的造型对齐；路径与动效钩子类名不变。
 *
 * plan-230-1144 M5.2 状态协议与尺寸档位：
 *  - `IconSize` 支持档位名（xs=12 / sm=14 / md=16 / lg=20），保留 number 兼容；
 *  - `IconState` 声明可交互图标的状态集合（供组件按状态切换形态）；
 *  - `IconChevron` 统一折叠/展开箭头——单一图标 + rotate 过渡，
 *    取代"展开/折叠切换两个图标组件"的硬切；
 *  - aria-hidden 默认 true 但可被调用方覆盖（按钮内唯一元素需可访问名）。
 *
 * plan-20261014-fix4 高精度与三态：
 *  - 默认描边改为按尺寸换算的光学线宽（见 opticalStroke），横竖线落整数坐标；
 *  - 界面骨架图标（面板开关 / 终端 / 更多 / 搜索 / 设置 / 文件夹）重绘：图形更饱满、留白统一；
 *  - 三态：常规单色线稿 → 悬停（含键盘聚焦）填充层淡入 + 微动 → 聚焦（选中 / 展开 / 按下）
 *    强调部件 `.ic-ac` / `.ic-ac-fill` 着主题强调色、填充层改为强调色淡底，规则见 styles/icons.css。
 */

import { CSSProperties, SVGProps } from "react";

/** 尺寸档位：名称档位或任意数字（兼容旧调用） */
export type IconSize = "xs" | "sm" | "md" | "lg" | number;
/** 图标状态协议：可交互图标按状态切换形态的约定集合 */
export type IconState = "idle" | "hover" | "active" | "focus" | "disabled" | "open" | "loading";
export type IconColor = string;

const _SIZE_TIERS: Record<string, number> = { xs: 12, sm: 14, md: 16, lg: 20 };

/** 档位名 → px；数字原样返回 */
function resolveSize(size: IconSize): number {
  if (typeof size === "number") return size;
  return _SIZE_TIERS[size] ?? 14;
}

/** 各图标的默认描边（调用方未显式传 strokeWidth 时即为此值），由 baseProps 换算为光学线宽 */
const DEFAULT_STROKE = 1.75;

/** plan-20261014-fix4：光学线宽（24 网格单位）。
 *
 * 固定 1.75 单位时，13~14px 渲染的实际线宽只有约 1px，叠加半像素坐标后发虚发灰（用户反馈「图标不精细」）。
 * 现在按渲染尺寸换算：16px 及以下取 2 单位——1.5 倍缩放屏上 16px 图标正好 1 单位 = 1 物理像素，
 * 2 单位描边落在整数坐标上即为 2 个整像素；更大尺寸逐步收窄，实际线宽稳定在约 1.33~1.5px。 */
export function opticalStroke(px: number): number {
  if (px <= 16) return 2;
  return Math.round(((1.333 + (px - 16) * 0.021) * 24 / px) * 1000) / 1000;
}

interface IconProps extends Omit<SVGProps<SVGSVGElement>, "size" | "color"> {
  size?: IconSize;
  color?: IconColor;
  strokeWidth?: number;
  className?: string;
}

/** plan-20261009-ui U3：每枚图标带 `ic ic-<名称>` 类名——图标三态（闲置/聚焦/状态变更）
 *  与部件微动效由 styles/icons.css 依据宿主状态驱动，调用方无需传任何状态参数。 */
const baseProps = (
  size: IconSize,
  color: IconColor,
  strokeWidth: number,
  rest: SVGProps<SVGSVGElement>,
  name?: string,
) => ({
  width: resolveSize(size),
  height: resolveSize(size),
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: color || "currentColor",
  strokeWidth: strokeWidth === DEFAULT_STROKE ? opticalStroke(resolveSize(size)) : strokeWidth,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
  "aria-hidden": true,
  ...rest, // 允许调用方覆盖 aria-hidden 等属性
  className: ["ic", name ? `ic-${name}` : "", rest.className ?? ""].filter(Boolean).join(" "),
});

/* ── 主图标 ──────────────────────────────────────────────── */

/** 齿轮外形（plan-20261014-fix4）：8 齿、齿顶与齿根均为圆弧，按 24 网格计算得到；
 *  原路径的齿沿细碎曲线在 13~16px 下糊成一团，现齿形简洁、齿间留白均匀。 */
export const GEAR_D =
  "M10.68 2.59A9.5 9.5 0 0 1 13.32 2.59L13.74 5.01A7.2 7.2 0 0 1 15.71 5.83L17.72 4.41A9.5 9.5 0 0 1 19.59 6.28L18.17 8.29A7.2 7.2 0 0 1 18.99 10.26L21.41 10.68A9.5 9.5 0 0 1 21.41 13.32L18.99 13.74A7.2 7.2 0 0 1 18.17 15.71L19.59 17.72A9.5 9.5 0 0 1 17.72 19.59L15.71 18.17A7.2 7.2 0 0 1 13.74 18.99L13.32 21.41A9.5 9.5 0 0 1 10.68 21.41L10.26 18.99A7.2 7.2 0 0 1 8.29 18.17L6.28 19.59A9.5 9.5 0 0 1 4.41 17.72L5.83 15.71A7.2 7.2 0 0 1 5.01 13.74L2.59 13.32A9.5 9.5 0 0 1 2.59 10.68L5.01 10.26A7.2 7.2 0 0 1 5.83 8.29L4.41 6.28A9.5 9.5 0 0 1 6.28 4.41L8.29 5.83A7.2 7.2 0 0 1 10.26 5.01L10.68 2.59Z";

/** 设置 (齿轮)：轴心为聚焦态强调部件 */
export function IconSettings({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "settings")}>
      <path className="ic-fill" d={GEAR_D} />
      <path d={GEAR_D} />
      <circle className="ic-ac" cx="12" cy="12" r="3" />
    </svg>
  );
}

/** 显示器 —— plan-334-1661：设置页「电脑操控」入口（表达"屏幕 + 操作"） */
export function IconMonitor({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "monitor")}>
      <rect className="ic-fill" x="3" y="4.5" width="18" height="12.5" rx="3.2" />
      <rect x="3" y="4.5" width="18" height="12.5" rx="3.2" />
      <path d="M9 20.5h6M12 17v3.5" />
    </svg>
  );
}

/** 宠物（爪印）—— plan-73-323：设置页「宠物」分区与面板入口 */
export function IconPet({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "pet")}>
      <ellipse className="ic-fill" cx="12" cy="15.5" rx="5" ry="4" />
      <ellipse cx="12" cy="15.5" rx="5" ry="4" />
      <circle className="ic-dot" style={{ ["--d" as string]: 0 }} cx="7.6" cy="9.6" r="2" />
      <circle className="ic-dot" style={{ ["--d" as string]: 1 }} cx="12" cy="7.8" r="2" />
      <circle className="ic-dot" style={{ ["--d" as string]: 2 }} cx="16.4" cy="9.6" r="2" />
    </svg>
  );
}

/** 文件夹外形（plan-20261014-fix4：横边落在 y=4 / 7 / 20 整数坐标，原 4.5 / 19.5 在小尺寸下发虚） */
const FOLDER_D =
  "M3 6.5A2.5 2.5 0 0 1 5.5 4h3.59a2 2 0 0 1 1.41.59L12.41 6.5c.38.38.89.5 1.42.5H18.5A2.5 2.5 0 0 1 21 9.5v8a2.5 2.5 0 0 1-2.5 2.5h-13A2.5 2.5 0 0 1 3 17.5z";

/** 工作目录(文件夹，闭合态，对齐图 2) */
export function IconFolder({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "folder")}>
      <path className="ic-fill" d={FOLDER_D} />
      <path d={FOLDER_D} />
    </svg>
  );
}

/** 打开的工作目录(文件夹，透视开口态，对齐图 3) */
export function IconFolderOpen({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "folder-open")}>
      <path className="ic-fill" d="M2.5 11.5L4.7 18.2C4.9 18.9 5.5 19.5 6.3 19.5H19.5C20.5 19.5 21.3 18.6 21.1 17.6L19.8 11.5H2.5Z" />
      <path d="M3 6.5C3 5.39543 3.89543 4.5 5 4.5H9.17C9.7 4.5 10.21 4.71 10.59 5.09L12.41 6.91C12.79 7.29 13.3 7.5 13.83 7.5H19C20.1 7.5 21 8.4 21 9.5V11H4.5" />
      <path d="M2.5 11.5L4.7 18.2C4.9 18.9 5.5 19.5 6.3 19.5H19.5C20.5 19.5 21.3 18.6 21.1 17.6L19.8 11.5H2.5Z" />
    </svg>
  );
}

/** 动态文件夹（按 open 属性自动切换闭合/打开状态） */
export function IconFolderDynamic({ open, size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps & { open?: boolean }) {
  return open
    ? <IconFolderOpen size={size} color={color} strokeWidth={strokeWidth} {...rest} />
    : <IconFolder size={size} color={color} strokeWidth={strokeWidth} {...rest} />;
}

/** 新建 + */
export function IconPlus({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "plus")}>
      <g className="ic-spin">
        <line x1="12" y1="5" x2="12" y2="19" />
        <line x1="5" y1="12" x2="19" y2="12" />
      </g>
    </svg>
  );
}

/** 上箭头 (发送) */
export function IconArrowUp({ size = 18, color = "currentColor", strokeWidth = 2, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "arrow-up")}>
      <g className="ic-up">
        <line x1="12" y1="19" x2="12" y2="5" />
        <polyline points="5 12 12 5 19 12" />
      </g>
    </svg>
  );
}

/** 右箭头 */
export function IconChevronRight({ size = 14, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "chevron-right")}>
      <polyline className="ic-right" points="9 18 15 12 9 6" />
    </svg>
  );
}

/** 下箭头 */
export function IconChevronDown({ size = 14, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "chevron-down")}>
      <polyline className="ic-down" points="6 9 12 15 18 9" />
    </svg>
  );
}

/** 左箭头 */
export function IconChevronLeft({ size = 14, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "chevron-left")}>
      <polyline className="ic-left" points="15 18 9 12 15 6" />
    </svg>
  );
}

/** 统一折叠/展开箭头（plan-230-1144 M5.2）。
 *
 * 取代"展开→折叠时替换图标组件"的硬切方式：单一右向 chevron 通过
 * CSS transform 旋转过渡（--dur-3），展开/折叠是连续动画。
 *
 * 用法：
 *   <IconChevron open={expanded} />                 // right → rotate(90deg)=down
 *   <IconChevron direction="down" open={expanded} /> // down → rotate(180deg)=up
 */
export function IconChevron({
  direction = "right",
  open = false,
  size = 14,
  color = "currentColor",
  strokeWidth = 1.75,
  className,
  style,
  ...rest
}: IconProps & { direction?: "right" | "down" | "left"; open?: boolean }) {
  const baseAngle = direction === "right" ? 0 : direction === "down" ? 90 : 180;
  const openDelta = direction === "down" ? 180 : 90; // right: 0→90(down)；down: 90→270(up)
  const angle = open ? baseAngle + openDelta : baseAngle;
  const cls = "ic ic-chevron icon-chevron" + (className ? " " + className : "");
  return (
    <svg
      {...baseProps(size, color, strokeWidth, rest, "chevron")}
      className={cls}
      style={{
        transition: "transform var(--dur-3) var(--ease-out)",
        transform: `rotate(${angle}deg)`,
        ...style,
      }}
    >
      <polyline points="9 18 15 12 9 6" />
    </svg>
  );
}

/** 左面板切换。plan-20261014-fix4：原折叠态的 1px 虚线小框缩小后成了噪点（用户指出的不精细图标之一）；
 *  现为整数坐标的外框 + 分隔线，面板打开时（data-open）左栏铺一层淡底，分隔线为聚焦态强调部件。 */
export function IconPanelLeft({ size = 18, color = "currentColor", strokeWidth = 1.75, open = false, ...rest }: IconProps & { open?: boolean }) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "panel-left")} data-open={open ? "true" : "false"}>
      <path className="ic-fill ic-pane" d="M9 4H6.5A3.5 3.5 0 0 0 3 7.5v9A3.5 3.5 0 0 0 6.5 20H9z" />
      <rect x="3" y="4" width="18" height="16" rx="3.5" />
      <path className="ic-ac" d="M9 4v16" />
    </svg>
  );
}

/** 右面板切换（与左面板开关同一套造型，镜像） */
export function IconPanelRight({ size = 18, color = "currentColor", strokeWidth = 1.75, open = false, ...rest }: IconProps & { open?: boolean }) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "panel-right")} data-open={open ? "true" : "false"}>
      <path className="ic-fill ic-pane" d="M15 4h2.5A3.5 3.5 0 0 1 21 7.5v9a3.5 3.5 0 0 1-3.5 3.5H15z" />
      <rect x="3" y="4" width="18" height="16" rx="3.5" />
      <path className="ic-ac" d="M15 4v16" />
    </svg>
  );
}

/** 群聊 / 对话气泡 */
export function IconMessageSquare({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "message-square")}>
      <path className="ic-fill" d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z" />
      <path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z" />
    </svg>
  );
}

/** 任务 / 检查框 */
export function IconCheckSquare({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "check-square")}>
      <rect className="ic-fill" x="3" y="3" width="18" height="18" rx="3.2" />
      <polyline className="ic-pop" points="9 11 12 14 22 4" />
      <path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11" />
    </svg>
  );
}

/** 用户 */
export function IconUser({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "user")}>
      <circle className="ic-fill" cx="12" cy="7" r="4" />
      <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" />
      <circle cx="12" cy="7" r="4" />
    </svg>
  );
}

/** 用户组 */
export function IconUsers({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "users")}>
      <circle className="ic-fill" cx="9" cy="7" r="4" />
      <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2" />
      <circle cx="9" cy="7" r="4" />
      <path className="ic-right" d="M23 21v-2a4 4 0 0 0-3-3.87" />
      <path className="ic-right" d="M16 3.13a4 4 0 0 1 0 7.75" />
    </svg>
  );
}

/** 数据库 / 知识库 */
export function IconDatabase({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "database")}>
      <ellipse className="ic-fill" cx="12" cy="5" rx="9" ry="3" />
      <ellipse cx="12" cy="5" rx="9" ry="3" />
      <path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3" />
      <path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5" />
    </svg>
  );
}

/** 模型 / cpu */
export function IconCpu({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "cpu")}>
      <rect className="ic-fill" x="9" y="9" width="6" height="6" />
      <rect x="4" y="4" width="16" height="16" rx="3.2" ry="2" />
      <rect className="ic-pop" x="9" y="9" width="6" height="6" />
      <line x1="9" y1="2" x2="9" y2="4" />
      <line x1="15" y1="2" x2="15" y2="4" />
      <line x1="9" y1="20" x2="9" y2="22" />
      <line x1="15" y1="20" x2="15" y2="22" />
      <line x1="20" y1="9" x2="22" y2="9" />
      <line x1="20" y1="14" x2="22" y2="14" />
      <line x1="2" y1="9" x2="4" y2="9" />
      <line x1="2" y1="14" x2="4" y2="14" />
    </svg>
  );
}

/** 列表 / 任务板 */
export function IconLayoutGrid({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "layout-grid")}>
      <rect className="ic-fill" x="3" y="3" width="7" height="7" />
      <rect className="ic-fill" x="14" y="14" width="7" height="7" />
      <rect x="3" y="3" width="7" height="7" />
      <rect className="ic-pop" x="14" y="3" width="7" height="7" />
      <rect x="14" y="14" width="7" height="7" />
      <rect className="ic-pop" x="3" y="14" width="7" height="7" />
    </svg>
  );
}

/** 产物 / 文件 */
export function IconFileText({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "file-text")}>
      <path className="ic-fill" d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
      <polyline points="14 2 14 8 20 8" />
      <line x1="16" y1="13" x2="8" y2="13" />
      <line x1="16" y1="17" x2="8" y2="17" />
      <polyline points="10 9 9 9 8 9" />
    </svg>
  );
}

/** 播放 / 试跑 */
export function IconPlay({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "play")}>
      <polygon className="ic-fill" points="5 3 19 12 5 21 5 3" />
      <polygon className="ic-right" points="5 3 19 12 5 21 5 3" />
    </svg>
  );
}

/** 闪电 / 技能 */
export function IconZap({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "zap")}>
      <polygon className="ic-fill" points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" />
      <polygon className="ic-pop" points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" />
    </svg>
  );
}

/** 魔方 / MCP */
export function IconBox({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "box")}>
      <path className="ic-fill" d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z" />
      <path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z" />
      <polyline points="3.27 6.96 12 12.01 20.73 6.96" />
      <line x1="12" y1="22.08" x2="12" y2="12" />
    </svg>
  );
}

/** 头脑 / 规则 */
export function IconBookOpen({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "book-open")}>
      <path className="ic-fill" d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z" />
      <path className="ic-fill" d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z" />
      <path className="ic-left" d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z" />
      <path className="ic-right" d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z" />
    </svg>
  );
}

/** 记忆 / 大脑（精修现代高光双半球智力回路，简化内部曲折碎线，在 13px 行高下通透无粘连） */
export function IconBrain({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "brain")}>
      <path className="ic-fill" d="M9.5 4a3.5 3.5 0 0 0-3.5 3.5c0 .46.09.9.25 1.3A3.5 3.5 0 0 0 4 12a3.5 3.5 0 0 0 2.25 3.25A3.5 3.5 0 0 0 9.5 20H11V4H9.5z" />
      <path className="ic-fill" d="M14.5 4a3.5 3.5 0 0 1 3.5 3.5c0 .46-.09.9-.25 1.3A3.5 3.5 0 0 1 20 12a3.5 3.5 0 0 1-2.25 3.25A3.5 3.5 0 0 1 14.5 20H13V4h1.5z" />
      <path d="M9.5 4a3.5 3.5 0 0 0-3.5 3.5c0 .46.09.9.25 1.3A3.5 3.5 0 0 0 4 12a3.5 3.5 0 0 0 2.25 3.25A3.5 3.5 0 0 0 9.5 20H11V4H9.5z" />
      <path d="M14.5 4a3.5 3.5 0 0 1 3.5 3.5c0 .46-.09.9-.25 1.3A3.5 3.5 0 0 1 20 12a3.5 3.5 0 0 1-2.25 3.25A3.5 3.5 0 0 1 14.5 20H13V4h1.5z" />
      <path className="ic-pop" d="M7 12h4M13 12h4" strokeWidth={1.3} />
    </svg>
  );
}

/** 压缩 / 上下文折叠 */
export function IconCompress({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "compress")}>
      <path d="M4 9V4h5" />
      <path d="M9 4l-5 5" />
      <path d="M20 9V4h-5" />
      <path d="M15 4l5 5" />
      <path d="M4 15v5h5" />
      <path d="M9 20l-5-5" />
      <path d="M20 15v5h-5" />
      <path d="M15 20l5-5" />
    </svg>
  );
}

/** 终端 / 命令 */
export function IconTerminal({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "terminal")}>
      {/* plan-20261014-fix4：提示符与光标整体居中（4.5→19.5 × 7→17），光标为聚焦态强调部件 */}
      <path className="ic-right" d="M4.5 7 9.5 12l-5 5" />
      <path className="ic-ac" d="M12 17h7.5" />
    </svg>
  );
}

/** 关闭 × */
export function IconX({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "x")}>
      <g className="ic-spin">
        <line x1="18" y1="6" x2="6" y2="18" />
        <line x1="6" y1="6" x2="18" y2="18" />
      </g>
    </svg>
  );
}

/** 停止(方块) */
export function IconStop({ size = 14, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "stop")} fill={color}>
      <rect x="6" y="6" width="12" height="12" rx="1.6" />
    </svg>
  );
}

/** 减号(最小化) */
export function IconMinus({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "minus")}>
      <line x1="5" y1="12" x2="19" y2="12" />
    </svg>
  );
}

/** 方框(最大化) */
export function IconSquare({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "square")}>
      <rect x="3" y="3" width="18" height="18" rx="3.2" ry="2" />
    </svg>
  );
}

/** 复制 / 拷 */
export function IconCopy({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "copy")}>
      <rect className="ic-fill" x="9" y="9" width="13" height="13" rx="3.2" ry="2" />
      <rect className="ic-up" x="9" y="9" width="13" height="13" rx="3.2" ry="2" />
      <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
    </svg>
  );
}

/** 刷新 */
export function IconRefresh({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "refresh")}>
      <polyline points="23 4 23 10 17 10" />
      <polyline points="1 20 1 14 7 14" />
      <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10" />
      <path d="M20.49 15a9 9 0 0 1-14.85 3.36L1 14" />
    </svg>
  );
}

/** 更多 ⋯ */
export function IconMoreHorizontal({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "more-horizontal")}>
      {/* plan-20261014-fix4：描边小圆在小尺寸下发糊，改为实心圆点；中间点为聚焦态强调部件 */}
      <circle className="ic-dot" style={{ ["--d" as string]: 0 }} cx="5" cy="12" r="1.75" fill={color || "currentColor"} stroke="none" />
      <circle className="ic-dot ic-ac-fill" style={{ ["--d" as string]: 1 }} cx="12" cy="12" r="1.75" fill={color || "currentColor"} stroke="none" />
      <circle className="ic-dot" style={{ ["--d" as string]: 2 }} cx="19" cy="12" r="1.75" fill={color || "currentColor"} stroke="none" />
    </svg>
  );
}

/** 麦克风 */
export function IconMic({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "mic")}>
      <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z" />
      <path d="M19 10v2a7 7 0 0 1-14 0v-2" />
      <line x1="12" y1="19" x2="12" y2="23" />
      <line x1="8" y1="23" x2="16" y2="23" />
    </svg>
  );
}

/** 太阳 (浅色) */
export function IconSun({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "sun")}>
      <circle cx="12" cy="12" r="4" />
      <line x1="12" y1="2" x2="12" y2="4" />
      <line x1="12" y1="20" x2="12" y2="22" />
      <line x1="4.93" y1="4.93" x2="6.34" y2="6.34" />
      <line x1="17.66" y1="17.66" x2="19.07" y2="19.07" />
      <line x1="2" y1="12" x2="4" y2="12" />
      <line x1="20" y1="12" x2="22" y2="12" />
      <line x1="4.93" y1="19.07" x2="6.34" y2="17.66" />
      <line x1="17.66" y1="6.34" x2="19.07" y2="4.93" />
    </svg>
  );
}

/** 月亮 (深色) */
export function IconMoon({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "moon")}>
      <path className="ic-fill" d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
      <path className="ic-tilt" d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
    </svg>
  );
}

/** 搜索 */
export function IconSearch({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "search")}>
      <circle className="ic-fill" cx="11" cy="11" r="7.5" />
      <circle className="ic-pop" cx="11" cy="11" r="7.5" />
      <path className="ic-ac" d="m16.5 16.5 4 4" />
    </svg>
  );
}

/** 全屏 / 最大化窗口 */
export function IconMaximize({ size = 14, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "maximize")}>
      <polyline points="15 3 21 3 21 9" />
      <polyline points="9 21 3 21 3 15" />
      <line x1="21" y1="3" x2="14" y2="10" />
      <line x1="3" y1="21" x2="10" y2="14" />
    </svg>
  );
}

/** 收起（两角向内的对角箭头）：浮层卡片收回为胶囊（plan-156-620） */
export function IconMinimize({ size = 14, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "minimize")}>
      <polyline points="4 14 10 14 10 20" />
      <polyline points="20 10 14 10 14 4" />
      <line x1="14" y1="10" x2="21" y2="3" />
      <line x1="3" y1="21" x2="10" y2="14" />
    </svg>
  );
}

/** 自动化调度（日历外框 + 底部调度循环齿轮/脉冲回路） */
export function IconCalendar({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "calendar")}>
      <rect className="ic-fill" x="3" y="4" width="18" height="17" rx="3" ry="3" />
      <rect x="3" y="4" width="18" height="17" rx="3" ry="3" />
      <line className="ic-up" x1="16" y1="2" x2="16" y2="5.5" />
      <line className="ic-up" x1="8" y1="2" x2="8" y2="5.5" />
      <line x1="3" y1="9.5" x2="21" y2="9.5" />
      <g className="ic-spin">
        <path d="M14.5 14.5a3 3 0 1 0-1.8 2.75" />
        <polyline points="14.5 12.5 14.5 14.5 16.5 14.5" />
      </g>
    </svg>
  );
}

/** 信息 */
export function IconInfo({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "info")}>
      <circle className="ic-fill" cx="12" cy="12" r="10" />
      <circle cx="12" cy="12" r="10" />
      <line x1="12" y1="16" x2="12" y2="12" />
      <line className="ic-up" x1="12" y1="8" x2="12.01" y2="8" />
    </svg>
  );
}

/** 警告 */
export function IconAlertTriangle({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "alert-triangle")}>
      <path className="ic-fill" d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
      <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
      <g className="ic-swing">
        <line x1="12" y1="9" x2="12" y2="13" />
        <line x1="12" y1="17" x2="12.01" y2="17" />
      </g>
    </svg>
  );
}

/** 加载 spinner —— plan-91-393：全局统一为左侧会话运行指示同款「轨道环 + 旋转弧」，
 *  颜色跟随 currentColor（轨道 25% 透明度），保证所有进行态观感一致。 */
export function IconSpinner({ size = 18, color = "currentColor", strokeWidth = 2, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "spinner")} viewBox="0 0 24 24" className={`ic ic-spinner ${rest.className || ""}`}>
      <circle cx="12" cy="12" r="8" strokeOpacity="0.25" />
      <path className="ic-spinner-arc" d="M12 4a8 8 0 0 1 8 8" />
    </svg>
  );
}

/** 附件(回形针) */
export function IconPaperclip({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "paperclip")}>
      <path className="ic-tilt" d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48" />
    </svg>
  );
}

/** @提及 */
export function IconAt({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "at")}>
      <circle cx="12" cy="12" r="4" />
      <path d="M16 8v5a3 3 0 0 0 6 0v-1a10 10 0 1 0-4 8" />
    </svg>
  );
}

/** 工具 */
export function IconTool({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "tool")}>
      <path className="ic-tilt" d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z" />
    </svg>
  );
}

/** 全局(地球) */
export function IconGlobe({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "globe")}>
      <circle className="ic-fill" cx="12" cy="12" r="10" />
      <circle cx="12" cy="12" r="10" />
      <line x1="2" y1="12" x2="22" y2="12" />
      <path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z" />
    </svg>
  );
}

/** 工作 / 团队 */
export function IconBriefcase({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "briefcase")}>
      <rect className="ic-fill" x="2" y="7" width="20" height="14" rx="3.2" ry="2" />
      <rect x="2" y="7" width="20" height="14" rx="3.2" ry="2" />
      <path className="ic-up" d="M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16" />
    </svg>
  );
}

/** 对勾 */
export function IconCheck({ size = 18, color = "currentColor", strokeWidth = 2, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "check")}>
      <polyline points="20 6 9 17 4 12" />
    </svg>
  );
}

/** 圆形警示（plan-282-1416：收编 TurnGroup 内联错误图标；
 *  相较三角警示，圆形在密集消息流中更安静、与线性图标体系一致） */
/** 语气徽标（plan-20261025-shell 需求 9）：实心圆底 + 反白符号，用于报错卡与全局提示的行首。
 *  线框圆在浅红底上与文字争夺注意力、又显得单薄；实心徽标一眼可辨语气，符号按 24 网格整数坐标绘制，
 *  16px 渲染时 2 单位线宽落在整像素上（用户屏幕 150% 缩放下锐利）。
 *  圆底取 currentColor（由宿主给语气色），符号取 --tone-glyph（缺省白色）。 */
export function IconToneBadge({ tone, size = 16, ...rest }: IconProps & { tone: "error" | "warning" | "success" | "info" }) {
  const glyph = "var(--tone-glyph, #fff)";
  return (
    <svg {...baseProps(size, "currentColor", 2, rest, `tone-${tone}`)} stroke="none">
      <circle cx="12" cy="12" r="10" fill="currentColor" />
      {tone === "success" ? (
        <path d="M7.5 12.5l3 3 6-6.5" fill="none" stroke={glyph} strokeWidth={2.2} strokeLinecap="round" strokeLinejoin="round" />
      ) : tone === "info" ? (
        <>
          <circle cx="12" cy="7.5" r="1.4" fill={glyph} />
          <path d="M12 11v6" fill="none" stroke={glyph} strokeWidth={2.2} strokeLinecap="round" />
        </>
      ) : (
        <>
          <path d="M12 7v6" fill="none" stroke={glyph} strokeWidth={2.2} strokeLinecap="round" />
          <circle cx="12" cy="16.5" r="1.4" fill={glyph} />
        </>
      )}
    </svg>
  );
}

export function IconAlertCircle({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "alert-circle")}>
      <circle className="ic-fill" cx="12" cy="12" r="9.5" />
      <circle cx="12" cy="12" r="9.5" />
      <g className="ic-pop">
        <line x1="12" y1="7.5" x2="12" y2="13" />
        <circle cx="12" cy="16.5" r="0.6" fill={color || "currentColor"} stroke="none" />
      </g>
    </svg>
  );
}

/** 环形进度（plan-282-1416：收编 ComposerCore 内联上下文占用环）
 *  value: 0~1；trackColor/indicatorColor 允许调用方按语义着色（默认走中性/品牌）。
 *  plan-282-1421（第10项）：新增 loading 模式——压缩等"进行中"场景不再依赖文字，
 *  由圆环自身变成一段固定弧度的旋转加载弧（旋转时长走 --dur-spin，遵循图形约定）。 */
export function IconRingProgress({
  value,
  size = 26,
  strokeWidth = 2.5,
  trackColor = "var(--border)",
  indicatorColor = "currentColor",
  loading = false,
  className,
}: {
  value: number;
  size?: number;
  strokeWidth?: number;
  trackColor?: string;
  indicatorColor?: string;
  /** 加载态：忽略 value，渲染旋转弧（用于压缩/整理上下文等无确定进度的场景） */
  loading?: boolean;
  className?: string;
}) {
  const r = (size - strokeWidth) / 2;
  const circ = 2 * Math.PI * r;
  const clamped = Math.max(0, Math.min(1, value));
  const offset = circ * (1 - clamped);
  return (
    <svg
      width={size}
      height={size}
      viewBox={`0 0 ${size} ${size}`}
      className={className ? `icon-ring-progress ${className}` : "icon-ring-progress"}
      data-loading={loading || undefined}
      aria-hidden="true"
    >
      <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke={trackColor} strokeWidth={strokeWidth} />
      {loading ? (
        /* plan-128-518：加载态 = 左侧会话「执行中」同款转圈——细底环（25% 透明）+ 一段 1/4 周长实色弧，
           平头线帽、线性匀速（session-spin / --dur-spin）。此前是圆头弧 + 另一套关键帧，两处观感不一致。 */
        <circle
          cx={size / 2}
          cy={size / 2}
          r={r}
          fill="none"
          stroke={indicatorColor}
          strokeWidth={strokeWidth}
          strokeDasharray={`${circ / 4} ${circ}`}
          className="icon-ring-spin"
        />
      ) : (
        <circle
          cx={size / 2}
          cy={size / 2}
          r={r}
          fill="none"
          stroke={indicatorColor}
          strokeWidth={strokeWidth}
          strokeDasharray={circ}
          strokeDashoffset={offset}
          strokeLinecap="round"
          transform={`rotate(-90 ${size / 2} ${size / 2})`}
          style={{ transition: "stroke-dashoffset var(--dur-3) var(--ease-standard)" }}
        />
      )}
    </svg>
  );
}

/** 拖拽(六点) */
export function IconGripVertical({ size = 14, color = "currentColor", strokeWidth = 2, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "grip-vertical")}>
      <circle cx="9" cy="6" r="1" />
      <circle cx="9" cy="12" r="1" />
      <circle cx="9" cy="18" r="1" />
      <circle cx="15" cy="6" r="1" />
      <circle cx="15" cy="12" r="1" />
      <circle cx="15" cy="18" r="1" />
    </svg>
  );
}

/** 文件读取（精修：文件外框 + 居中透视眼睛/水平扫描视线，彻底消除对勾造成的任务完成误判） */
export function IconFileRead({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "file-read")}>
      <path className="ic-fill" d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
      <polyline points="14 2 14 8 20 8" />
      <path d="M7.5 15s1.8-2.5 4.5-2.5 4.5 2.5 4.5 2.5-1.8 2.5-4.5 2.5-4.5-2.5-4.5-2.5z" strokeWidth={1.4} />
      <circle className="ic-pop" cx="12" cy="15" r="1.1" fill={color || "currentColor"} stroke="none" />
    </svg>
  );
}

/** 文件写入（精修：圆角折角纸张 + 底部精准 45° 笔尖写入接触线，比例舒展和谐） */
export function IconFileWrite({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "file-write")}>
      <path className="ic-fill" d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9" />
      <path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9" />
      <polyline points="13 2 13 8 19 8" />
      <path className="ic-tilt" d="M10 18l5-5 2.5 2.5-5 5L9 21l1-3z" strokeWidth={1.4} />
    </svg>
  );
}

/** 文件补丁 / 差异修补（代码文件折角框 + 内部横向增删差分指示微条 +/-） */
export function IconFilePatch({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "file-patch")}>
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
      <polyline points="14 2 14 8 20 8" />
      {/* 加号 + 刻线 */}
      <line x1="12" y1="12" x2="12" y2="16" strokeWidth={1.5} />
      <line x1="10" y1="14" x2="14" y2="14" strokeWidth={1.5} />
      {/* 减号 - 刻线 */}
      <line x1="9" y1="18" x2="15" y2="18" strokeWidth={1.5} />
    </svg>
  );
}

/** 复合操作聚合流（专为 ActionClusterRow 设计：多层卡片流水线与执行光芒，替代突兀的放大镜） */
export function IconOperationCluster({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "operation-cluster")}>
      <rect x="3" y="4" width="13" height="13" rx="3.2" />
      <path d="M7 20h11a2 2 0 0 0 2-2V7" />
      {/* 内部操作流水线指示齿/执行符 */}
      <polyline points="7 9 10 12 13 8" strokeWidth={1.5} />
    </svg>
  );
}

/** 任务分步执行清单（todo_write 专属：任务纸板 + 对齐步骤条目与勾选标） */
export function IconChecklist({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "checklist")}>
      <rect className="ic-fill" x="3" y="3" width="18" height="18" rx="3" />
      <rect x="3" y="3" width="18" height="18" rx="3" />
      <path className="ic-pop" d="M7 8l1.5 1.5 3-3" strokeWidth={1.5} />
      <line x1="14" y1="8" x2="17" y2="8" strokeWidth={1.5} />
      <circle cx="8" cy="13" r="1" fill={color || "currentColor"} stroke="none" />
      <line x1="11" y1="13" x2="17" y2="13" strokeWidth={1.5} />
      <circle cx="8" cy="17" r="1" fill={color || "currentColor"} stroke="none" />
      <line x1="11" y1="17" x2="17" y2="17" strokeWidth={1.5} />
    </svg>
  );
}

/** 代码行级/全库搜索特化放大镜（放大镜内嵌代码尖括号刻度点，强化代码检索属性） */
export function IconFileSearch({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "file-search")}>
      <circle cx="10.5" cy="10.5" r="7.5" />
      <line x1="21" y1="21" x2="15.8" y2="15.8" />
      {/* 镜面内嵌代码刻度点/尖角 */}
      <path d="M9 8.5L7 10.5L9 12.5" strokeWidth={1.3} />
      <path d="M12 8.5L14 10.5L12 12.5" strokeWidth={1.3} />
    </svg>
  );
}

/** 包/包装 */
export function IconPackage({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "package")}>
      <path className="ic-fill" d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z" />
      <line x1="16.5" y1="9.4" x2="7.5" y2="4.21" />
      <path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z" />
      <polyline points="3.27 6.96 12 12.01 20.73 6.96" />
      <line x1="12" y1="22.08" x2="12" y2="12" />
    </svg>
  );
}

/** 剪贴板(规则) */
export function IconClipboard({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "clipboard")}>
      <path className="ic-fill" d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2" />
      <path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2" />
      <rect className="ic-up" x="8" y="2" width="8" height="4" rx="1.6" ry="1" />
    </svg>
  );
}

/** 文件差异 */
export function IconDiff({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "diff")}>
      <polyline points="22 12 18 12 15 21 9 3 6 12 2 12" />
    </svg>
  );
}

/** 烧瓶 / 测试 */
export function IconFlask({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "flask")}>
      <path className="ic-fill" d="M10 2v6L4 18a2 2 0 0 0 1.7 3h12.6A2 2 0 0 0 20 18L14 8V2" />
      <g className="ic-swing">
        <path d="M9 2h6" />
        <path d="M10 2v6L4 18a2 2 0 0 0 1.7 3h12.6A2 2 0 0 0 20 18L14 8V2" />
        <line x1="8" y1="14" x2="16" y2="14" />
      </g>
    </svg>
  );
}

/** Git 分支 */
export function IconGitBranch({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "git-branch")}>
      <circle className="ic-fill" cx="18" cy="6" r="3" />
      <circle className="ic-fill" cx="6" cy="18" r="3" />
      <line x1="6" y1="3" x2="6" y2="15" />
      <circle className="ic-pop" cx="18" cy="6" r="3" />
      <circle cx="6" cy="18" r="3" />
      <path d="M18 9a9 9 0 0 1-9 9" />
    </svg>
  );
}

/** 文件差异（带 ± 的文档）：工具卡 Git 栏「更改」。24 网格整数坐标，描边 2 单位。 */
export function IconFileDiff({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "file-diff")}>
      <path className="ic-fill" d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8l-6-6z" />
      <path d="M14 2v4a2 2 0 0 0 2 2h4" />
      <path className="ic-pop" d="M12 11v4M10 13h4" />
      <path d="M10 18h4" />
    </svg>
  );
}

/** Git 提交（线上一个圆点）：工具卡 Git 栏「提交或推送」 */
export function IconGitCommit({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "git-commit")}>
      <circle className="ic-fill" cx="12" cy="12" r="3" />
      <path d="M3 12h6M15 12h6" />
      <circle className="ic-pop" cx="12" cy="12" r="3" />
    </svg>
  );
}

/** Git 图谱（主干两端节点 + 一条旁支）：分支弹窗里的「Git 图谱」入口 */
export function IconGitGraph({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "git-graph")}>
      <circle className="ic-fill" cx="6" cy="5" r="2" />
      <circle className="ic-fill" cx="6" cy="19" r="2" />
      <circle className="ic-fill" cx="18" cy="12" r="2" />
      <path d="M6 7v10M6 12h10" />
      <circle cx="6" cy="5" r="2" />
      <circle cx="6" cy="19" r="2" />
      <circle className="ic-pop" cx="18" cy="12" r="2" />
    </svg>
  );
}

/** 左箭头(返回) */
export function IconArrowLeft({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "arrow-left")}>
      <g className="ic-left">
        <line x1="19" y1="12" x2="5" y2="12" />
        <polyline points="12 19 5 12 12 5" />
      </g>
    </svg>
  );
}

/* ── 向后兼容的文本图标占位(避免破坏其他组件, 但不强推) ───── */

/** 文字图标(中性灰 chip, 用于业务标题字符) */
export function TextIcon({ letter, size = 18, color = "var(--text)", shape = "square", style, className }: {
  letter: React.ReactNode;
  size?: number;
  color?: string;
  shape?: "square" | "round";
  style?: CSSProperties;
  className?: string;
}) {
  return (
    <span
      className={`text-icon${className ? ` ${className}` : ""}`}
      style={{
        width: size,
        height: size,
        background: "var(--bg-muted)",
        color,
        borderRadius: shape === "round" ? "50%" : "var(--radius-xs)",
        fontSize: Math.max(10, Math.round(size * 0.5)),
        lineHeight: 1,
        ...style,
      }}
    >
      {letter}
    </span>
  );
}

/** 业务标题用文字字符(保留给群聊默认头像等场景使用) */
export const IconChat = IconMessageSquare;
export const IconBoard = IconLayoutGrid;
export const IconTeam = IconUsers;
export const IconKnowledge = IconDatabase;
export const IconModel = IconCpu;
export const IconTask = IconCheckSquare;
export const IconArtifact = IconFileText;
export const IconSkill = IconZap;
export const IconMcp = IconBox;
export const IconRule = IconBookOpen;
export const IconMemory = IconBrain;
export const IconGlobal = IconGlobe;
export const IconGroup = IconUsers;
export const IconWorkdir = IconFolder;

/** 插头（MCP 连接） */
export function IconPlug({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "plug")}>
      <path className="ic-fill" d="M7 8h10a1 1 0 0 1 1 1v2a6 6 0 0 1-12 0V9a1 1 0 0 1 1-1z" />
      <g className="ic-up">
        <path d="M9 2v6" />
        <path d="M15 2v6" />
      </g>
      <path d="M7 8h10a1 1 0 0 1 1 1v2a6 6 0 0 1-12 0V9a1 1 0 0 1 1-1z" />
      <path d="M12 17v5" />
    </svg>
  );
}

/** Markdown 标识（M 字母徽章） */
export function IconMarkdown({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "markdown")}>
      <path d="M4 7.5h16v9H4z" />
      <path d="M7.5 14V10l2 2 2-2v4" />
      <path d="M15.5 14v-4" />
      <path d="M17.5 12.5h-1" />
    </svg>
  );
}

/** 回滚（逆时针箭头） */
export function IconRotateCcw({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "rotate-ccw")}>
      <path d="M3 12a9 9 0 1 0 3-6.7L3 8" />
      <path d="M3 3v5h5" />
    </svg>
  );
}

/** 上箭头（chevron up） */
export function IconChevronUp({ size = 14, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "chevron-up")}>
      <path className="ic-up" d="M18 15l-6-6-6 6" />
    </svg>
  );
}

/** 统一折叠箭头：展开态朝下，折叠态朝右，带 90° 旋转过渡 */
export function IconArrowToggle({ open, size = 14, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps & { open: boolean }) {
  return (
    <svg
      {...baseProps(size, color, strokeWidth, rest, "arrow-toggle")}
      style={{
        transform: open ? "rotate(90deg)" : "rotate(0deg)",
        // plan-353-1738 M4b（S5）：统一为折叠展开档时长 + 弹性曲线（原为 --dur-fast + 字面量 ease，
        // 与 .tc-chevron / .debug-card-caret 手感不一致）
        transition: "transform var(--dur-3) var(--ease-spring)",
        ...((rest as Record<string, unknown>).style as Record<string, unknown> | undefined),
      }}
    >
      <path d="M9 18l6-6-6-6" />
    </svg>
  );
}

/** 右箭头 */
export function IconArrowRight({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "arrow-right")}>
      <path className="ic-right" d="M4 12h16M14 6l6 6-6 6" />
    </svg>
  );
}

/** 时钟 */
export function IconClock({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "clock")}>
      <circle className="ic-fill" cx="12" cy="12" r="9" />
      <circle cx="12" cy="12" r="9" />
      <path className="ic-hand" d="M12 7v5l3 2" />
    </svg>
  );
}

/** 图钉（置顶排序）：45° 现代纤细倾斜图钉，精修针尖与重心比例 */
export function IconPin({ size = 18, color = "currentColor", strokeWidth = 1.6, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "pin")}>
      <path className="ic-fill" d="M5 17h14v-2l-2-2V7a5 5 0 0 0-10 0v6l-2 2v2z" />
      <g className="ic-tilt">
        <line x1="12" y1="17" x2="12" y2="22" />
        <path d="M5 17h14v-2l-2-2V7a5 5 0 0 0-10 0v6l-2 2v2z" />
      </g>
    </svg>
  );
}

/** 魔术棒（星星）——AI 智能合并/建议（plan-282-1441 #5） */
export function IconWand({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "wand")}>
      <g className="ic-pop">
        <path d="M15 4V2" />
        <path d="M15 16v-2" />
        <path d="M8 9h2" />
        <path d="M20 9h2" />
        <path d="M17.8 11.8L19 13" />
        <path d="M15 9h0" />
        <path d="M17.8 6.2L19 5" />
        <path d="M12.2 6.2L11 5" />
      </g>
      <path className="ic-tilt" d="M3 21l9-9" />
    </svg>
  );
}
export function IconArchive({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "archive")}>
      <path className="ic-fill" d="M5 8v11a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8" />
      <rect className="ic-lid" x="3" y="4" width="18" height="4" rx="1.6" />
      <path d="M5 8v11a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8" />
      <line x1="9" y1="12" x2="15" y2="12" />
    </svg>
  );
}

/** 垃圾桶（盖+桶+两条竖线）——永久删除类操作（plan-282-1441 #4 归档页批量删除） */
export function IconTrash({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "trash")}>
      <path className="ic-fill" d="M19 6l-1 14a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1L5 6" />
      <g className="ic-lid">
        <path d="M3 6h18" />
        <path d="M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2" />
      </g>
      <path d="M19 6l-1 14a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1L5 6" />
      <line x1="10" y1="11" x2="10" y2="17" />
      <line x1="14" y1="11" x2="14" y2="17" />
    </svg>
  );
}

/** 铅笔（编辑） */
export function IconPencil({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "pencil")}>
      <path className="ic-fill" d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4Z" />
      <path d="M12 20h9" />
      <path className="ic-tilt" d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4Z" />
    </svg>
  );
}

/** 眼睛（显示密钥） */
export function IconEye({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "eye")}>
      <path className="ic-fill" d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7Z" />
      <path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7Z" />
      <circle className="ic-pop" cx="12" cy="12" r="3" />
    </svg>
  );
}

/** 眼睛（隐藏密钥，带斜线） */
export function IconEyeOff({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "eye-off")}>
      <path d="M10.7 5.1A9.9 9.9 0 0 1 12 5c6.5 0 10 7 10 7a17 17 0 0 1-3 3.9" />
      <path d="M6.6 6.6A17 17 0 0 0 2 12s3.5 7 10 7a9.9 9.9 0 0 0 4.4-1" />
      <path d="M9.9 9.9a3 3 0 0 0 4.2 4.2" />
      <path d="M2 2l20 20" />
    </svg>
  );
}

/** 字母排序（AZ） */
export function IconSortAlpha({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "sort-alpha")}>
      <path d="M3 6h4l2 5-2 5H3" />
      <path d="M17 6v12" />
      <path d="M13 18h8" />
      <path d="M13 6h8" />
    </svg>
  );
}

/** 准星（选择元素） */
export function IconTarget({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "target")}>
      <circle className="ic-fill" cx="12" cy="12" r="8" />
      <circle cx="12" cy="12" r="8" />
      <circle className="ic-pop" cx="12" cy="12" r="2" />
      <path d="M12 2v4M12 18v4M2 12h4M18 12h4" />
    </svg>
  );
}

/** 点赞（大拇指向上） */
export function IconThumbsUp({ size = 16, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "thumbs-up")}>
      <path className="ic-fill" d="M14 9V5a3 3 0 0 0-3-3l-4 9v11h11.28a2 2 0 0 0 2-1.7l1.38-9a2 2 0 0 0-2-2.3zM7 22H4a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2h3" />
      <path className="ic-tilt" d="M14 9V5a3 3 0 0 0-3-3l-4 9v11h11.28a2 2 0 0 0 2-1.7l1.38-9a2 2 0 0 0-2-2.3zM7 22H4a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2h3" />
    </svg>
  );
}

/** 踩（大拇指向下） */
export function IconThumbsDown({ size = 16, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "thumbs-down")}>
      <path className="ic-fill" d="M10 15v4a3 3 0 0 0 3 3l4-9V2H5.72a2 2 0 0 0-2 1.7l-1.38 9a2 2 0 0 0 2 2.3zM17 2h3a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2h-3" />
      <path className="ic-tilt" d="M10 15v4a3 3 0 0 0 3 3l4-9V2H5.72a2 2 0 0 0-2 1.7l-1.38 9a2 2 0 0 0 2 2.3zM17 2h3a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2h-3" />
    </svg>
  );
}

/** 外部链接（右上箭头） */
export function IconExternalLink({ size = 16, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "external-link")}>
      <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
      <g className="ic-ne">
        <polyline points="15 3 21 3 21 9" />
        <line x1="10" y1="14" x2="21" y2="3" />
      </g>
    </svg>
  );
}

/** 暂停（竖线） */
export function IconPause({ size = 16, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "pause")}>
      <rect x="6" y="4" width="4" height="16" rx="1.6" />
      <rect x="14" y="4" width="4" height="16" rx="1.6" />
    </svg>
  );
}

/** 图片（风景） */
export function IconImage({ size = 16, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "image")}>
      <rect className="ic-fill" x="3" y="3" width="18" height="18" rx="3.2" />
      <rect x="3" y="3" width="18" height="18" rx="3.2" />
      <circle className="ic-pop" cx="8.5" cy="8.5" r="1.5" />
      <polyline points="21 15 16 10 5 21" />
    </svg>
  );
}

/** 多模态（文本 + 图像可同时输入）——plan-238-1210:
 *  左侧图文小卡 + 右侧文本行，语义为"图文可一起输入"，区别于 IconImage（纯图片/附件）。
 *  12px 下仍保持清晰：只保留 3 条粗笔画，避免小尺寸糊成一团。 */
export function IconMultimodal({ size = 14, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "multimodal")}>
      {/* 左：图像卡（内嵌小太阳点） */}
      <rect x="2.5" y="4" width="10" height="10" rx="3.2" />
      <circle cx="6" cy="7.5" r="1" />
      {/* 右：文本行 */}
      <line x1="15" y1="7" x2="21.5" y2="7" />
      <line x1="15" y1="11" x2="21.5" y2="11" />
      {/* 底部：两类输入合流 */}
      <line x1="2.5" y1="17.5" x2="21.5" y2="17.5" />
    </svg>
  );
}

/** 扳手（技能/工具） */
export function IconWrench({ size = 16, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "wrench")}>
      <path className="ic-tilt" d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z" />
    </svg>
  );
}

/** 下箭头（跳转底部） */
export function IconArrowDown({ size = 18, color = "currentColor", strokeWidth = 2, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "arrow-down")}>
      <g className="ic-down">
        <line x1="12" y1="5" x2="12" y2="19" />
        <polyline points="19 12 12 19 5 12" />
      </g>
    </svg>
  );
}

/** 对勾圆圈（完成/成功状态） */
export function IconCheckCircle({ size = 16, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "check-circle")}>
      <path className="ic-fill" d="M22 11.08V12a10 10 0 1 1-5.93-9.14" />
      <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" />
      <polyline className="ic-pop" points="22 4 12 14.01 9 11.01" />
    </svg>
  );
}

/** 盾牌（权限/完全访问标识，精修外轮廓 + 内部安全核刻线） */
export function IconShield({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "shield")}>
      <path className="ic-fill" d="M12 2.8c2.5 1.7 5.1 2.4 7.3 2.5.5 0 .7.3.7.8v5.8c0 4.4-3 7.7-8 9.9-5-2.2-8-5.5-8-9.9V6.1c0-.5.2-.8.7-.8 2.2-.1 4.8-.8 7.3-2.5z" />
      <path d="M12 2.8c2.5 1.7 5.1 2.4 7.3 2.5.5 0 .7.3.7.8v5.8c0 4.4-3 7.7-8 9.9-5-2.2-8-5.5-8-9.9V6.1c0-.5.2-.8.7-.8 2.2-.1 4.8-.8 7.3-2.5z" />
      <g className="ic-pop">
        <path d="M12 8v5" />
        <circle cx="12" cy="16" r="1" fill={color || "currentColor"} stroke="none" />
      </g>
    </svg>
  );
}

/** 下载（导入入口，对齐 zcode 技能页头部） */
export function IconDownload({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "download")}>
      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
      <g className="ic-down">
        <polyline points="7 10 12 15 17 10" />
        <line x1="12" y1="15" x2="12" y2="3" />
      </g>
    </svg>
  );
}

/** 调色板（外观设置，对齐 zcode 设置导航） */
export function IconPalette({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "palette")}>
      <path className="ic-fill" d="M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10c.926 0 1.648-.746 1.648-1.688 0-.437-.18-.835-.437-1.125-.29-.289-.438-.652-.438-1.125a1.64 1.64 0 0 1 1.668-1.668h1.996c3.051 0 5.555-2.503 5.555-5.554C21.965 6.012 17.461 2 12 2z" />
      <g className="ic-tilt">
        <circle cx="13.5" cy="6.5" r=".5" />
        <circle cx="17.5" cy="10.5" r=".5" />
        <circle cx="8.5" cy="7.5" r=".5" />
        <circle cx="6.5" cy="12.5" r=".5" />
        <path d="M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10c.926 0 1.648-.746 1.648-1.688 0-.437-.18-.835-.437-1.125-.29-.289-.438-.652-.438-1.125a1.64 1.64 0 0 1 1.668-1.668h1.996c3.051 0 5.555-2.503 5.555-5.554C21.965 6.012 17.461 2 12 2z" />
      </g>
    </svg>
  );
}

/** 锚（钩子设置，对齐 zcode 设置导航） */
export function IconAnchor({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "anchor")}>
      <g className="ic-swing">
        <circle cx="12" cy="5" r="3" />
        <line x1="12" y1="22" x2="12" y2="8" />
        <path d="M5 12H2a10 10 0 0 0 20 0h-3" />
      </g>
    </svg>
  );
}

/** 柱状图（使用统计，对齐 zcode 设置导航） */
export function IconBarChart({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "bar-chart")}>
      <line className="ic-dot" style={{ ["--d" as string]: 0 }} x1="6" y1="20" x2="6" y2="16" />
      <line className="ic-dot" style={{ ["--d" as string]: 1 }} x1="12" y1="20" x2="12" y2="10" />
      <line className="ic-dot" style={{ ["--d" as string]: 2 }} x1="18" y1="20" x2="18" y2="4" />
    </svg>
  );
}

/** 图层/分组（替代旧版井号，直观传达分组归纳与层级收拢） */
export function IconLayers({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "layers")}>
      <polygon className="ic-fill" points="12 2 2 7 12 12 22 7 12 2" />
      <polygon className="ic-up" points="12 2 2 7 12 12 22 7 12 2" />
      <polyline points="2 12 12 17 22 12" />
      <polyline className="ic-down" points="2 17 12 22 22 17" />
    </svg>
  );
}

/** 井号（兼容旧导出，底层映射为图层分组） */
export const IconHash = IconLayers;

/** 排序切换（阶梯降序横线 + 垂直升降上下微双向箭头，消除筛选歧义） */
export function IconSortDesc({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "sort-desc")}>
      <line x1="3" y1="6" x2="13" y2="6" />
      <line x1="3" y1="12" x2="10" y2="12" />
      <line x1="3" y1="18" x2="7" y2="18" />
      <path className="ic-down" d="M17 7v11M14 15l3 3 3-3" />
    </svg>
  );
}

/** 列表筛选（保留兼容，但优先推荐 IconSortDesc） */
export function IconListFilter({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "list-filter")}>
      <line x1="3" y1="6" x2="13" y2="6" />
      <circle cx="17" cy="6" r="2" />
      <line x1="3" y1="12" x2="7" y2="12" />
      <circle cx="11" cy="12" r="2" />
      <line x1="15" y1="12" x2="21" y2="12" />
      <line x1="3" y1="18" x2="15" y2="18" />
      <circle cx="19" cy="18" r="2" />
    </svg>
  );
}

/** 对角展开（侧栏宽度展开，对齐 zcode） */
export function IconExpandDiagonal({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "expand-diagonal")}>
      <polyline points="15 3 21 3 21 9" />
      <polyline points="9 21 3 21 3 15" />
      <line x1="21" y1="3" x2="14" y2="10" />
      <line x1="3" y1="21" x2="10" y2="14" />
    </svg>
  );
}

/** 开发者工具/代码 */
export function IconCode({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "code")}>
      <polyline className="ic-right" points="16 18 22 12 16 6" />
      <polyline className="ic-left" points="8 6 2 12 8 18" />
    </svg>
  );
}

/** Bug / 调试图标 */
export function IconBug({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "bug")}>
      <rect className="ic-fill" width="8" height="14" x="8" y="6" rx="4" />
      <g className="ic-swing">
        <rect width="8" height="14" x="8" y="6" rx="4" />
        <path d="m19 7-3 2" />
        <path d="m5 7 3 2" />
        <path d="m19 19-3-2" />
        <path d="m5 19 3-2" />
        <path d="M20 13h-4" />
        <path d="M4 13h4" />
        <path d="m10 4 1 2" />
        <path d="m14 4-1 2" />
      </g>
    </svg>
  );
}

/** 拍照/截图 */
export function IconCamera({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "camera")}>
      <path className="ic-fill" d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z" />
      <path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z" />
      <circle className="ic-pop" cx="12" cy="13" r="4" />
    </svg>
  );
}

/** 新建项目文件夹（圆润文件夹轮廓 + 右下角悬浮精巧加号微章，消除方框加号歧义） */
export function IconFolderPlus({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "folder-plus")}>
      {/* plan-20261014-fix4：与文件夹同一外形，加号收进夹身中央（整数坐标），为聚焦态强调部件 */}
      <path className="ic-fill" d={FOLDER_D} />
      <path d={FOLDER_D} />
      <g className="ic-spin">
        <path className="ic-ac" d="M12 10v6M9 13h6" />
      </g>
    </svg>
  );
}

/** 方形加号（兼容旧导出，映射为 IconFolderPlus） */
export const IconSquarePlus = IconFolderPlus;

/* ── 打开方式品牌专属彩色图标（对齐图二） ──────────────────────────────────── */

/** 品牌图标：Windows 文件资源管理器（黄蓝夹心文件夹） */
export function IconBrandExplorer({ size = 16, className }: { size?: number; className?: string }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" className={className} aria-hidden="true">
      <path d="M2.5 7C2.5 5.6 3.6 4.5 5 4.5H9.5L12 7.5H19C20.4 7.5 21.5 8.6 21.5 10V18C21.5 19.4 20.4 20.5 19 20.5H5C3.6 20.5 2.5 19.4 2.5 18V7Z" fill="#F5BA31" />
      <path d="M2.5 10.5C2.5 9.5 3.3 8.7 4.3 8.7H19.7C20.7 8.7 21.5 9.5 21.5 10.5V18C21.5 19.4 20.4 20.5 19 20.5H5C3.6 20.5 2.5 19.4 2.5 18V10.5Z" fill="#F8D147" />
      <path d="M2 13H22V17.5C22 18.9 20.9 20 19.5 20H4.5C3.1 20 2 18.9 2 17.5V13Z" fill="#0078D4" fillOpacity="0.88" />
    </svg>
  );
}

/** 品牌图标：Visual Studio Code（经典蓝蝴蝶结折角） */
export function IconBrandVSCode({ size = 16, className }: { size?: number; className?: string }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" className={className} aria-hidden="true">
      <path d="M17.5 2.5L7.5 10.2L3.8 7.3C3.3 6.9 2.5 7.2 2.5 7.8V16.2C2.5 16.8 3.3 17.1 3.8 16.7L7.5 13.8L17.5 21.5C18.2 22 19.2 21.6 19.4 20.7L21.8 4.3C22 3.4 21.1 2.7 20.3 3L17.5 2.5Z" fill="#0066B8" />
      <path d="M17.5 2.5L6.8 12L17.5 21.5L21.5 19.5V4.5L17.5 2.5Z" fill="#007ACC" />
      <path d="M17.5 2.5L13.5 12L17.5 21.5L21.5 19.5V4.5L17.5 2.5Z" fill="#1F9CF0" />
      <path d="M3.8 7.3L13.8 15.5L17.5 12.5L7.5 4.8L3.8 7.3Z" fill="#0065A9" />
    </svg>
  );
}

/** 品牌图标：IntelliJ IDEA（经典黑底色块方框 + 彩条） */
export function IconBrandIdea({ size = 16, className }: { size?: number; className?: string }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" className={className} aria-hidden="true">
      <rect x="2" y="2" width="20" height="20" rx="4" fill="#21D789" />
      <path d="M18 2H6C3.8 2 2 3.8 2 6V18C2 20.2 3.8 22 6 22H18C20.2 22 22 20.2 22 18V6C22 3.8 20.2 2 18 2Z" fill="#000000" />
      <path d="M4 18.5L8.5 15L6 20H4V18.5Z" fill="#FE2857" />
      <path d="M14 4L20 8L16.5 12L12 9L14 4Z" fill="#007ACC" />
      <path d="M20 18L15 20L13 16L18 14L20 18Z" fill="#FC801D" />
      <path d="M4 6L9 4L11 8L6 10L4 6Z" fill="#36D468" />
      <rect x="4.5" y="4.5" width="15" height="15" rx="3.2" fill="#000000" fillOpacity="0.88" />
      <text x="6" y="14" fill="#FFFFFF" fontSize="7.5" fontWeight="900" fontFamily="sans-serif">IJ</text>
      <rect x="6" y="15.5" width="7" height="1.5" fill="#FFFFFF" />
    </svg>
  );
}

/** 品牌图标：Windows 终端（黑框灰底 + 蓝色命令提示符方块） */
export function IconBrandWindowsTerminal({ size = 16, className }: { size?: number; className?: string }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" className={className} aria-hidden="true">
      <rect x="2" y="3" width="20" height="18" rx="3" fill="#1C1C1C" />
      <rect x="2" y="3" width="20" height="18" rx="3" stroke="#4F4F4F" strokeWidth="1" />
      <path d="M6 8L10 12L6 16" stroke="#4CC2FF" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
      <line x1="12" y1="16" x2="17" y2="16" stroke="#FFFFFF" strokeWidth="2" strokeLinecap="round" />
    </svg>
  );
}

/* ── plan-20261009-ui U3：新增线性图标（几何取自 Lucide，ISC License），带双色调与部件微动效 ── */

/** 拼图（插件） */
export function IconPuzzle({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  const d = "M15.39 4.39a1 1 0 0 0 1.68-.474 2.5 2.5 0 1 1 3.014 3.015 1 1 0 0 0-.474 1.68l1.683 1.682a2.414 2.414 0 0 1 0 3.414L19.61 15.39a1 1 0 0 1-1.68-.474 2.5 2.5 0 1 0-3.014 3.015 1 1 0 0 1 .474 1.68l-1.683 1.682a2.414 2.414 0 0 1-3.414 0L8.61 19.61a1 1 0 0 0-1.68.474 2.5 2.5 0 1 1-3.014-3.015 1 1 0 0 0 .474-1.68l-1.683-1.682a2.414 2.414 0 0 1 0-3.414L4.39 8.61a1 1 0 0 1 1.68.474 2.5 2.5 0 1 0 3.014-3.015 1 1 0 0 1-.474-1.68l1.683-1.682a2.414 2.414 0 0 1 3.414 0z";
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "puzzle")}>
      <path className="ic-fill" d={d} />
      <path className="ic-tilt" d={d} />
    </svg>
  );
}

/** 星芒（技能 / 智能体模式） */
export function IconSparkles({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  const d = "M11.017 2.814a1 1 0 0 1 1.966 0l1.051 5.558a2 2 0 0 0 1.594 1.594l5.558 1.051a1 1 0 0 1 0 1.966l-5.558 1.051a2 2 0 0 0-1.594 1.594l-1.051 5.558a1 1 0 0 1-1.966 0l-1.051-5.558a2 2 0 0 0-1.594-1.594l-5.558-1.051a1 1 0 0 1 0-1.966l5.558-1.051a2 2 0 0 0 1.594-1.594z";
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "sparkles")}>
      <path className="ic-fill" d={d} />
      <path className="ic-pop" d={d} />
      <g className="ic-dot" style={{ ["--d" as string]: 1 }}>
        <path d="M20 2v4" />
        <path d="M22 4h-4" />
      </g>
      <circle className="ic-dot" style={{ ["--d" as string]: 2 }} cx="4" cy="20" r="2" />
    </svg>
  );
}

/** 待办清单（计划模式） */
export function IconListTodo({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "list-todo")}>
      <rect className="ic-fill" x="3" y="4" width="6" height="6" rx="1.6" />
      <path d="M13 5h8" />
      <path d="M13 12h8" />
      <path d="M13 19h8" />
      <path className="ic-pop" d="m3 17 2 2 4-4" />
      <rect x="3" y="4" width="6" height="6" rx="1.6" />
    </svg>
  );
}

/** 勾选清单（任务标签） */
export function IconListChecks({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "list-checks")}>
      <path d="M13 5h8" />
      <path d="M13 12h8" />
      <path d="M13 19h8" />
      <path className="ic-pop" d="m3 17 2 2 4-4" />
      <path className="ic-pop" d="m3 7 2 2 4-4" />
    </svg>
  );
}

/** 右上箭头（前往 / 探索更多） */
export function IconArrowUpRight({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "arrow-up-right")}>
      <g className="ic-ne">
        <path d="M7 7h10v10" />
        <path d="M7 17 17 7" />
      </g>
    </svg>
  );
}

/** 锁（会话工作区已锁定） */
export function IconLock({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "lock")}>
      <rect className="ic-fill" x="3" y="10" width="18" height="12" rx="3.2" />
      <path className="ic-up" d="M7 10V7a5 5 0 0 1 10 0v3" />
      <rect x="3" y="10" width="18" height="12" rx="3.2" />
      <circle cx="12" cy="16" r="1" />
    </svg>
  );
}

/** 仪表（思考深度） */
export function IconGauge({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "gauge")}>
      <path d="M3.34 19a10 10 0 1 1 17.32 0" />
      <path className="ic-needle" d="m12 14 4-4" />
    </svg>
  );
}

/** 应用窗口（浏览器标签） */
export function IconAppWindow({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "app-window")}>
      <rect className="ic-fill" x="2" y="4" width="20" height="16" rx="3.2" />
      <rect x="2" y="4" width="20" height="16" rx="3.2" />
      <path d="M10 4v4" />
      <path d="M2 8h20" />
      <path d="M6 4v4" />
    </svg>
  );
}

/** 方框终端（终端标签） */
export function IconSquareTerminal({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "square-terminal")}>
      <rect className="ic-fill" width="18" height="18" x="3" y="3" rx="3.2" ry="2" />
      <path className="ic-right" d="m7 11 2-2-2-2" />
      <path d="M11 13h4" />
      <rect width="18" height="18" x="3" y="3" rx="3.2" ry="2" />
    </svg>
  );
}

/** 文件树（文件标签） */
export function IconFolderTree({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  const a = "M20 10a1 1 0 0 0 1-1V6a1 1 0 0 0-1-1h-2.5a1 1 0 0 1-.8-.4l-.9-1.2A1 1 0 0 0 15 3h-2a1 1 0 0 0-1 1v5a1 1 0 0 0 1 1Z";
  const b = "M20 21a1 1 0 0 0 1-1v-3a1 1 0 0 0-1-1h-2.9a1 1 0 0 1-.88-.55l-.42-.85a1 1 0 0 0-.92-.6H13a1 1 0 0 0-1 1v5a1 1 0 0 0 1 1Z";
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "folder-tree")}>
      <path className="ic-fill" d={a} />
      <path className="ic-fill" d={b} />
      <path className="ic-right" d={a} />
      <path className="ic-right" d={b} />
      <path d="M3 5a2 2 0 0 0 2 2h3" />
      <path d="M3 3v13a2 2 0 0 0 2 2h3" />
    </svg>
  );
}

/** 机器人（子代理） */
export function IconBot({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "bot")}>
      <rect className="ic-fill" width="16" height="12" x="4" y="8" rx="3.2" />
      <path className="ic-swing" d="M12 8V4H8" />
      <rect width="16" height="12" x="4" y="8" rx="3.2" />
      <path d="M2 14h2" />
      <path d="M20 14h2" />
      <path className="ic-dot" style={{ ["--d" as string]: 0 }} d="M15 13v2" />
      <path className="ic-dot" style={{ ["--d" as string]: 1 }} d="M9 13v2" />
    </svg>
  );
}

/** 火焰（热门） */
export function IconFlame({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  const d = "M12 3q1 4 4 6.5t3 5.5a1 1 0 0 1-14 0 5 5 0 0 1 1-3 1 1 0 0 0 5 0c0-2-1.5-3-1.5-5q0-2 2.5-4";
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "flame")}>
      <path className="ic-fill" d={d} />
      <path className="ic-swing" d={d} />
    </svg>
  );
}

/** 历史（最近 / 最新） */
export function IconHistory({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "history")}>
      <path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8" />
      <path d="M3 3v5h5" />
      <path className="ic-hand" d="M12 7v5l4 2" />
    </svg>
  );
}

/** 回车（发送提示） */
export function IconCornerDownLeft({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "corner-down-left")}>
      <path d="M20 4v7a4 4 0 0 1-4 4H4" />
      <path className="ic-left" d="m9 10-5 5 5 5" />
    </svg>
  );
}

/** 调节（管理入口） */
export function IconSliders({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "sliders")}>
      <path d="M14 17H5" />
      <path d="M19 7h-9" />
      <circle className="ic-left" cx="17" cy="17" r="3" />
      <circle className="ic-right" cx="7" cy="7" r="3" />
    </svg>
  );
}

/** 文件叠（变更文件） */
export function IconFileStack({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  const top = "M21 6a2 2 0 0 0-.586-1.414l-2-2A2 2 0 0 0 17 2h-3a1 1 0 0 0-1 1v8a1 1 0 0 0 1 1h6a1 1 0 0 0 1-1z";
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "file-stack")}>
      <path className="ic-fill" d={top} />
      <path d="M11 21a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1v-8a1 1 0 0 1 1-1" />
      <path d="M16 16a1 1 0 0 1-1 1H9a1 1 0 0 1-1-1V8a1 1 0 0 1 1-1" />
      <path className="ic-up" d={top} />
    </svg>
  );
}

/** 积木（拓展 / 模块化） */
export function IconBlocks({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "blocks")}>
      <rect className="ic-fill" x="14" y="2" width="8" height="8" rx="1.6" />
      <path d="M10 22V7a1 1 0 0 0-1-1H4a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-5a1 1 0 0 0-1-1H2" />
      <rect className="ic-ne" x="14" y="2" width="8" height="8" rx="1.6" />
    </svg>
  );
}

/** 望远镜（空态：没找到） */
export function IconTelescope({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "telescope")}>
      <g className="ic-tilt">
        <path d="m10.065 12.493-6.18 1.318a.934.934 0 0 1-1.108-.702l-.537-2.15a1.07 1.07 0 0 1 .691-1.265l13.504-4.44" />
        <path d="m13.56 11.747 4.332-.924" />
        <path d="M16.485 5.94a2 2 0 0 1 1.455-2.425l1.09-.272a1 1 0 0 1 1.212.727l1.515 6.06a1 1 0 0 1-.727 1.213l-1.09.272a2 2 0 0 1-2.425-1.455z" />
        <path d="m6.158 8.633 1.114 4.456" />
      </g>
      <path d="m16 21-3.105-6.21" />
      <path d="m8 21 3.105-6.21" />
      <circle cx="12" cy="13" r="2" />
    </svg>
  );
}

/** 有序列表（排队） */
export function IconListOrdered({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "list-ordered")}>
      <path className="ic-right" d="M11 5h10" />
      <path d="M11 12h10" />
      <path d="M11 19h10" />
      <path d="M4 4h1v5" />
      <path d="M4 9h2" />
      <path d="M6.5 20H3.4c0-1 2.6-1.925 2.6-3.5a1.5 1.5 0 0 0-2.6-1.02" />
    </svg>
  );
}
