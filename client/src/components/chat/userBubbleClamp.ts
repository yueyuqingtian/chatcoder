/** 用户气泡「超长折叠」的纯判据（plan-631-2594）。
 *
 *  ── 为什么单独抽出来 ──
 *  折叠与否原本完全依赖一次像素测量（`scrollHeight > 行高 × 6`）。可一旦这次测量没有跑出
 *  结果（面板折叠过渡 / 窗口运动期间挂载被跳过、容器当时不可见测到 0 高度等），
 *  `overflow` 就停在 false——长消息不折叠，「展开全文」按钮也不渲染，两个症状必然同时出现。
 *
 *  这里补一条不依赖 DOM 的下限判据，保证「明显很长」的消息在任何时序下都会被折叠：
 *   · `white-space: pre-wrap` 下硬换行必然独占一行，超过 COLLAPSE_LINES 行就一定超长；
 *   · 字符数按最宽气泡（--content-max-w 1050px、14px 字号，一行约 75 个汉字）折算，
 *     COLLAPSE_CHARS 也远超 6 行。
 *  边界情况仍以像素测量为准（宽度变化时它会把结论校正回来）。
 */

/** 折叠行数阈值：超过 6 行才出现控件 */
export const COLLAPSE_LINES = 6;

/** 兜底字符数阈值：超过即判超长，不再等测量 */
export const COLLAPSE_CHARS = 800;

/** 文本本身是否已足以判定「超长」（不读布局）。 */
export function isLongUserMessage(text: string): boolean {
  if (!text) return false;
  if (text.length > COLLAPSE_CHARS) return true;
  let lines = 1;
  for (let i = 0; i < text.length; i++) {
    if (text.charCodeAt(i) === 10) lines++;
  }
  return lines > COLLAPSE_LINES;
}
