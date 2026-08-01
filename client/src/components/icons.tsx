/**
 * 线条 SVG 图标系统(参考图风格)
 *
 * 模仿 Feather / Lucide 风格:
 *  - 24x24 viewBox, 默认 1.75px stroke, round 端点
 *  - 仅线条,无填充,统一 `currentColor`
 *  - 由父容器 color 控制颜色
 *  - 导出兼容 Feather Icons API(签名 `<Icon size color className />`)
 */

import { CSSProperties, SVGProps } from "react";

export type IconSize = number;
export type IconColor = string;

interface IconProps extends Omit<SVGProps<SVGSVGElement>, "size" | "color"> {
  size?: IconSize;
  color?: IconColor;
  strokeWidth?: number;
  className?: string;
}

const baseProps = (
  size: IconSize,
  color: IconColor,
  strokeWidth: number,
  rest: SVGProps<SVGSVGElement>,
) => ({
  width: size,
  height: size,
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: color || "currentColor",
  strokeWidth,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
  "aria-hidden": true,
  ...rest,
});

/* ── 主图标 ──────────────────────────────────────────────── */

/** 设置 (齿轮) */
export function IconSettings({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest)}>
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06A1.65 1.65 0 0 0 4.6 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06A1.65 1.65 0 0 0 9 4.6a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9c.36.15.65.41.86.73.21.32.31.7.31 1.08V11a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
    </svg>
  );
}

/** 工作目录(文件夹) */
export function IconFolder({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest)}>
      <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
    </svg>
  );
}

/** 新建 + */
export function IconPlus({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest)}>
      <line x1="12" y1="5" x2="12" y2="19" />
      <line x1="5" y1="12" x2="19" y2="12" />
    </svg>
  );
}

/** 上箭头 (发送) */
export function IconArrowUp({ size = 18, color = "currentColor", strokeWidth = 2, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest)}>
      <line x1="12" y1="19" x2="12" y2="5" />
      <polyline points="5 12 12 5 19 12" />
    </svg>
  );
}

/** 右箭头 */
export function IconChevronRight({ size = 14, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest)}>
      <polyline points="9 18 15 12 9 6" />
    </svg>
  );
}

/** 下箭头 */
export function IconChevronDown({ size = 14, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest)}>
      <polyline points="6 9 12 15 18 9" />
    </svg>
  );
}

/** 左箭头 */
export function IconChevronLeft({ size = 14, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest)}>
      <polyline points="15 18 9 12 15 6" />
    </svg>
  );
}

/** 左面板 */
export function IconPanelLeft({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest)}>
      <rect x="3" y="4" width="18" height="16" rx="2" ry="2" />
      <line x1="9" y1="4" x2="9" y2="20" />
    </svg>
  );
}

/** 右面板 */
export function IconPanelRight({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest)}>
      <rect x="3" y="4" width="18" height="16" rx="2" ry="2" />
      <line x1="15" y1="4" x2="15" y2="20" />
    </svg>
  );
}

/** 群聊 / 对话气泡 */
export function IconMessageSquare({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest)}>
      <path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z" />
    </svg>
  );
}

/** 任务 / 检查框 */
export function IconCheckSquare({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest)}>
      <polyline points="9 11 12 14 22 4" />
      <path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11" />
    </svg>
  );
}

/** 用户 */
export function IconUser({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest)}>
      <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" />
      <circle cx="12" cy="7" r="4" />
    </svg>
  );
}

/** 用户组 */
export function IconUsers({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest)}>
      <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2" />
      <circle cx="9" cy="7" r="4" />
      <path d="M23 21v-2a4 4 0 0 0-3-3.87" />
      <path d="M16 3.13a4 4 0 0 1 0 7.75" />
    </svg>
  );
}

/** 数据库 / 知识库 */
export function IconDatabase({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest)}>
      <ellipse cx="12" cy="5" rx="9" ry="3" />
      <path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3" />
      <path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5" />
    </svg>
  );
}

/** 模型 / cpu */
export function IconCpu({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest)}>
      <rect x="4" y="4" width="16" height="16" rx="2" ry="2" />
      <rect x="9" y="9" width="6" height="6" />
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
    <svg {...baseProps(size, color, strokeWidth, rest)}>
      <rect x="3" y="3" width="7" height="7" />
      <rect x="14" y="3" width="7" height="7" />
      <rect x="14" y="14" width="7" height="7" />
      <rect x="3" y="14" width="7" height="7" />
    </svg>
  );
}

