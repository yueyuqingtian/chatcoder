/** 工具卡形态判定（plan-156-619 S7b；plan-156-620 改为按真实几何判定）。
 *
 * 三档形态：
 *   · expanded：右侧（含内容列按需左移后）放得下展开卡片，且用户展开过；
 *   · capsule ：放不下展开卡片、或用户收起；
 *   · hidden  ：连胶囊都会遮挡会话内容（小窗 / 窄面板），或五个栏目都没有内容。
 *
 * 旧实现按宿主宽度的固定阈值（900 / 640）判定，与内容列实际占位脱节：
 * 宽度设置较大时胶囊照样压在正文上，宽屏时又过早降级。现在两档阈值都来自
 * useContentColumnGeometry 给出的「最大左移后仍缺多少空间」。
 */

export type ToolCardForm = "expanded" | "capsule" | "hidden";

export interface ToolCardFit {
  /** 展开卡片在最大左移后仍缺的空间（px）。 */
  expandedShortfall: number;
  /** 胶囊在最大左移后仍缺的空间（px）。 */
  capsuleShortfall: number;
}

/** 纯函数：按几何缺口与用户意图判定形态（便于推理，不读 DOM）。 */
export function pickForm(fit: ToolCardFit, wantExpanded: boolean, hasContent: boolean): ToolCardForm {
  if (!hasContent) return "hidden";
  // 连胶囊都放不下：直接隐藏，绝不遮挡会话
  if (fit.capsuleShortfall > 0) return "hidden";
  // 让不出展开卡片：降为胶囊（保内容列宽度，绝不压窄消息流）
  if (fit.expandedShortfall > 0) return "capsule";
  return wantExpanded ? "expanded" : "capsule";
}
