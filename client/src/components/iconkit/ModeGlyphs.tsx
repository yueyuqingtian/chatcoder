/** 执行模式 / 权限模式图标（plan-630-2590）。
 *
 * 历史：plan-128-518 起为手绘 24 网格线条图标，靠 `svg.lx` + `.lx-*` 类名做悬停微动。
 *
 * 为什么要换成 antd 图标（用户明确要求 + 顺手修掉一个真实缺陷）：
 *  - 视觉统一：主图标库、侧栏、右面板都已是 antd 图形，只剩输入框这两组模式图标还是自绘线条，
 *    同屏并排时一个是实心面、一个是细线稿，重量明显不一致；
 *  - 颜色割裂（关键）：styles/icons.css 里有一条祖先态规则会把 `data-state="open"` 浮层内的
 *    **所有** `svg.lx` 强行染成 `--ic-accent`。模式浮层正是 `data-state="open"`，于是浮层一打开，
 *    这 8 枚图标就整体偏红/偏橙（暖色主题下尤其明显），而同一菜单里插件/技能/连接器用的是
 *    `svg.ic`（antd），只继承 `--text-2` 的灰。用户反馈的「一部分红、一部分黑」就是它。
 *    改用 antd 图形后，这批图标不再带 `lx` 类名，该规则自然不再命中。
 *
 * 颜色仍走 `currentColor`：默认次级色、选中主色、危险项错误色全部由宿主（`.pop-item-icon`
 * / `.cc-accent-chip` / `.is-danger`）决定，本文件不写任何颜色。
 */

import { AntIcon } from "../antd-icons/AntIcon";
import { GLYPHS } from "../antd-icons/registry";

type P = { size?: number };

/** 智能体：自主读写的执行主体 */
export function MgAgent({ size = 16 }: P) {
  return <AntIcon glyph={GLYPHS.robot} glyphKey="robot" name="mg-agent" size={size} />;
}
/** 工作流：多个步骤节点串接成流水线 */
export function MgWorkflow({ size = 16 }: P) {
  return <AntIcon glyph={GLYPHS.partition} glyphKey="partition" name="mg-workflow" size={size} />;
}
/** 计划：先出方案再动手 */
export function MgPlan({ size = 16 }: P) {
  return <AntIcon glyph={GLYPHS.profile} glyphKey="profile" name="mg-plan" size={size} />;
}
/** 只读：只读不改 */
export function MgReadonly({ size = 16 }: P) {
  return <AntIcon glyph={GLYPHS.eye} glyphKey="eye" name="mg-readonly" size={size} />;
}
/** 目标：同心靶心 */
export function MgGoal({ size = 16 }: P) {
  return <AntIcon glyph={GLYPHS.aim} glyphKey="aim" name="mg-goal" size={size} />;
}

/** 每次询问：带问号的圆形提示（对应「每次都要问」的语义） */
export function MgAsk({ size = 16 }: P) {
  return <AntIcon glyph={GLYPHS.questionCircle} glyphKey="questionCircle" name="mg-ask" size={size} />;
}
/** 自动审批：闪电表示「不再逐条拦」 */
export function MgAuto({ size = 16 }: P) {
  return <AntIcon glyph={GLYPHS.thunderbolt} glyphKey="thunderbolt" name="mg-auto" size={size} />;
}
/** 完全访问：已打开的锁（危险色由宿主 .is-danger 提供，本文件不写颜色） */
export function MgFull({ size = 16 }: P) {
  return <AntIcon glyph={GLYPHS.unlock} glyphKey="unlock" name="mg-full" size={size} />;
}
