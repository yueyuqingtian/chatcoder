/** 线性交互图标（plan-91-393，参考图5：细线、留白充足、选中时着主题色）。
 * 用户反馈「全部做成同一种叠层样式很丑」，因此每枚图标按含义各自设计微动：
 *  · 常规：1.6 描边线稿，currentColor；
 *  · hover：带 .lx-* 类的部件做一次贴合含义的动作（气泡冒出加号、放大镜扫一下、积木分开…）；
 *  · 按下：整枚轻压回弹；
 *  · 选中（宿主 .active / aria-selected / aria-current）：线色变主题色，.lx-fill 部件填充淡主题色。
 * 状态由宿主驱动（styles/icons.css「线性交互图标」段）。导出名沿用 Ix* 以免改动调用处。 */
import type { ReactNode } from "react";

type P = { size?: number; className?: string };

function Line({ size = 16, name, children }: P & { name: string; children: ReactNode }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.6}
      strokeLinecap="round" strokeLinejoin="round" aria-hidden className={`ic lx lx-${name}`}>
      {children}
    </svg>
  );
}

/* ── 左侧导航 ── */

/** 新任务：气泡，hover 时右上加号转出 */
export function IxNewChat(p: P) {
  return <Line {...p} name="newchat">
    <path className="lx-fill" d="M4.5 6.5a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v6a2 2 0 0 1-2 2H10l-3.5 3v-3a2 2 0 0 1-2-2z" />
    <path className="lx-pop" d="M19 5.5v4M17 7.5h4" />
  </Line>;
}
/** 搜索：放大镜，hover 时向左上扫一下 */
export function IxSearch(p: P) {
  return <Line {...p} name="search">
    <g className="lx-scan"><circle className="lx-fill" cx="10.5" cy="10.5" r="5.5" /><path d="M14.5 14.5L19.5 19.5" /></g>
  </Line>;
}
/** 自动化：日历，hover 时挂环跳起 + 闪电转出 */
export function IxAutomation(p: P) {
  return <Line {...p} name="auto">
    <rect className="lx-fill" x="4" y="5.5" width="16" height="14" rx="2.5" />
    <path className="lx-hop" d="M8.5 3.5v3.5M15.5 3.5v3.5" />
    <path d="M4 10h16" />
    <path className="lx-pop" d="M12.6 12l-2 3h3l-1.4 2.6" />
  </Line>;
}
/** 拓展：四块积木，hover 时右上一块翘起 */
export function IxExtensions(p: P) {
  return <Line {...p} name="ext">
    <rect x="4.5" y="4.5" width="6" height="6" rx="1.6" />
    <rect x="4.5" y="13.5" width="6" height="6" rx="1.6" />
    <rect x="13.5" y="13.5" width="6" height="6" rx="1.6" />
    <g className="lx-lift"><rect className="lx-fill" x="13.5" y="4.5" width="6" height="6" rx="1.6" transform="rotate(45 16.5 7.5)" /></g>
  </Line>;
}
/** 设置：齿轮，hover 时转 60° */
export function IxSettings(p: P) {
  return <Line {...p} name="settings">
    <g className="lx-spin">
      <path className="lx-fill" d="M12 3.8l1.6 1.6 2.2-.5.6 2.2 2.2.6-.5 2.2 1.6 1.6-1.6 1.6.5 2.2-2.2.6-.6 2.2-2.2-.5L12 20.2l-1.6-1.6-2.2.5-.6-2.2-2.2-.6.5-2.2L4.3 12l1.6-1.6-.5-2.2 2.2-.6.6-2.2 2.2.5z" />
      <circle cx="12" cy="12" r="2.6" />
    </g>
  </Line>;
}

/* ── 消息操作 ── */

