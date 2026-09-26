/** 宠物窗口图标集（plan-73-341）：改用 Lucide 官方图标，不再自绘。
 *
 * 图标来源：Lucide v1.48.0（ISC 许可）—— https://unpkg.com/lucide-static/icons/<name>.svg
 * 未修改任何官方 path 数据，仅统一外层尺寸/描边属性（尺寸随调用方传入）。
 *
 * 为什么不自绘：此前自绘图标线条风格不统一，观感粗糙；Lucide 是单一设计系统
 * （24×24 视口、`stroke-width 2`、圆角端点），且与主窗既有图标同源，不会产生风格割裂。
 * 内联而非引依赖：宠物页是独立 chunk，内联 21 个图标约 9KB，比引入组件库更小更可控。
 */
interface IconProps {
  size?: number;
  className?: string;
}

/** 统一外壳：24 视口 + 描边渲染（与 Lucide 官方一致） */
const base = (size: number | undefined, className?: string) => ({
  width: size ?? 14,
  height: size ?? 14,
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 2,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
  "aria-hidden": true,
  className,
});

/* ─────────────────────────── 操作类 ─────────────────────────── */

/** 回到会话（lucide: message-square） */
export function IconGoSession({ size, className }: IconProps) {
  return (
    <svg {...base(size, className)}>
      <path d="M22 17a2 2 0 0 1-2 2H6.828a2 2 0 0 0-1.414.586l-2.202 2.202A.71.71 0 0 1 2 21.286V5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2z" />
    </svg>
  );
}

/** 停止本次运行（lucide: square） */
export function IconStopRun({ size, className }: IconProps) {
  return (
    <svg {...base(size, className)} fill="currentColor" stroke="none">
      <rect width="18" height="18" x="3" y="3" rx="2" />
    </svg>
  );
}

/** 折叠 / 展开浮窗（lucide: chevron-down；展开时由 CSS 旋转 180°） */
export function IconChevron({ size, className }: IconProps) {
  return (
    <svg {...base(size, className)}>
      <path d="m6 9 6 6 6-6" />
    </svg>
  );
}

/** 拖拽调序手柄（lucide: grip-horizontal，六个点） */
export function IconGrip({ size, className }: IconProps) {
  return (
    <svg {...base(size, className)} fill="currentColor" stroke="none">
      <circle cx="12" cy="9" r="1" />
      <circle cx="19" cy="9" r="1" />
      <circle cx="5" cy="9" r="1" />
      <circle cx="12" cy="15" r="1" />
      <circle cx="19" cy="15" r="1" />
      <circle cx="5" cy="15" r="1" />
    </svg>
  );
}

/** 拖拽缩放（lucide: move-diagonal-2，箭头由**左上指向右下**） */
export function IconResize({ size, className }: IconProps) {
  return (
    <svg {...base(size, className)}>
      <path d="M19 13v6h-6" />
      <path d="M5 11V5h6" />
      <path d="m5 5 14 14" />
    </svg>
  );
}

/** 等待处理（lucide: circle-help，问号角标） */
export function IconAsk({ size, className }: IconProps) {
  return (
    <svg {...base(size, className)}>
      <circle cx="12" cy="12" r="10" />
      <path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3" />
      <path d="M12 17h.01" />
    </svg>
  );
}

/** 右键菜单：隐藏宠物（lucide: eye-off） */
export function IconHide({ size, className }: IconProps) {
  return (
    <svg {...base(size, className)}>
      <path d="M10.733 5.076a10.744 10.744 0 0 1 11.205 6.575 1 1 0 0 1 0 .696 10.747 10.747 0 0 1-1.444 2.49" />
      <path d="M14.084 14.158a3 3 0 0 1-4.242-4.242" />
      <path d="M17.479 17.499a10.75 10.75 0 0 1-15.417-5.151 1 1 0 0 1 0-.696 10.75 10.75 0 0 1 4.446-5.143" />
      <path d="m2 2 20 20" />
    </svg>
  );
}

/** 右键菜单：打开设置（lucide: settings） */
export function IconSettings({ size, className }: IconProps) {
  return (
    <svg {...base(size, className)}>
      <path d="M9.671 4.136a2.34 2.34 0 0 1 4.659 0 2.34 2.34 0 0 0 3.319 1.915 2.34 2.34 0 0 1 2.33 4.033 2.34 2.34 0 0 0 0 3.831 2.34 2.34 0 0 1-2.33 4.033 2.34 2.34 0 0 0-3.319 1.915 2.34 2.34 0 0 1-4.659 0 2.34 2.34 0 0 0-3.32-1.915 2.34 2.34 0 0 1-2.33-4.033 2.34 2.34 0 0 0 0-3.831A2.34 2.34 0 0 1 6.35 6.051a2.34 2.34 0 0 0 3.319-1.915" />
      <circle cx="12" cy="12" r="3" />
    </svg>
  );
}

/* ─────────────────────────── 活动类型 ─────────────────────────── */

