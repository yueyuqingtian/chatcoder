/**
 * 项目图标统一出口（plan-630-2590）。
 *
 * 历史：本文件曾是 1864 行的手绘 24 网格线条图标库（Feather / Lucide 风格，
 * `stroke` 描边 + 按尺寸换算光学线宽 + 内部路径 class 驱动的三态微动效）。
 *
 * 现状：图形统一改为 Ant Design 图标（`@ant-design/icons-svg` 的 1024 网格数据），
 * 由 `antd-icons/AntIcon.tsx` 渲染。**本文件的导出名、props 签名与默认 size 全部不变**，
 * 所以 100+ 个业务模块、650+ 处调用点无需改动一行代码。
 *
 * 为什么要保留下面这些「保留自绘」项（不要顺手换成 antd 图标）：
 *  - `IconRingProgress`：按 value 运行时算 strokeDasharray 画进度弧，是数据可视件；
 *  - `IconBrand*`：Windows 资源管理器 / VS Code / IDEA / Windows 终端的品牌标识，
 *    antd 没有对应图形，换掉会丢失识别度；
 *  - `IconPanelLeft/Right`：表达「左右面板开合」，antd 无等价语义（LayoutOutlined 是通用布局）；
 *  - `IconToneBadge`：实心圆底 + 反白符号的语气徽标，是容器不是单图标。
 *
 * 动效说明（plan-630-2590 D3）：`.ic-fill` 双色填充淡入、`.ic-ac` 强调色点亮、
 * 内部部件微动（`.ic-hand` 等）对 antd 图标不生效，已按用户决策接受降级；
 * 整枚旋转（`.ic-settings` / `.ic-refresh` / `.ic-rotate-ccw` / `.ic-globe`）、
 * `IconChevron` 与 `IconArrowToggle` 的 JS 旋转、`IconSpinner` 的持续旋转仍然生效——
 * 这些依赖的是**外层 svg 的类名 / inline style**，与内部路径结构无关。
 */

import type { CSSProperties, ReactNode, SVGProps } from "react";
import { AntIcon } from "./antd-icons/AntIcon";
import { GLYPHS, type GlyphKey } from "./antd-icons/registry";

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
 * 仍被 ModeGlyphs / StackIcons / ToolGlyphs 三个 iconkit 文件引用（它们保留自绘），
 * 所以即使主图标库改用 antd 也必须继续导出。
 *
 * 固定 1.75 单位时，13~14px 渲染的实际线宽只有约 1px，叠加半像素坐标后发虚发灰。 */
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

/** 保留自绘项共用的 svg 基础属性（24 网格 + 光学线宽 + `ic ic-<名称>` 类名） */
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

/** 解析实际线宽（24 网格单位）：未显式指定时走光学线宽，与 baseProps 同一口径 */
const resolveStroke = (size: IconSize, strokeWidth: number): number =>
  strokeWidth === DEFAULT_STROKE ? opticalStroke(resolveSize(size)) : strokeWidth;

/** 自绘图形围绕 24 网格中心缩放（plan-630-2590 返工）。
 *
 * 为什么要这个：antd 图标经 `opticalScale` 已把实际绘制跨度归一到「外框的 89.3%」，
 * 而自绘图标的坐标是各自手写的，从未参与归一。实测发现这些图标偏离目标：
 *   · IconArchive / IconPanelLeft / IconPanelRight：75%
 *   · IconShield（antd 的 safetyCertificate）：97.6%，偏大
 * 与 antd 图标并排时，这些就是用户反复反馈的「大小不一致」。
 *
 * **折叠/展开箭头（Chevron 系与 ArrowToggle）刻意不参与归一**：用户要求沿用 Git 之前的形态，
 * 它们只占 24 网格的 50%，就是原本的观感，不要顺手给它们加 OptScale 放大。
 *
 * 用 transform 而不是改坐标：形状原样保留，只改视觉跨度，和 antd 侧的做法完全一致。 */
const centerScale = (s: number) => `translate(12 12) scale(${s}) translate(-12 -12)`;

