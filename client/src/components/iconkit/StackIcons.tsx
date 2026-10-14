/** 线性交互图标（plan-91-393 起；plan-20261014-fix4 重绘为高精度版本）。
 *
 * plan-20261014-fix4（用户反馈「全局很多图标不精细」，参考图二图三的高精度风格）：
 *  · 绘制：24 网格，图形活动区约 3→21（原 4.5→19.5，缩到 16px 后图形只有 10px 左右），
 *    横竖线落整数坐标，描边走光学线宽（opticalStroke：16px 时 2 单位 = 1.5 倍屏 2 个整像素）；
 *  · 三态（宿主驱动，规则见 styles/icons.css）：
 *    常规 —— 单色线稿；
 *    悬停 —— 线色加深（宿主负责）、填充层 .ic-fill 淡入、带 .lx-* 的部件做一次贴合含义的微动；
 *    聚焦 —— 选中 / 展开 / 按下：强调部件 .ic-ac / .ic-ac-fill 着主题强调色、填充层改为强调色淡底
 *            （双色：主线稿保持中性，只点亮一处），不加任何描边外圈。
 * 导出名沿用 Ix* 以免改动调用处。 */
import type { ReactNode } from "react";
import { GEAR_D, opticalStroke } from "../icons";

type P = { size?: number; className?: string };

function Line({ size = 16, name, className, children }: P & { name: string; children: ReactNode }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth={opticalStroke(size)} strokeLinecap="round" strokeLinejoin="round" aria-hidden
      className={["ic lx ic-duo", `lx-${name}`, className ?? ""].filter(Boolean).join(" ")}>
      {children}
    </svg>
  );
}

/** 对话气泡（新建任务）：主体 3→21 × 4→18，尾巴落在左下 */
const BUBBLE_D = "M7 4h10a4 4 0 0 1 4 4v6a4 4 0 0 1-4 4h-5.6l-3.6 2.7c-.3.22-.8.02-.8-.38V18a4 4 0 0 1-4-4V8a4 4 0 0 1 4-4z";
/** 文件夹（与 icons.tsx 的 IconFolder 同一外形） */
const FOLDER_D = "M3 6.5A2.5 2.5 0 0 1 5.5 4h3.59a2 2 0 0 1 1.41.59L12.41 6.5c.38.38.89.5 1.42.5H18.5A2.5 2.5 0 0 1 21 9.5v8a2.5 2.5 0 0 1-2.5 2.5h-13A2.5 2.5 0 0 1 3 17.5z";

/* ── 左侧导航 ── */

/** 新任务：气泡 + 居中加号（加号为强调部件，悬停时转出） */
export function IxNewChat(p: P) {
  return <Line {...p} name="newchat">
    <path className="ic-fill" d={BUBBLE_D} />
    <path d={BUBBLE_D} />
    <path className="ic-ac lx-pop" d="M12 8v6M9 11h6" />
  </Line>;
}
/** 搜索：放大镜，悬停时向左上扫一下；镜片为强调部件 */
export function IxSearch(p: P) {
  return <Line {...p} name="search">
    <g className="lx-scan">
      <circle className="ic-fill" cx="11" cy="11" r="7" />
      <circle className="ic-ac" cx="11" cy="11" r="7" />
      <path d="m16 16 4.5 4.5" />
    </g>
  </Line>;
}
/** 自动化：日历 + 闪电，悬停时挂环跳起、闪电轻闪；闪电为强调部件 */
export function IxAutomation(p: P) {
  return <Line {...p} name="auto">
    <rect className="ic-fill" x="3" y="5" width="18" height="15" rx="3.5" />
    <rect x="3" y="5" width="18" height="15" rx="3.5" />
    <path className="lx-hop" d="M8 3v4M16 3v4" />
    <path d="M3 10h18" />
    <path className="ic-ac lx-zap" d="M13 12 10.75 15.25h2.5L11 18.5" />
  </Line>;
}
/** 拓展：三块积木 + 一枚圆形插件，悬停时圆形弹出；圆形为强调部件 */
export function IxExtensions(p: P) {
  return <Line {...p} name="ext">
    <rect x="3" y="3" width="7" height="7" rx="2" />
    <rect x="3" y="14" width="7" height="7" rx="2" />
    <rect x="14" y="14" width="7" height="7" rx="2" />
    <g className="lx-lift">
      <circle className="ic-fill" cx="17.5" cy="6.5" r="3.5" />
      <circle className="ic-ac" cx="17.5" cy="6.5" r="3.5" />
    </g>
  </Line>;
}
/** 设置：齿轮，悬停时转 45°（8 齿，转一个齿距后形状复位）；轴心为强调部件 */
export function IxSettings(p: P) {
  return <Line {...p} name="settings">
    <g className="lx-spin">
      <path className="ic-fill" d={GEAR_D} />
      <path d={GEAR_D} />
      <circle className="ic-ac" cx="12" cy="12" r="3" />
    </g>
  </Line>;
}

/* ── 消息操作 ── */

