/** plan-20261103-preview-polish：加载态填满宿主，避免显示未就绪的空网格角标。 */
export function ResourceLoading({ children = "正在加载预览…" }: { children?: string }) {
  return <div className="resource-state resource-loading" role="status">
    <span className="resource-loading-ring" aria-hidden="true" />
    <span>{children}</span>
  </div>;
}