/** 自绘图标的光学放大容器。
 *
 * 必须走这个组件、不要直接写 `<g transform={centerScale(...)}>`：SVG 的 transform 会把
 * **描边一起放大**，1.4 倍的箭头线宽也会变 1.4 倍，比同排其它图标粗一圈——那是把
 * 「尺寸不一」换成了「粗细不一」，同样刺眼。这里按缩放比例把 strokeWidth 回缩，
 * 保证放大后的线宽与未缩放图标完全相同。 */
function OptScale({ scale, size, strokeWidth, children }: {
  scale: number;
  size: IconSize;
  strokeWidth: number;
  children: ReactNode;
}) {
  return (
    <g transform={centerScale(scale)} strokeWidth={resolveStroke(size, strokeWidth) / scale}>
      {children}
    </g>
  );
}

/* ── antd 图标桥接 ─────────────────────────────────────────────
 *
 * defineIcon 的作用：把一个 antd 图标包成项目原有的调用签名
 * `<Icon size={n} className=... />`。参数逐项都有理由：
 *  - displayName：React DevTools 里能看出是哪个业务图标，不是一堆匿名组件；
 *  - key：registry 里的图标键（保证只 import 用到的图标，tree-shaking 生效）；
 *  - cssName：`ic-<cssName>` 类名，styles/icons.css 的整枚旋转动效按它匹配；
 *  - defaultSize：**必须与原手绘实现逐个对齐**，否则界面尺寸会整体偏移。
 */
function defineIcon(displayName: string, key: GlyphKey, cssName: string, defaultSize: number) {
  const Icon = ({ size = defaultSize, color = "currentColor", strokeWidth, className, ...rest }: IconProps) => {
    void strokeWidth; // antd 图标的线宽写死在路径里、无法通过属性调整；保留入参只为不破坏调用方签名
    return <AntIcon glyph={GLYPHS[key]} glyphKey={key} name={cssName} size={size} color={color} className={className} {...rest} />;
  };
  Icon.displayName = displayName;
  return Icon;
}

/* 18px 常规档：界面里最常用的一档 */
export const IconAlertCircle = defineIcon("IconAlertCircle", "exclamationCircle", "alert-circle", 18);
export const IconAlertTriangle = defineIcon("IconAlertTriangle", "warning", "alert-triangle", 18);
export const IconAnchor = defineIcon("IconAnchor", "link", "anchor", 18);
export const IconAppWindow = defineIcon("IconAppWindow", "appstore", "app-window", 18);

/** 归档：保留自绘（用户要求沿用原图标）。
 *  为什么不用 antd 的 InboxOutlined：那是「收件箱」语义（开口托盘），
 *  而这里的语义是「装箱封存」——带盖子的一条横梁 + 箱体 + 中间抽手，一眼可辨。
 *  盖子是聚焦态微动部件（.ic-lid 绕左端抬起）。 */
export function IconArchive({ size = 18, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "archive")}>
      <OptScale scale={1.19} size={size} strokeWidth={strokeWidth}>
      <path className="ic-fill" d="M5 8v11a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8" />
      <rect className="ic-lid" x="3" y="4" width="18" height="4" rx="1.6" />
      <path d="M5 8v11a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8" />
      <line x1="9" y1="12" x2="15" y2="12" />
      </OptScale>
    </svg>
  );
}