/** 产物 / 文件 */
export function IconFileText({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest)}>
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
      <polyline points="14 2 14 8 20 8" />
      <line x1="16" y1="13" x2="8" y2="13" />
      <line x1="16" y1="17" x2="8" y2="17" />
      <polyline points="10 9 9 9 8 9" />
    </svg>
  );
}

/** 闪电 / 技能 */
export function IconZap({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest)}>
      <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" />
    </svg>
  );
}

/** 魔方 / MCP */
export function IconBox({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest)}>
      <path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z" />
      <polyline points="3.27 6.96 12 12.01 20.73 6.96" />
      <line x1="12" y1="22.08" x2="12" y2="12" />
    </svg>
  );
}

/** 头脑 / 规则 */
export function IconBookOpen({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest)}>
      <path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z" />
      <path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z" />
    </svg>
  );
}

/** 记忆 / 大脑 */
export function IconBrain({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest)}>
      <path d="M9.5 2A2.5 2.5 0 0 1 12 4.5v15a2.5 2.5 0 0 1-4.96.44 2.5 2.5 0 0 1-2.96-3.08 3 3 0 0 1-.34-5.58 2.5 2.5 0 0 1 1.32-4.24 2.5 2.5 0 0 1 1.98-3A2.5 2.5 0 0 1 9.5 2z" />
      <path d="M14.5 2A2.5 2.5 0 0 0 12 4.5v15a2.5 2.5 0 0 0 4.96.44 2.5 2.5 0 0 0 2.96-3.08 3 3 0 0 0 .34-5.58 2.5 2.5 0 0 0-1.32-4.24 2.5 2.5 0 0 0-1.98-3A2.5 2.5 0 0 0 14.5 2z" />
    </svg>
  );
}

/** 终端 / 命令 */
export function IconTerminal({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest)}>
      <polyline points="4 17 10 11 4 5" />
      <line x1="12" y1="19" x2="20" y2="19" />
    </svg>
  );
}

/** 关闭 × */
export function IconX({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest)}>
      <line x1="18" y1="6" x2="6" y2="18" />
      <line x1="6" y1="6" x2="18" y2="18" />
    </svg>
  );
}

/** 停止(方块) */
export function IconStop({ size = 14, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest)} fill={color}>
      <rect x="6" y="6" width="12" height="12" rx="1" />
    </svg>
  );
}

/** 减号(最小化) */
export function IconMinus({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest)}>
      <line x1="5" y1="12" x2="19" y2="12" />
    </svg>
  );
}

/** 方框(最大化) */
export function IconSquare({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest)}>
      <rect x="3" y="3" width="18" height="18" rx="2" ry="2" />
    </svg>
  );
}

/** 复制 / 拷 */
export function IconCopy({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest)}>
      <rect x="9" y="9" width="13" height="13" rx="2" ry="2" />
      <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
    </svg>
  );
}

/** 刷新 */
export function IconRefresh({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest)}>
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
    <svg {...baseProps(size, color, strokeWidth, rest)}>
      <circle cx="12" cy="12" r="1" />
      <circle cx="19" cy="12" r="1" />
      <circle cx="5" cy="12" r="1" />
    </svg>
  );
}

/** 麦克风 */
export function IconMic({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest)}>
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
    <svg {...baseProps(size, color, strokeWidth, rest)}>
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
    <svg {...baseProps(size, color, strokeWidth, rest)}>
      <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
    </svg>
  );
}

/** 搜索 */
export function IconSearch({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest)}>
      <circle cx="11" cy="11" r="8" />
      <line x1="21" y1="21" x2="16.65" y2="16.65" />
    </svg>
  );
}

/** 全屏 / 最大化窗口 */
export function IconMaximize({ size = 14, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest)}>
      <polyline points="15 3 21 3 21 9" />
      <polyline points="9 21 3 21 3 15" />
      <line x1="21" y1="3" x2="14" y2="10" />
      <line x1="3" y1="21" x2="10" y2="14" />
    </svg>
  );
}

