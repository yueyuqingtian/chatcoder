import type { Virtualizer } from "@tanstack/react-virtual";

export interface ReadingAnchor {
  key: string | number | bigint;
  index: number;
  offset: number;
  top: number;
  messageId?: string;
  messageOffset?: number;
}
export interface FlowReadingPosition { anchor: ReadingAnchor | null; top: number; following: boolean }
const positions = new Map<string | number, FlowReadingPosition>();
/** plan-20261103-flow-blank：设置页会卸载主消息流，阅读位置不能只存在组件 ref 中。 */
export function saveFlowPosition(key: string | number, value: FlowReadingPosition): void {
  positions.delete(key); positions.set(key, { ...value, anchor: value.anchor ? { ...value.anchor } : null });
  while (positions.size > 20) positions.delete(positions.keys().next().value!);
}
export function getFlowPosition(key: string | number): FlowReadingPosition | undefined { return positions.get(key); }
export function findAnchorIndex(count: number, keyOf: (index: number) => string | number | bigint, anchor: ReadingAnchor): number {
  if (anchor.index < count && keyOf(anchor.index) === anchor.key) return anchor.index;
  for (let index = 0; index < count; index++) if (keyOf(index) === anchor.key) return index;
  return -1;
}
/** plan-20261103-flow-blank：仅用实际相交项记录阅读位置，不能用空白处的估算项覆盖好锚点。 */
export function captureReadingAnchor(root: HTMLElement, virtualizer: Virtualizer<HTMLDivElement, Element>): ReadingAnchor | null {
  if (root.clientWidth <= 0 || root.clientHeight <= 0) return null;
  const rect = root.getBoundingClientRect();
  const top = rect.top + root.clientTop;
  const first = visibleFlowItems(root)[0];
  if (!first) return null;
  const index = Number(first.dataset.index);
  if (!Number.isFinite(index) || index < 0 || index >= virtualizer.options.count) return null;
  const message = Array.from(first.querySelectorAll<HTMLElement>("[data-message-id]"))
    .find((node) => node.getBoundingClientRect().bottom > top);
  return { key: virtualizer.options.getItemKey(index), index, offset: first.getBoundingClientRect().top - top,
    top: root.scrollTop, messageId: message?.dataset.messageId,
    messageOffset: message ? message.getBoundingClientRect().top - top : undefined };
}
export function visibleFlowItems(root: HTMLElement): HTMLElement[] {
  const viewport = root.getBoundingClientRect();
  return Array.from(root.querySelectorAll<HTMLElement>(".message-flow-virtual-inner > .message-flow-virtual-item")).filter((node) => {
    const rect = node.getBoundingClientRect();
    return rect.height > 0 && rect.bottom > viewport.top && rect.top < viewport.bottom;
  });
}

/** plan-20261103-flow-tail-gap：隐藏的零高度保留缓存，可见空项的零高度必须收敛为零。 */
export function resolveFlowItemSize(measured: number, cached: number | undefined, estimate: number, laidOut: boolean): number {
  return laidOut && Number.isFinite(measured) && measured >= 0 ? measured : cached ?? estimate;
}

/** plan-20261103-flow-blank：先读齐有效高度，再通过公开方法失效，零尺寸视图保留缓存。 */
export function refreshFlowMeasurements(root: HTMLElement, virtualizer: Virtualizer<HTMLDivElement, Element>): boolean {
  if (!root.isConnected || root.clientWidth <= 0 || root.clientHeight <= 0) return false;
  const measured: Array<{ index: number; height: number }> = [];
  virtualizer.elementsCache.forEach((node) => {
    const index = virtualizer.indexFromElement(node);
    if (!node.isConnected || index < 0 || index >= virtualizer.options.count) return;
    const height = node.getBoundingClientRect().height;
    if (height >= 0 && Number.isFinite(height)) measured.push({ index, height });
  });
  virtualizer.measure();
  for (const item of measured) virtualizer.resizeItem(item.index, item.height);
  return true;
}
