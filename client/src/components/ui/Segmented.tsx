/** Segmented —— 胶囊分段控件（plan-20261009-ui U4）。
 *
 * 选中滑块是一枚独立的绝对定位胶囊，随选中项平移 + 伸缩（弹簧），而不是给选中项换底色——
 * 切换时视线跟着滑块走，状态变化可被感知。滑块位置由选中按钮的 offsetLeft/offsetWidth 决定，
 * 字体加载或容器尺寸变化时由 ResizeObserver 重新校准，不出现错位。 */
import { useLayoutEffect, useRef, useState, type ReactNode } from "react";

/** 计数展示：≥1 万缩写成 k（43737 → 43.7k），其余带千分位——分段标签里的数字不能把标签撑得过宽 */
function formatCount(n: number): string {
  if (n >= 10000) return `${(n / 1000).toFixed(n >= 100000 ? 0 : 1).replace(/\.0$/, "")}k`;
  return n.toLocaleString();
}

export interface SegmentedOption<T extends string> {
  value: T;
  label: ReactNode;
  icon?: ReactNode;
  /** 右侧计数（等宽数字） */
  count?: number;
  disabled?: boolean;
}

export function Segmented<T extends string>({ value, onChange, options, size = "md", block = false, className = "", disabled = false, "aria-label": ariaLabel }: {
  value: T;
  onChange: (v: T) => void;
  options: Array<SegmentedOption<T>>;
  size?: "sm" | "md";
  block?: boolean;
  className?: string;
  /** 整体禁用：所有选项不可点、不可聚焦（与 option 级 disabled 叠加） */
  disabled?: boolean;
  "aria-label"?: string;
}) {
  const trackRef = useRef<HTMLDivElement>(null);
  const [thumb, setThumb] = useState<{ x: number; w: number } | null>(null);

  useLayoutEffect(() => {
    const track = trackRef.current;
    if (!track) return;
    const measure = () => {
      const btn = track.querySelector<HTMLButtonElement>(`button[data-value="${CSS.escape(value)}"]`);
      if (!btn) { setThumb(null); return; }
      setThumb({ x: btn.offsetLeft, w: btn.offsetWidth });
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(track);
    return () => ro.disconnect();
  }, [value, options.length]);

  return (
    <div
      ref={trackRef}
      className={`ui-seg seg-${size}${block ? " seg-block" : ""} ${className}`.trim()}
      role="tablist"
      aria-label={ariaLabel}
    >
      {thumb && (
        <span
          className="ui-seg-thumb"
          aria-hidden
          style={{ transform: `translateX(${thumb.x}px)`, width: thumb.w }}
        />
      )}
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          role="tab"
          data-value={o.value}
          aria-selected={o.value === value}
          disabled={disabled || o.disabled}
          className={"ui-seg-item" + (o.value === value ? " is-active" : "")}
          onClick={() => { if (disabled) return; if (o.value !== value) onChange(o.value); }}
        >
          {o.icon}
          <span className="ui-seg-label">{o.label}</span>
          {o.count != null && <span className="ui-seg-count">{formatCount(o.count)}</span>}
        </button>
      ))}
    </div>
  );
}
