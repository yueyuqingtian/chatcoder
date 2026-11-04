/** plan-20261103-attachment-preview：滚轮按连续比例变化，兼容像素、行和整页滚动单位。 */
export function wheelZoom(current: number, delta: number, mode = 0): number {
  const pixels = delta * (mode === 1 ? 16 : mode === 2 ? 800 : 1);
  return Math.min(400, Math.max(10, current * Math.exp(-pixels * 0.0015)));
}
