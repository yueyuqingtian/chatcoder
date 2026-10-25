/** 工具卡形态判定（plan-156-619 S7b；plan-156-620 改为按真实几何判定；plan-20261025-shell 需求 4 改为「放不下就悬浮」）。
 *
 * 四档形态：
 *   · expanded：右侧（含内容列按需左移后）放得下展开卡片，且用户展开过——停靠在右上角，内容列让位；
 *   · floating：放不下展开卡片、但用户展开过——卡片悬浮在内容之上（不让位、不压窄消息流），
 *               此前这里会自动收回胶囊，用户在窄窗口 / 右面板展开时根本打不开卡片；
 *   · capsule ：用户收起；窄到连完整胶囊都会盖住半个消息列时，胶囊收成只剩图标的小圆钮（compactCapsule）；
 *   · hidden  ：没有任何栏目有内容，或几何尚未测量（首帧不闪错误形态）。
 */

export type ToolCardForm = "expanded" | "floating" | "capsule" | "hidden";

export interface ToolCardFit {
  /** 展开卡片在最大左移后仍缺的空间（px）。 */
  expandedShortfall: number;
  /** 胶囊在最大左移后仍缺的空间（px）。 */
  capsuleShortfall: number;
}

/** 纯函数：按几何缺口与用户意图判定形态（便于推理，不读 DOM）。 */
export function pickForm(fit: ToolCardFit, wantExpanded: boolean, hasContent: boolean): ToolCardForm {
  if (!hasContent) return "hidden";
  // 几何尚未测量（首帧）：先不显示，避免闪出一个错误形态
  if (!Number.isFinite(fit.capsuleShortfall)) return "hidden";
  if (!wantExpanded) return "capsule";
  // 放得下：停靠（内容列按需左移让位）；放不下：悬浮在内容之上，绝不压窄消息流
  return fit.expandedShortfall > 0 ? "floating" : "expanded";
}

/** 是否为卡片形态（停靠或悬浮）——形变动画、设置浮层等只区分「卡片 / 胶囊」 */
export function isCardForm(form: ToolCardForm): boolean {
  return form === "expanded" || form === "floating";
}
