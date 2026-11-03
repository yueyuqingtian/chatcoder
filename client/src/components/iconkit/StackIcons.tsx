/** 界面主图标（侧栏导航 / 消息操作 / 右面板标签）。
 *
 * 历史：plan-91-393 起为手绘 24 网格线条图标，plan-20261014-fix4 重绘为高精度版本，
 * 并配合 styles/icons.css 的 `.lx-*` 实现了「悬停填充淡入 + 部件微动 + 选中强调色」三态。
 *
 * 现状（plan-630-2590）：图形统一改为 Ant Design 图标，由 antd-icons/AntIcon 渲染。
 * 导出名 `Ix*` 与 props 签名（`{ size?: number; className?: string }`）保持不变，
 * 调用方无需改动。
 *
 * 动效取舍：`.lx-*` 系列部件微动（扫描 / 抬起 / 回旋等）依赖手绘 SVG 的内部路径，
 * antd 图标结构里没有这些节点，按 plan-630-2590 D3 接受降级；
 * `.ic-settings` 这类整枚旋转仍由外层 svg 类名驱动，继续生效。
 */

import { AntIcon } from "../antd-icons/AntIcon";
import { GLYPHS } from "../antd-icons/registry";

type P = { size?: number; className?: string };

/* ── 左侧导航 ── */

/** 新任务：新建对话 */
export function IxNewChat({ size = 16, className }: P) {
  return <AntIcon glyph={GLYPHS.message} name="newchat" size={size} className={className} />;
}
/** 搜索 */
export function IxSearch({ size = 16, className }: P) {
  return <AntIcon glyph={GLYPHS.search} name="search" size={size} className={className} />;
}
/** 自动化：定时任务 */
export function IxAutomation({ size = 16, className }: P) {
  return <AntIcon glyph={GLYPHS.schedule} name="auto" size={size} className={className} />;
}
/** 拓展：插件 / 技能 */
export function IxExtensions({ size = 16, className }: P) {
  return <AntIcon glyph={GLYPHS.appstore} name="ext" size={size} className={className} />;
}
/** 设置 */
export function IxSettings({ size = 16, className }: P) {
  return <AntIcon glyph={GLYPHS.setting} name="settings" size={size} className={className} />;
}

/* ── 消息操作 ── */

export function IxCopy({ size = 16, className }: P) {
  return <AntIcon glyph={GLYPHS.copy} name="copy" size={size} className={className} />;
}
export function IxRegenerate({ size = 16, className }: P) {
  return <AntIcon glyph={GLYPHS.reload} name="regen" size={size} className={className} />;
}
export function IxRollback({ size = 16, className }: P) {
  return <AntIcon glyph={GLYPHS.rollback} name="rollback" size={size} className={className} />;
}
export function IxThumbUp({ size = 16, className }: P) {
  return <AntIcon glyph={GLYPHS.like} name="up" size={size} className={className} />;
}
export function IxThumbDown({ size = 16, className }: P) {
  return <AntIcon glyph={GLYPHS.dislike} name="down" size={size} className={className} />;
}

/* ── 右侧面板标签 ── */

export function IxTasks({ size = 16, className }: P) {
  return <AntIcon glyph={GLYPHS.checkSquare} name="tasks" size={size} className={className} />;
}
/** 工作流 */
export function IxWorkflow({ size = 16, className }: P) {
  return <AntIcon glyph={GLYPHS.partition} name="workflow" size={size} className={className} />;
}
export function IxBrowser({ size = 16, className }: P) {
  return <AntIcon glyph={GLYPHS.global} name="browser" size={size} className={className} />;
}
export function IxTerminal({ size = 16, className }: P) {
  return <AntIcon glyph={GLYPHS.code} name="term" size={size} className={className} />;
}
export function IxFiles({ size = 16, className }: P) {
  return <AntIcon glyph={GLYPHS.folder} name="files" size={size} className={className} />;
}
export function IxBot({ size = 16, className }: P) {
  return <AntIcon glyph={GLYPHS.robot} name="bot" size={size} className={className} />;
}
export function IxBug({ size = 16, className }: P) {
  return <AntIcon glyph={GLYPHS.bug} name="bug" size={size} className={className} />;
}
