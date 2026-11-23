/** InteractiveSandbox —— 消息流内嵌可交互 HTML/SVG/图表的安全沙箱容器。
 *
 *  设计要点：
 *  1. 严格隔离：`<iframe sandbox="allow-scripts">`（不含 `allow-same-origin`），
 *     叠加 CSP `connect-src 'none'`，彻底阻断对宿主 DOM / 存储 / chatcoderAPI / 外网的访问。
 *  2. 主题同步：自动注入宿主 CSS 语义变量并监听 `useThemeStore` 变化实时推送，
 *     浅/深色切换时无需重载 iframe（保留用户当前滑块/表单交互状态）。
 *  3. 虚拟列表友好：模块级 LRU 缓存最近一次实测高度，虚拟滚动卸载再挂载时首帧即占准高度，
 *     配合 `ResizeObserver` 防抖回调，避免消息流高度跳动。
 */
import { memo, useEffect, useId, useMemo, useRef, useState } from "react";
import { useThemeStore } from "../../store/theme";
import { buildSandboxDocument, readHostThemeVars } from "./sandboxDoc";

export { buildSandboxDocument };

const HEIGHT_CACHE_LIMIT = 200;
const heightCache = new Map<string, number>();

function cacheHeight(key: string, height: number): void {
  if (!key || height <= 0) return;
  if (!heightCache.has(key) && heightCache.size >= HEIGHT_CACHE_LIMIT) {
    const oldest = heightCache.keys().next().value;
    if (oldest !== undefined) heightCache.delete(oldest);
  }
  heightCache.set(key, height);
}

export interface InteractiveSandboxProps {
  html: string;
  /** 用于虚拟列表卸载/重挂载时复用实测高度，避免滚动跳动 */
  cacheKey?: string;
  minHeight?: number;
  maxHeight?: number;
  /** true 时铺满父容器高度（用于大窗预览） */
  fillHeight?: boolean;
  /** true 时内容贴边渲染，不附加外层内边距 */
  flush?: boolean;
  /** iframe 的无障碍标题 */
  title?: string;
  className?: string;
  onAction?: (action: string, payload: unknown) => void;
  onOverflowChange?: (overflowing: boolean) => void;
}

export const InteractiveSandbox = memo(function InteractiveSandbox({
  html,
  cacheKey = "",
  minHeight = 120,
  maxHeight = 500,
  fillHeight = false,
  flush = false,
  title = "可交互组件预览",
  className,
  onAction,
  onOverflowChange,
}: InteractiveSandboxProps) {
  const instanceId = useId();
  const iframeRef = useRef<HTMLIFrameElement>(null);
  const theme = useThemeStore((s) => s.theme);
  const world = useThemeStore((s) => s.world);
  const initialThemeRef = useRef(theme);
  const onActionRef = useRef(onAction);
  onActionRef.current = onAction;
  const onOverflowRef = useRef(onOverflowChange);
  onOverflowRef.current = onOverflowChange;

  const [height, setHeight] = useState<number>(() => {
    const cached = cacheKey ? heightCache.get(cacheKey) : undefined;
    const base = cached ?? Math.max(minHeight, 180);
    return maxHeight > 0 ? Math.min(maxHeight, Math.max(minHeight, base)) : Math.max(minHeight, base);
  });

  // 仅当 html 变化（或首次挂载）时重建 srcDoc；主题切换走 postMessage 无刷新换肤
  const srcDoc = useMemo(() => {
    const vars = readHostThemeVars();
    return buildSandboxDocument(html, instanceId, initialThemeRef.current, vars, flush);
  }, [html, instanceId, flush]);

  useEffect(() => {
    const onMessage = (e: MessageEvent) => {
      const data = e.data as { channel?: string; id?: string; type?: string; height?: number; action?: string; payload?: unknown } | null;
      if (!data || data.channel !== "chatcoder-ui" || data.id !== instanceId) return;
      if (e.source !== iframeRef.current?.contentWindow) return;
      if (data.type === "resize") {
        const rawH = Number(data.height) || 0;
        if (rawH <= 0) return;
        const overflowing = maxHeight > 0 && rawH > maxHeight;
        onOverflowRef.current?.(overflowing);
        const clamped = maxHeight > 0 ? Math.min(maxHeight, Math.max(minHeight, rawH)) : Math.max(minHeight, rawH);
        if (cacheKey) cacheHeight(cacheKey, clamped);
        setHeight(clamped);
      } else if (data.type === "action") {
        onActionRef.current?.(String(data.action ?? ""), data.payload);
      }
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [instanceId, cacheKey, minHeight, maxHeight]);

  // 宿主题或主题世界变化时向沙箱推送最新 CSS 变量
  useEffect(() => {
    const win = iframeRef.current?.contentWindow;
    if (!win) return;
    // 等一帧确保 :root 的 data-theme / data-world 已应用新计算样式
    const raf = requestAnimationFrame(() => {
      try {
        win.postMessage({ channel: "chatcoder-host", type: "theme", theme, vars: readHostThemeVars() }, "*");
      } catch { /* ignore */ }
    });
    return () => cancelAnimationFrame(raf);
  }, [theme, world]);

  const frameClass = ["iui-sandbox-frame", fillHeight ? "is-fill" : "", className ?? ""].filter(Boolean).join(" ");

  return (
    <iframe
      ref={iframeRef}
      className={frameClass}
      sandbox="allow-scripts"
      srcDoc={srcDoc}
      title={title}
      style={fillHeight ? undefined : { height: `${height}px` }}
      loading="lazy"
    />
  );
});