export const IconArrowDown = defineIcon("IconArrowDown", "arrowDown", "arrow-down", 18);
export const IconArrowLeft = defineIcon("IconArrowLeft", "arrowLeft", "arrow-left", 18);
export const IconArrowRight = defineIcon("IconArrowRight", "arrowRight", "arrow-right", 18);
export const IconArrowUp = defineIcon("IconArrowUp", "arrowUp", "arrow-up", 18);
export const IconArrowUpRight = defineIcon("IconArrowUpRight", "export", "arrow-up-right", 18);
export const IconAt = defineIcon("IconAt", "mail", "at", 18);
export const IconBarChart = defineIcon("IconBarChart", "barChart", "bar-chart", 18);
export const IconBlocks = defineIcon("IconBlocks", "appstore", "blocks", 18);
export const IconBookOpen = defineIcon("IconBookOpen", "read", "book-open", 18);
export const IconBot = defineIcon("IconBot", "robot", "bot", 18);
export const IconBox = defineIcon("IconBox", "codeSandbox", "box", 18);
export const IconBrain = defineIcon("IconBrain", "bulb", "brain", 18);
export const IconBriefcase = defineIcon("IconBriefcase", "solution", "briefcase", 18);
export const IconBug = defineIcon("IconBug", "bug", "bug", 18);
export const IconCalendar = defineIcon("IconCalendar", "calendar", "calendar", 18);
export const IconCamera = defineIcon("IconCamera", "camera", "camera", 18);
export const IconCheck = defineIcon("IconCheck", "check", "check", 18);
export const IconChecklist = defineIcon("IconChecklist", "checkSquare", "checklist", 18);
export const IconCheckSquare = defineIcon("IconCheckSquare", "checkSquare", "check-square", 18);
/* ── 折叠展开箭头：保留自绘（用户要求沿用原图标） ──────────────────
 *
 * 为什么不用 antd：这几个是全应用最高频的「层级操作」符号（列表展开、面板折叠、
 * 文件树、下拉胶囊），原自绘是 24 网格 2 单位光学线宽的细箭头，圆端点；
 * 换 antd 后变成更粗更短的实心感箭头，与周边细线图标同屏时明显不协调，
 * 且这类符号的形态本身就是用户的操作肌肉记忆，不宜更换。
 *
 * 尺寸也不做 antd 侧的光学归一（不加 OptScale）：按用户要求沿用 Git 之前的原尺寸与线宽。
 *
 * 旋转协议不变：`IconChevron` / `IconArrowToggle` 仍靠外层 svg 的 inline transform
 * 做连续旋转（不是切换两个图标），所以展开/折叠依然是过渡动画。 */

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

/** 上箭头 */
export function IconChevronUp({ size = 14, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "chevron-up")}>
      <path className="ic-up" d="M18 15l-6-6-6 6" />
    </svg>
  );
}
/* plan-630-2590 返工：`IconClipboard` 已移除。
 *
 * 它原先映射到 antd 的 `snippets`，而 `snippets` 的图形是「两块重叠的纸」= 复制图标，
 * 画在计划卡左边会被读成「复制」（用户反馈）。antd 没有剪贴板图形，
 * 而两处调用（计划卡、Git 站会摘要）的语义都是「文档 / 摘要」，
 * 所以统一改用 `IconFileText`（带折角的文档），不再保留这个误导性的导出。 */
