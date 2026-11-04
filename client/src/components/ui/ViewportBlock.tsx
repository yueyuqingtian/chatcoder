import { createContext, memo, useCallback, useContext, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { observeFlowBlock, shouldMountBlock, updateFlowViewport } from "../../perf/flowViewport";

export const FlowViewportVisibility = createContext(true);
interface Props {
  children: ReactNode;
  estimate?: number;
  inline?: boolean;
  tableColumns?: number;
  windowed?: boolean;
  contentVersion?: unknown;
}

/** plan-20261103-flow-blank：普通正文由外层虚拟器管理，不能再套一层空壳卸载。 */
export const ViewportBlock = memo(function ViewportBlock(props: Props) {
  return props.windowed ? <MeasuredWindow {...props} /> : <>{props.children}</>;
});

function MeasuredWindow({ children, estimate = 160, inline = false, tableColumns, contentVersion }: Props) {
  const visible = useContext(FlowViewportVisibility);
  const ref = useRef<HTMLElement | null>(null);
  const setElement = useCallback((element: HTMLElement | null) => { ref.current = element; }, []);
  const [mounted, setMounted] = useState(true);
  const cache = useRef({ height: estimate, width: 0, measured: false, version: contentVersion });
  const versionChanged = cache.current.version !== contentVersion;
  const rendered = mounted || versionChanged;
  const renderedRef = useRef(rendered);
  renderedRef.current = rendered;
  const versionRef = useRef(contentVersion);
  versionRef.current = contentVersion;
  useLayoutEffect(() => {
    if (!versionChanged) return;
    cache.current = { ...cache.current, measured: false, version: contentVersion };
    setMounted(true);
  }, [versionChanged, contentVersion]);
  useLayoutEffect(() => {
    const element = ref.current;
    const root = element?.closest<HTMLElement>(".message-flow");
    if (!visible || !element || !root) return;
    // plan-20261103-flow-blank：设置里字体可改变而宽度不变，恢复时也必须重新取得行高。
    cache.current.measured = false;
    return observeFlowBlock(root, element, (frame) => {
      const stored = cache.current;
      if (frame.valid) {
        if (Math.abs(stored.width - frame.width) > 0.5 || stored.version !== versionRef.current) stored.measured = false;
        stored.width = frame.width; stored.version = versionRef.current;
        if (renderedRef.current && frame.height > 0) { stored.height = frame.height; stored.measured = true; }
      }
      const next = shouldMountBlock(renderedRef.current, stored.measured, frame);
      setMounted((previous) => previous === next ? previous : next);
    });
  }, [visible]);
  useLayoutEffect(() => { updateFlowViewport(ref.current?.closest<HTMLElement>(".message-flow") ?? null); });
  const Tag = tableColumns ? "tbody" : inline ? "span" : "div";
  return <Tag ref={setElement}
    className="viewport-block" data-flow-window="" data-mounted={rendered || undefined}
    style={rendered || tableColumns ? undefined : { height: cache.current.height }}>
    {rendered ? children : tableColumns
      ? <tr aria-hidden="true"><td colSpan={tableColumns} style={{ height: cache.current.height, padding: 0, border: 0 }} /></tr>
      : null}
  </Tag>;
}