/** 日历 / 安排 */
export function IconCalendar({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest)}>
      <rect x="3" y="4" width="18" height="18" rx="2" ry="2" />
      <line x1="16" y1="2" x2="16" y2="6" />
      <line x1="8" y1="2" x2="8" y2="6" />
      <line x1="3" y1="10" x2="21" y2="10" />
    </svg>
  );
}

/** 信息 */
export function IconInfo({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest)}>
      <circle cx="12" cy="12" r="10" />
      <line x1="12" y1="16" x2="12" y2="12" />
      <line x1="12" y1="8" x2="12.01" y2="8" />
    </svg>
  );
}

/** 警告 */
export function IconAlertTriangle({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest)}>
      <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
      <line x1="12" y1="9" x2="12" y2="13" />
      <line x1="12" y1="17" x2="12.01" y2="17" />
    </svg>
  );
}

/** 加载 spinner */
export function IconSpinner({ size = 18, color = "currentColor", strokeWidth = 2, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest)} className={`spinning ${rest.className || ""}`}>
      <path d="M21 12a9 9 0 1 1-6.219-8.56" />
    </svg>
  );
}

/** 附件(回形针) */
export function IconPaperclip({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest)}>
      <path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48" />
    </svg>
  );
}

/** @提及 */
export function IconAt({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest)}>
      <circle cx="12" cy="12" r="4" />
      <path d="M16 8v5a3 3 0 0 0 6 0v-1a10 10 0 1 0-4 8" />
    </svg>
  );
}

/** 工具 */
export function IconTool({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest)}>
      <path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z" />
    </svg>
  );
}

/** 全局(地球) */
export function IconGlobe({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest)}>
      <circle cx="12" cy="12" r="10" />
      <line x1="2" y1="12" x2="22" y2="12" />
      <path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z" />
    </svg>
  );
}

/** 工作 / 团队 */
export function IconBriefcase({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest)}>
      <rect x="2" y="7" width="20" height="14" rx="2" ry="2" />
      <path d="M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16" />
    </svg>
  );
}

/** 对勾 */
export function IconCheck({ size = 18, color = "currentColor", strokeWidth = 2, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest)}>
      <polyline points="20 6 9 17 4 12" />
    </svg>
  );
}

/** 拖拽(六点) */
export function IconGripVertical({ size = 14, color = "currentColor", strokeWidth = 2, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest)}>
      <circle cx="9" cy="6" r="1" />
      <circle cx="9" cy="12" r="1" />
      <circle cx="9" cy="18" r="1" />
      <circle cx="15" cy="6" r="1" />
      <circle cx="15" cy="12" r="1" />
      <circle cx="15" cy="18" r="1" />
    </svg>
  );
}

/** 文件读取(打开) */
export function IconFileRead({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest)}>
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
      <polyline points="14 2 14 8 20 8" />
      <path d="M9 13l2 2 4-4" />
    </svg>
  );
}

/** 文件写入(铅笔) */
export function IconFileWrite({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest)}>
      <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" />
      <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z" />
    </svg>
  );
}

/** 包/包装 */
export function IconPackage({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest)}>
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
    <svg {...baseProps(size, color, strokeWidth, rest)}>
      <path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2" />
      <rect x="8" y="2" width="8" height="4" rx="1" ry="1" />
    </svg>
  );
}

/** 文件差异 */
export function IconDiff({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest)}>
      <polyline points="22 12 18 12 15 21 9 3 6 12 2 12" />
    </svg>
  );
}

/** 烧瓶 / 测试 */
export function IconFlask({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest)}>
      <path d="M9 2h6" />
      <path d="M10 2v6L4 18a2 2 0 0 0 1.7 3h12.6A2 2 0 0 0 20 18L14 8V2" />
      <line x1="8" y1="14" x2="16" y2="14" />
    </svg>
  );
}

/** Git 分支 */
export function IconGitBranch({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest)}>
      <line x1="6" y1="3" x2="6" y2="15" />
      <circle cx="18" cy="6" r="3" />
      <circle cx="6" cy="18" r="3" />
      <path d="M18 9a9 9 0 0 1-9 9" />
    </svg>
  );
}