export const IconClock = defineIcon("IconClock", "clockCircle", "clock", 18);
export const IconCode = defineIcon("IconCode", "code", "code", 18);
export const IconCompress = defineIcon("IconCompress", "shrink", "compress", 18);
export const IconCopy = defineIcon("IconCopy", "copy", "copy", 18);
export const IconCornerDownLeft = defineIcon("IconCornerDownLeft", "enter", "corner-down-left", 18);
export const IconCpu = defineIcon("IconCpu", "deploymentUnit", "cpu", 18);
export const IconDatabase = defineIcon("IconDatabase", "database", "database", 18);
export const IconDiff = defineIcon("IconDiff", "diff", "diff", 18);
export const IconDownload = defineIcon("IconDownload", "download", "download", 18);
export const IconExpandDiagonal = defineIcon("IconExpandDiagonal", "expandAlt", "expand-diagonal", 18);
export const IconEye = defineIcon("IconEye", "eye", "eye", 18);
export const IconEyeOff = defineIcon("IconEyeOff", "eyeInvisible", "eye-off", 18);
export const IconFileDiff = defineIcon("IconFileDiff", "diff", "file-diff", 18);
export const IconFilePatch = defineIcon("IconFilePatch", "fileText", "file-patch", 18);
export const IconFileRead = defineIcon("IconFileRead", "fileText", "file-read", 18);
export const IconFileSearch = defineIcon("IconFileSearch", "fileSearch", "file-search", 18);
export const IconFileStack = defineIcon("IconFileStack", "copy", "file-stack", 18);
export const IconFileText = defineIcon("IconFileText", "fileText", "file-text", 18);
export const IconFileWrite = defineIcon("IconFileWrite", "fileText", "file-write", 18);
export const IconFlame = defineIcon("IconFlame", "fire", "flame", 18);
export const IconFlask = defineIcon("IconFlask", "experiment", "flask", 18);
export const IconFolder = defineIcon("IconFolder", "folder", "folder", 18);
export const IconFolderOpen = defineIcon("IconFolderOpen", "folderOpen", "folder-open", 18);
export const IconFolderPlus = defineIcon("IconFolderPlus", "folderAdd", "folder-plus", 18);
export const IconFolderTree = defineIcon("IconFolderTree", "apartment", "folder-tree", 18);
export const IconGauge = defineIcon("IconGauge", "dashboard", "gauge", 18);
export const IconGitBranch = defineIcon("IconGitBranch", "branches", "git-branch", 18);
export const IconGitCommit = defineIcon("IconGitCommit", "nodeIndex", "git-commit", 18);
export const IconGitGraph = defineIcon("IconGitGraph", "partition", "git-graph", 18);
export const IconGlobe = defineIcon("IconGlobe", "global", "globe", 18);
export const IconHistory = defineIcon("IconHistory", "history", "history", 18);
export const IconInfo = defineIcon("IconInfo", "infoCircle", "info", 18);
export const IconLayers = defineIcon("IconLayers", "block", "layers", 18);
export const IconLayoutGrid = defineIcon("IconLayoutGrid", "appstore", "layout-grid", 18);
export const IconListChecks = defineIcon("IconListChecks", "checkSquare", "list-checks", 18);
export const IconListFilter = defineIcon("IconListFilter", "filter", "list-filter", 18);
export const IconListOrdered = defineIcon("IconListOrdered", "orderedList", "list-ordered", 18);
export const IconListTodo = defineIcon("IconListTodo", "unorderedList", "list-todo", 18);
export const IconLock = defineIcon("IconLock", "lock", "lock", 18);
export const IconMarkdown = defineIcon("IconMarkdown", "fileMarkdown", "markdown", 18);
export const IconMessageSquare = defineIcon("IconMessageSquare", "message", "message-square", 18);
export const IconMic = defineIcon("IconMic", "audio", "mic", 18);
export const IconMinus = defineIcon("IconMinus", "minus", "minus", 18);
export const IconMonitor = defineIcon("IconMonitor", "desktop", "monitor", 18);
export const IconMoon = defineIcon("IconMoon", "moon", "moon", 18);
export const IconMoreHorizontal = defineIcon("IconMoreHorizontal", "more", "more-horizontal", 18);
export const IconOperationCluster = defineIcon("IconOperationCluster", "cluster", "operation-cluster", 18);
export const IconPackage = defineIcon("IconPackage", "codeSandbox", "package", 18);
export const IconPalette = defineIcon("IconPalette", "bgColors", "palette", 18);
export const IconPaperclip = defineIcon("IconPaperclip", "paperClip", "paperclip", 18);
export const IconPencil = defineIcon("IconPencil", "edit", "pencil", 18);
export const IconPet = defineIcon("IconPet", "tag", "pet", 18);
export const IconPin = defineIcon("IconPin", "pushpin", "pin", 18);
export const IconPlay = defineIcon("IconPlay", "caretRight", "play", 18);
export const IconPlug = defineIcon("IconPlug", "api", "plug", 18);
export const IconPlus = defineIcon("IconPlus", "plus", "plus", 18);
export const IconPuzzle = defineIcon("IconPuzzle", "appstoreAdd", "puzzle", 18);
export const IconRefresh = defineIcon("IconRefresh", "reload", "refresh", 18);
export const IconRotateCcw = defineIcon("IconRotateCcw", "undo", "rotate-ccw", 18);
export const IconSearch = defineIcon("IconSearch", "search", "search", 18);
export const IconSettings = defineIcon("IconSettings", "setting", "settings", 18);
export const IconShield = defineIcon("IconShield", "safetyCertificate", "shield", 18);
export const IconSliders = defineIcon("IconSliders", "control", "sliders", 18);
export const IconSortAlpha = defineIcon("IconSortAlpha", "sortAscending", "sort-alpha", 18);
export const IconSortDesc = defineIcon("IconSortDesc", "sortDescending", "sort-desc", 18);
export const IconSparkles = defineIcon("IconSparkles", "highlight", "sparkles", 18);
export const IconSquare = defineIcon("IconSquare", "border", "square", 18);
export const IconSquareTerminal = defineIcon("IconSquareTerminal", "code", "square-terminal", 18);
export const IconSun = defineIcon("IconSun", "sun", "sun", 18);
export const IconTarget = defineIcon("IconTarget", "aim", "target", 18);
export const IconTelescope = defineIcon("IconTelescope", "read", "telescope", 18);
export const IconTerminal = defineIcon("IconTerminal", "code", "terminal", 18);
export const IconTool = defineIcon("IconTool", "tool", "tool", 18);
export const IconTrash = defineIcon("IconTrash", "delete", "trash", 18);
export const IconUser = defineIcon("IconUser", "user", "user", 18);
export const IconUsers = defineIcon("IconUsers", "team", "users", 18);
export const IconX = defineIcon("IconX", "close", "x", 18);
export const IconZap = defineIcon("IconZap", "thunderbolt", "zap", 18);