/** 活动类型（决定浮窗第二行图标与配色） */
export type ActivityKind =
  | "thinking"
  | "message"
  | "read"
  | "edit"
  | "run"
  | "search"
  | "web"
  | "folder"
  | "tree"
  | "other"
  | "done"
  | "fail";

/** 活动图标：按类型分派（全部 Lucide 官方 path） */
export function IconActivity({ kind, size, className }: IconProps & { kind: ActivityKind }) {
  const s = size ?? 13;
  switch (kind) {
    case "thinking": // lucide: sparkles
      return (
        <svg {...base(s, className)}>
          <path d="M11.017 2.814a1 1 0 0 1 1.966 0l1.051 5.558a2 2 0 0 0 1.594 1.594l5.558 1.051a1 1 0 0 1 0 1.966l-5.558 1.051a2 2 0 0 0-1.594 1.594l-1.051 5.558a1 1 0 0 1-1.966 0l-1.051-5.558a2 2 0 0 0-1.594-1.594l-5.558-1.051a1 1 0 0 1 0-1.966l5.558-1.051a2 2 0 0 0 1.594-1.594z" />
          <path d="M20 2v4" />
          <path d="M22 4h-4" />
          <circle cx="4" cy="20" r="2" />
        </svg>
      );
    case "message": // lucide: message-circle
      return (
        <svg {...base(s, className)}>
          <path d="M2.992 16.342a2 2 0 0 1 .094 1.167l-1.065 3.29a1 1 0 0 0 1.236 1.168l3.413-.998a2 2 0 0 1 1.099.092 10 10 0 1 0-4.777-4.719" />
        </svg>
      );
    case "read": // lucide: file-text
      return (
        <svg {...base(s, className)}>
          <path d="M6 22a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h8a2.4 2.4 0 0 1 1.704.706l3.588 3.588A2.4 2.4 0 0 1 20 8v12a2 2 0 0 1-2 2z" />
          <path d="M14 2v5a1 1 0 0 0 1 1h5" />
          <path d="M10 9H8" />
          <path d="M16 13H8" />
          <path d="M16 17H8" />
        </svg>
      );
    case "edit": // lucide: file-pen
      return (
        <svg {...base(s, className)}>
          <path d="M12.659 22H18a2 2 0 0 0 2-2V8a2.4 2.4 0 0 0-.706-1.706l-3.588-3.588A2.4 2.4 0 0 0 14 2H6a2 2 0 0 0-2 2v9.34" />
          <path d="M14 2v5a1 1 0 0 0 1 1h5" />
          <path d="M10.378 12.622a1 1 0 0 1 3 3.003L8.36 20.637a2 2 0 0 1-.854.506l-2.867.837a.5.5 0 0 1-.62-.62l.836-2.869a2 2 0 0 1 .506-.853z" />
        </svg>
      );
    case "run": // lucide: square-terminal
      return (
        <svg {...base(s, className)}>
          <path d="m7 11 2-2-2-2" />
          <path d="M11 13h4" />
          <rect width="18" height="18" x="3" y="3" rx="2" ry="2" />
        </svg>
      );
    case "search": // lucide: search
      return (
        <svg {...base(s, className)}>
          <path d="m21 21-4.34-4.34" />
          <circle cx="11" cy="11" r="8" />
        </svg>
      );
    case "web": // lucide: globe
      return (
        <svg {...base(s, className)}>
          <circle cx="12" cy="12" r="10" />
          <path d="M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20" />
          <path d="M2 12h20" />
        </svg>
      );
    case "folder": // lucide: folder-open
      return (
        <svg {...base(s, className)}>
          <path d="m6 14 1.5-2.9A2 2 0 0 1 9.24 10H20a2 2 0 0 1 1.94 2.5l-1.54 6a2 2 0 0 1-1.95 1.5H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h3.9a2 2 0 0 1 1.69.9l.81 1.2a2 2 0 0 0 1.67.9H18a2 2 0 0 1 2 2v2" />
        </svg>
      );
    case "tree": // lucide: list-tree
      return (
        <svg {...base(s, className)}>
          <path d="M8 5h13" />
          <path d="M13 12h8" />
          <path d="M13 19h8" />
          <path d="M3 10a2 2 0 0 0 2 2h3" />
          <path d="M3 5v12a2 2 0 0 0 2 2h3" />
        </svg>
      );
    case "done": // lucide: check
      return (
        <svg {...base(s, className)}>
          <path d="M20 6 9 17l-5-5" />
        </svg>
      );
    case "fail": // lucide: circle-alert
      return (
        <svg {...base(s, className)}>
          <circle cx="12" cy="12" r="10" />
          <line x1="12" x2="12" y1="8" y2="12" />
          <line x1="12" x2="12.01" y1="16" y2="16" />
        </svg>
      );
    default: // lucide: wrench
      return (
        <svg {...base(s, className)}>
          <path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.106-3.105c.32-.322.863-.22.983.218a6 6 0 0 1-8.259 7.057l-7.91 7.91a1 1 0 0 1-2.999-3l7.91-7.91a6 6 0 0 1 7.057-8.259c.438.12.54.662.219.984z" />
        </svg>
      );
  }
}
