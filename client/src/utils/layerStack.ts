/** 浮层栈（plan-562-2318 B6）：记录当前打开的所有浮层，让 Esc 只关最上面那一层。
 *
 *  为什么需要单独一个模块：`Popover.tsx` 与 `Floating.tsx` 各自维护过一份模块级
 *  `openStack` + 递增 `seq`，各自判断「我是不是栈顶」。两者同时打开时（例如右面板的
 *  仓库菜单 + 标题栏的「⋯」菜单），一次 Esc 会把**两层都关掉**——两个监听都在捕获阶段，
 *  `stopPropagation()` 拦不住同一元素上注册的其它监听（那需要 `stopImmediatePropagation`，
 *  而它又会误伤真正的嵌套浮层）。
 *
 *  现在两层共用同一份栈与同一套 id 序列，「是否为栈顶」只有一个判定口径：
 *  只有最后打开的那一层响应 Esc，其余层按普通冒泡处理。
 */

/** 一层浮层：id 用于身份比较，close 供 Esc 调用。 */
export interface LayerEntry {
  id: number;
  close: () => void;
}

const stack: LayerEntry[] = [];
let seq = 0;

/** 登记一层，返回它的 id（卸载时用 `removeLayer(id)` 摘除）。 */
export function pushLayer(close: () => void): number {
  seq += 1;
  stack.push({ id: seq, close });
  return seq;
}

/** 摘除一层（幂等：找不到直接返回）。 */
export function removeLayer(id: number): void {
  const i = stack.findIndex((x) => x.id === id);
  if (i >= 0) stack.splice(i, 1);
}

/** 该层是否是最上面那一层（只有栈顶响应 Esc）。 */
export function isTopLayer(id: number): boolean {
  return stack.length > 0 && stack[stack.length - 1].id === id;
}

/** 当前栈深度（诊断 / 测试用）。 */
export function layerDepth(): number {
  return stack.length;
}