/* 14 / 16px 小档：通用尺寸比 18 更小，必须逐个保留原默认值 */
export const IconCheckCircle = defineIcon("IconCheckCircle", "checkCircle", "check-circle", 16);
export const IconExternalLink = defineIcon("IconExternalLink", "export", "external-link", 16);
export const IconGripVertical = defineIcon("IconGripVertical", "holder", "grip-vertical", 14);
export const IconImage = defineIcon("IconImage", "picture", "image", 16);
export const IconMaximize = defineIcon("IconMaximize", "fullscreen", "maximize", 14);
export const IconMinimize = defineIcon("IconMinimize", "fullscreenExit", "minimize", 14);
export const IconMultimodal = defineIcon("IconMultimodal", "bgColors", "multimodal", 14);
export const IconPause = defineIcon("IconPause", "pauseCircle", "pause", 16);
export const IconStop = defineIcon("IconStop", "border", "stop", 14);
export const IconThumbsDown = defineIcon("IconThumbsDown", "dislike", "thumbs-down", 16);
export const IconThumbsUp = defineIcon("IconThumbsUp", "like", "thumbs-up", 16);
export const IconWand = defineIcon("IconWand", "bulb", "wand", 16);
export const IconWrench = defineIcon("IconWrench", "tool", "wrench", 16);

/* ── 需要保留行为、不能纯桥接的图标 ───────────────────────────── */

/** 统一折叠/展开箭头：单一右向 chevron + CSS transform 旋转过渡（不是切换两个图标）。
 *
 * 展开/折叠是连续旋转动画，旋转挂在**外层 svg 的 inline style** 上，与内部路径结构无关。
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

/** 折叠箭头（展开态朝下）：与 IconChevron 同一套旋转协议，供旧调用点使用 */
export function IconArrowToggle({ open, size = 14, color = "currentColor", strokeWidth = 1.75, ...rest }: IconProps & { open: boolean }) {
  return (
    <svg
      {...baseProps(size, color, strokeWidth, rest, "arrow-toggle")}
      style={{
        transform: open ? "rotate(90deg)" : "rotate(0deg)",
        /* plan-353-1738 M4b（S5）：统一为折叠展开档时长 + 弹性曲线 */
        transition: "transform var(--dur-3) var(--ease-spring)",
        ...((rest as Record<string, unknown>).style as Record<string, unknown> | undefined),
      }}
    >
      <path d="M9 18l6-6-6-6" />
    </svg>
  );
}

/** 动态文件夹：按 open 在闭合 / 打开两个 antd 图标间切换 */
export function IconFolderDynamic({ open, size = 18, color = "currentColor", strokeWidth, ...rest }: IconProps & { open?: boolean }) {
  void strokeWidth;
  return (
    <AntIcon
      glyph={open ? GLYPHS.folderOpen : GLYPHS.folder}
      name={open ? "folder-open" : "folder"}
      size={size}
      color={color}
      {...rest}
    />
  );
}

/** 加载 spinner：antd 的 LoadingOutlined + 持续旋转（session-spin 与左侧会话运行指示同一条关键帧）。
 *
 * 旋转挂在 svg 自身而不是内部路径上，所以不再依赖被移除的 `.ic-spinner-arc` 部件。 */
export function IconSpinner({ size = 18, color = "currentColor", strokeWidth, className, style, ...rest }: IconProps) {
  void strokeWidth;
  return (
    <AntIcon
      glyph={GLYPHS.loading}
      name="spinner"
      size={size}
      color={color}
      className={className}
      style={{ animation: "session-spin var(--dur-spin) linear infinite", ...style }}
      {...rest}
    />
  );
}