export function IxCopy(p: P) {
  return <Line {...p} name="copy">
    <path d="M16 4.5A1.5 1.5 0 0 0 14.5 3H6a3 3 0 0 0-3 3v8.5A1.5 1.5 0 0 0 4.5 16" />
    <g className="lx-nudge">
      <rect className="ic-fill" x="8" y="8" width="13" height="13" rx="3" />
      <rect className="ic-ac" x="8" y="8" width="13" height="13" rx="3" />
    </g>
  </Line>;
}
export function IxRegenerate(p: P) {
  return <Line {...p} name="regen">
    <g className="lx-turn">
      <path d="M20 12a8 8 0 1 1-2.34-5.66" />
      <path className="ic-ac" d="M20 4v4.5h-4.5" />
    </g>
  </Line>;
}
export function IxRollback(p: P) {
  return <Line {...p} name="rollback">
    <g className="lx-turn-back">
      <path d="M4 12a8 8 0 1 0 2.34-5.66" />
      <path className="ic-ac" d="M4 4v4.5h4.5" />
    </g>
    <path d="M12 8v4l2.5 1.5" />
  </Line>;
}
export function IxThumbUp(p: P) {
  return <Line {...p} name="up">
    <g className="lx-hop">
      <path className="ic-fill" d="M7 10.5 10.6 3.8a1.9 1.9 0 0 1 3.5 1.3L13.4 9h5.1a2.5 2.5 0 0 1 2.46 2.95l-1.1 6A2.5 2.5 0 0 1 17.4 20H7z" />
      <path d="M7 10.5 10.6 3.8a1.9 1.9 0 0 1 3.5 1.3L13.4 9h5.1a2.5 2.5 0 0 1 2.46 2.95l-1.1 6A2.5 2.5 0 0 1 17.4 20H7" />
      <rect className="ic-ac" x="3" y="10" width="4" height="10" rx="1.5" />
    </g>
  </Line>;
}
export function IxThumbDown(p: P) {
  return <Line {...p} name="down">
    <g className="lx-drop">
      <path className="ic-fill" d="M7 13.5 10.6 20.2a1.9 1.9 0 0 0 3.5-1.3L13.4 15h5.1a2.5 2.5 0 0 0 2.46-2.95l-1.1-6A2.5 2.5 0 0 0 17.4 4H7z" />
      <path d="M7 13.5 10.6 20.2a1.9 1.9 0 0 0 3.5-1.3L13.4 15h5.1a2.5 2.5 0 0 0 2.46-2.95l-1.1-6A2.5 2.5 0 0 0 17.4 4H7" />
      <rect className="ic-ac" x="3" y="4" width="4" height="10" rx="1.5" />
    </g>
  </Line>;
}

/* ── 右侧面板标签 ── */

export function IxTasks(p: P) {
  return <Line {...p} name="tasks">
    <path d="M11 7h9M11 12h9M11 17h6" />
    <path className="ic-ac lx-draw" d="m4 7 1.5 1.5L8.5 5.5M4 12l1.5 1.5L8.5 10.5" />
    <circle cx="6" cy="17" r="1.5" fill="currentColor" stroke="none" />
  </Line>;
}
/** 工作流：两个步骤块由折线相连，终点块为强调部件，悬停时沿折线轻推 */
export function IxWorkflow(p: P) {
  return <Line {...p} name="workflow">
    <rect x="3" y="3" width="8" height="8" rx="2.5" />
    <path d="M7 11v3a3 3 0 0 0 3 3h3" />
    <g className="lx-nudge">
      <rect className="ic-fill" x="13" y="13" width="8" height="8" rx="2.5" />
      <rect className="ic-ac" x="13" y="13" width="8" height="8" rx="2.5" />
    </g>
  </Line>;
}
export function IxBrowser(p: P) {
  return <Line {...p} name="browser">
    <circle className="ic-fill" cx="12" cy="12" r="9" />
    <circle cx="12" cy="12" r="9" />
    <path d="M3 12h18" />
    <path className="ic-ac lx-squish" d="M12 3c2.5 2.4 3.75 5.4 3.75 9S14.5 18.6 12 21c-2.5-2.4-3.75-5.4-3.75-9S9.5 5.4 12 3z" />
  </Line>;
}
export function IxTerminal(p: P) {
  return <Line {...p} name="term">
    <rect className="ic-fill" x="3" y="4" width="18" height="16" rx="3.5" />
    <rect x="3" y="4" width="18" height="16" rx="3.5" />
    <path className="lx-nudge" d="m7.5 9.5 2.5 2.5-2.5 2.5" />
    <path className="ic-ac lx-blink" d="M13 15h4" />
  </Line>;
}
export function IxFiles(p: P) {
  return <Line {...p} name="files">
    <path className="ic-fill" d={FOLDER_D} />
    <path d={FOLDER_D} />
    <path className="ic-ac lx-lid" d="M3 11h18" />
  </Line>;
}
export function IxBot(p: P) {
  return <Line {...p} name="bot">
    <rect className="ic-fill" x="4" y="8" width="16" height="12" rx="4" />
    <rect x="4" y="8" width="16" height="12" rx="4" />
    <g className="lx-hop">
      <path d="M12 8V5.5" />
      <circle className="ic-ac-fill" cx="12" cy="4" r="1.5" fill="currentColor" stroke="none" />
    </g>
    <circle className="ic-ac-fill" cx="9" cy="14" r="1.5" fill="currentColor" stroke="none" />
    <circle className="ic-ac-fill" cx="15" cy="14" r="1.5" fill="currentColor" stroke="none" />
  </Line>;
}
export function IxBug(p: P) {
  return <Line {...p} name="bug">
    <rect className="ic-fill" x="7" y="8" width="10" height="13" rx="5" />
    <rect x="7" y="8" width="10" height="13" rx="5" />
    <path d="M9.5 8a2.5 2.5 0 0 1 5 0" />
    <path className="ic-ac" d="M12 12v9" />
    <path className="lx-wiggle" d="M3.5 13H7M17 13h3.5M4.5 19.5 7.5 18M19.5 19.5 16.5 18" />
  </Line>;
}