/** 左箭头(返回) */
export function IconArrowLeft({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest)}>
      <line x1="19" y1="12" x2="5" y2="12" />
      <polyline points="12 19 5 12 12 5" />
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
    <svg {...baseProps(size, color, strokeWidth, rest)}>
      <path d="M9 2v6" />
      <path d="M15 2v6" />
      <path d="M6 8h12v3a6 6 0 0 1-12 0V8z" />
      <path d="M12 17v5" />
    </svg>
  );
}

/** Markdown 标识（M 字母徽章） */
export function IconMarkdown({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest)}>
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
    <svg {...baseProps(size, color, strokeWidth, rest)}>
      <path d="M3 12a9 9 0 1 0 3-6.7L3 8" />
      <path d="M3 3v5h5" />
    </svg>
  );
}

/** 上箭头（chevron up） */
export function IconChevronUp({ size = 14, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest)}>
      <path d="M18 15l-6-6-6 6" />
    </svg>
  );
}

/** 统一折叠箭头：展开态朝下，折叠态朝右，带 90° 旋转过渡 */
export function IconArrowToggle({ open, size = 14, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps & { open: boolean }) {
  return (
    <svg
      {...baseProps(size, color, strokeWidth, rest)}
      style={{
        transform: open ? "rotate(90deg)" : "rotate(0deg)",
        transition: "transform var(--dur-fast, 0.15s) ease",
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
    <svg {...baseProps(size, color, strokeWidth, rest)}>
      <path d="M4 12h16M14 6l6 6-6 6" />
    </svg>
  );
}

/** 时钟 */
export function IconClock({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest)}>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 7v5l3 2" />
    </svg>
  );
}

/** 图钉（置顶排序） */
export function IconPin({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest)}>
      <path d="M12 17v5" />
      <path d="M9 10.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24V16a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1v-.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 11 10.76V7a1 1 0 0 1 1-1 2 2 0 0 0 0-4H8a2 2 0 0 0 0 4 1 1 0 0 1 1 1z" />
    </svg>
  );
}

/** 字母排序（AZ） */
export function IconSortAlpha({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest)}>
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
    <svg {...baseProps(size, color, strokeWidth, rest)}>
      <circle cx="12" cy="12" r="8" />
      <circle cx="12" cy="12" r="2" />
      <path d="M12 2v4M12 18v4M2 12h4M18 12h4" />
    </svg>
  );
}

/** 打开的文件夹 */
export function IconFolderOpen({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest)}>
      <path d="M3 6a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v1H3z" />
      <path d="M3 10h18l-2 8a2 2 0 0 1-2 1.5H7A2 2 0 0 1 5 18z" />
    </svg>
  );
}

/** 点赞（大拇指向上） */
export function IconThumbsUp({ size = 16, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest)}>
      <path d="M14 9V5a3 3 0 0 0-3-3l-4 9v11h11.28a2 2 0 0 0 2-1.7l1.38-9a2 2 0 0 0-2-2.3zM7 22H4a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2h3" />
    </svg>
  );
}

/** 踩（大拇指向下） */
export function IconThumbsDown({ size = 16, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest)}>
      <path d="M10 15v4a3 3 0 0 0 3 3l4-9V2H5.72a2 2 0 0 0-2 1.7l-1.38 9a2 2 0 0 0 2 2.3zM17 2h3a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2h-3" />
    </svg>
  );
}

/** 外部链接（右上箭头） */
export function IconExternalLink({ size = 16, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest)}>
      <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
      <polyline points="15 3 21 3 21 9" />
      <line x1="10" y1="14" x2="21" y2="3" />
    </svg>
  );
}

/** 暂停（竖线） */
export function IconPause({ size = 16, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest)}>
      <rect x="6" y="4" width="4" height="16" rx="1" />
      <rect x="14" y="4" width="4" height="16" rx="1" />
    </svg>
  );
}

/** 图片（风景） */
export function IconImage({ size = 16, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest)}>
      <rect x="3" y="3" width="18" height="18" rx="2" />
      <circle cx="8.5" cy="8.5" r="1.5" />
      <polyline points="21 15 16 10 5 21" />
    </svg>
  );
}

/** 扳手（技能/工具） */
export function IconWrench({ size = 16, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest)}>
      <path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z" />
    </svg>
  );
}