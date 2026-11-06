/** plan-20261020-retry-cred-perf D11：预览编辑器自己接管尺寸重排。
 *  Monaco 的 automaticLayout 在拖右面板 / 面板折叠过渡期会逐帧重排整个可见视口（一帧甚至多次）；
 *  这里改为 rAF 合帧的 ResizeObserver——每帧最多 layout 一次，拖拽期内容仍与容器同帧跟随，
 *  视觉与自动布局一致，只是把重复重排压成一次。只作用于预览编辑器。
 *  尺寸一律量宿主容器、显式传给 layout()，不让 Monaco 量自己的根节点：diff 编辑器关闭 automaticLayout 后
 *  会把根节点高度写成上一次测到的值（至少 5px）。若在隐藏（0 高）时测过一次，根节点就被钉在 5px，
 *  之后容器撑开也量不到变化，变更对比只剩一行。 */
interface LayoutTarget {
  layout: (dimension: { width: number; height: number }) => void;
  onDidDispose: (listener: () => void) => unknown;
  /** 代码编辑器与 diff 编辑器都挂在这个容器里 */
  getContainerDomNode: () => HTMLElement;
}

export function attachManualLayout(target: LayoutTarget): void {
  const host = target.getContainerDomNode();
  let raf = 0;
  const ro = new ResizeObserver(() => {
    if (raf) return;
    raf = requestAnimationFrame(() => {
      raf = 0;
      const width = host.clientWidth;
      const height = host.clientHeight;
      // 隐藏态（0 尺寸）不排版：重新可见时容器尺寸变化会再触发一次
      if (width === 0 || height === 0) return;
      try { target.layout({ width, height }); } catch { /* 实例已销毁 */ }
    });
  });
  ro.observe(host);
  target.onDidDispose(() => {
    ro.disconnect();
    if (raf) cancelAnimationFrame(raf);
  });
}