/* ── 保留自绘（见文件头说明，不要替换为 antd 图标） ───────────── */

/** 齿轮外形：仍被 iconkit/StackIcons.tsx 引用，继续导出 */
export const GEAR_D =
  "M10.68 2.59A9.5 9.5 0 0 1 13.32 2.59L13.74 5.01A7.2 7.2 0 0 1 15.71 5.83L17.72 4.41A9.5 9.5 0 0 1 19.59 6.28L18.17 8.29A7.2 7.2 0 0 1 18.99 10.26L21.41 10.68A9.5 9.5 0 0 1 21.41 13.32L18.99 13.74A7.2 7.2 0 0 1 18.17 15.71L19.59 17.72A9.5 9.5 0 0 1 17.72 19.59L15.71 18.17A7.2 7.2 0 0 1 13.74 18.99L13.32 21.41A9.5 9.5 0 0 1 10.68 21.41L10.26 18.99A7.2 7.2 0 0 1 8.29 18.17L6.28 19.59A9.5 9.5 0 0 1 4.41 17.72L5.83 15.71A7.2 7.2 0 0 1 5.01 13.74L2.59 13.32A9.5 9.5 0 0 1 2.59 10.68L5.01 10.26A7.2 7.2 0 0 1 5.83 8.29L4.41 6.28A9.5 9.5 0 0 1 6.28 4.41L8.29 5.83A7.2 7.2 0 0 1 10.26 5.01L10.68 2.59Z";

/** 语气徽标（plan-20261025-shell 需求 9）：实心圆底 + 反白符号，用于报错卡与全局提示的行首。
 *  线框圆在浅红底上与文字争夺注意力、又显得单薄；实心徽标一眼可辨语气。
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

/** 环形进度（plan-282-1416 收编 ComposerCore 内联上下文占用环）。
 *  value: 0~1；trackColor/indicatorColor 允许调用方按语义着色。
 *  plan-282-1421（第10项）：loading 模式——压缩等"进行中"场景由圆环自身变成旋转加载弧。 */
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
        /* plan-128-518：加载态 = 左侧会话「执行中」同款转圈——细底环 + 1/4 周长实色弧，平头线帽、线性匀速 */
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

/** 左面板切换：表达「面板开合」，antd 无等价语义，保留自绘。
 *  面板打开时（data-open）左栏铺一层淡底；分隔线为聚焦态强调部件。 */
export function IconPanelLeft({ size = 18, color = "currentColor", strokeWidth = 1.75, open = false, ...rest }: IconProps & { open?: boolean }) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "panel-left")} data-open={open ? "true" : "false"}>
      <OptScale scale={1.19} size={size} strokeWidth={strokeWidth}>
        <path className="ic-fill ic-pane" d="M9 4H6.5A3.5 3.5 0 0 0 3 7.5v9A3.5 3.5 0 0 0 6.5 20H9z" />
        <rect x="3" y="4" width="18" height="16" rx="3.5" />
        <path className="ic-ac" d="M9 4v16" />
      </OptScale>
    </svg>
  );
}

/** 右面板切换（与左面板开关同一套造型，镜像） */
export function IconPanelRight({ size = 18, color = "currentColor", strokeWidth = 1.75, open = false, ...rest }: IconProps & { open?: boolean }) {
  return (
    <svg {...baseProps(size, color, strokeWidth, rest, "panel-right")} data-open={open ? "true" : "false"}>
      <OptScale scale={1.19} size={size} strokeWidth={strokeWidth}>
        <path className="ic-fill ic-pane" d="M15 4h2.5A3.5 3.5 0 0 1 21 7.5v9a3.5 3.5 0 0 1-3.5 3.5H15z" />
        <rect x="3" y="4" width="18" height="16" rx="3.5" />
        <path className="ic-ac" d="M15 4v16" />
      </OptScale>
    </svg>
  );
}

/* ── 品牌专属彩色图标（antd 无对应图形，保留自绘） ───────────── */

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

/* ── 语义别名（指向上面已桥接的实现，不要删：调用方按语义名取用） ── */

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
export const IconHash = IconLayers;
export const IconSquarePlus = IconFolderPlus;

/** 供调用方显式指定 CSS 属性时的类型别名（历史用法保留） */
export type IconStyle = CSSProperties;