export function IxCopy(p: P) {
  return <Line {...p} name="copy">
    <path d="M15 4.5H6.5a2 2 0 0 0-2 2V15" />
    <rect className="lx-fill lx-nudge" x="8.5" y="8.5" width="11" height="11" rx="2" />
  </Line>;
}
export function IxRegenerate(p: P) {
  return <Line {...p} name="regen">
    <g className="lx-turn"><path d="M19 12a7 7 0 1 1-2.05-4.95" /><path d="M19.5 4.5v3.5H16" /></g>
  </Line>;
}
export function IxRollback(p: P) {
  return <Line {...p} name="rollback">
    <g className="lx-turn-back"><path d="M5 12a7 7 0 1 0 2.05-4.95" /><path d="M4.5 4.5v3.5H8" /></g>
    <path d="M12 9v3.2l2 1.3" />
  </Line>;
}
export function IxThumbUp(p: P) {
  return <Line {...p} name="up">
    <g className="lx-hop"><path d="M7.5 11v8.5H5a1 1 0 0 1-1-1V12a1 1 0 0 1 1-1z" /><path className="lx-fill" d="M7.5 11l3.6-6.4a1.7 1.7 0 0 1 3.1 1.2L13.4 9.5h4.7a2 2 0 0 1 2 2.3l-1 6a2 2 0 0 1-2 1.7H7.5" /></g>
  </Line>;
}
export function IxThumbDown(p: P) {
  return <Line {...p} name="down">
    <g className="lx-drop"><path d="M7.5 13V4.5H5a1 1 0 0 0-1 1V12a1 1 0 0 0 1 1z" /><path className="lx-fill" d="M7.5 13l3.6 6.4a1.7 1.7 0 0 0 3.1-1.2l-.8-3.7h4.7a2 2 0 0 0 2-2.3l-1-6a2 2 0 0 0-2-1.7H7.5" /></g>
  </Line>;
}

/* ── 右侧面板标签 ── */

export function IxTasks(p: P) {
  return <Line {...p} name="tasks">
    <path d="M11 7h8.5M11 12h8.5M11 17h6" />
    <path className="lx-draw" d="M4.5 7l1.3 1.3L8 6M4.5 12l1.3 1.3L8 11" />
    <circle cx="6.2" cy="17" r="1.2" />
  </Line>;
}
/** 工作流：三节点分支，末端节点悬停时沿分支滑入 */
export function IxWorkflow(p: P) {
  return <Line {...p} name="workflow">
    <circle cx="6" cy="6" r="2" />
    <circle cx="6" cy="18" r="2" />
    <circle className="lx-fill" cx="18" cy="12" r="2.2" />
    <path d="M6 8v8M8 6.5c4 0 6 2 7.8 4.6M8 17.5c4 0 6-2 7.8-4.6" />
  </Line>;
}
export function IxBrowser(p: P) {
  return <Line {...p} name="browser">
    <circle className="lx-fill" cx="12" cy="12" r="8" />
    <path d="M4 12h16" />
    <ellipse className="lx-squish" cx="12" cy="12" rx="3.5" ry="8" />
  </Line>;
}
export function IxTerminal(p: P) {
  return <Line {...p} name="term">
    <rect className="lx-fill" x="3.5" y="4.5" width="17" height="15" rx="2.5" />
    <path className="lx-nudge" d="M7.5 9.5l3 2.5-3 2.5" />
    <path className="lx-blink" d="M13 15h4" />
  </Line>;
}
export function IxFiles(p: P) {
  return <Line {...p} name="files">
    <path className="lx-fill" d="M3.5 8.5v9a2 2 0 0 0 2 2h13a2 2 0 0 0 2-2v-7a2 2 0 0 0-2-2h-7l-2-2.5h-4a2 2 0 0 0-2 2z" />
    <path className="lx-lid" d="M3.5 10.5h17" />
  </Line>;
}
export function IxBot(p: P) {
  return <Line {...p} name="bot">
    <rect className="lx-fill" x="4.5" y="8" width="15" height="11" rx="3" />
    <path d="M9.5 13h.01M14.5 13h.01" strokeWidth={2.4} />
    <g className="lx-hop"><path d="M12 8V5.5" /><circle cx="12" cy="4.5" r="1.1" /></g>
  </Line>;
}
export function IxBug(p: P) {
  return <Line {...p} name="bug">
    <rect className="lx-fill" x="7.5" y="8" width="9" height="11.5" rx="4.5" />
    <path d="M9.5 8a2.5 2.5 0 0 1 5 0M12 11v8.5" />
    <path className="lx-wiggle" d="M4.5 12h3M16.5 12h3M5 17l2.5-1M19 17l-2.5-1M5 7l2.5 1.5M19 7l-2.5 1.5" />
  </Line>;
}
