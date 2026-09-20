/** Slider —— 统一滑块（Radix Slider，键盘可达）
 *
 * 替换原先散落在 AppearancePanel(7 处) / GeneralPanel(1 处) 的原生 range：
 *  - 轨道 4px、thumb 14px，与 Checkbox/Switch 同一视觉密度；
 *  - 支持 ←→ / Home / End 键盘操作（Radix 原生）；
 *  - showValue 时右侧常显数值（min-width 40px、tabular-nums），避免拖动时宽度抖动。
 *
 * plan-282-1421（图1 修复）：`.ui-slider-row` 必须有**确定宽度**。
 * Radix 的 Root 是 `flex:1`，若行容器宽度由 flex 上下文决定、而外层控件槽又是
 * `flex-shrink:0` 且不含宽度声明，轨道会塌陷到最小尺寸（此前只剩一个圆点）。
 * 现由 `--slider-track-w` 给出确定宽度；需要占满父级时传 `fluid`。
 */
import * as RadixSlider from "@radix-ui/react-slider";
import type { ReactNode } from "react";

interface SliderProps {
  value: number;
  onChange: (v: number) => void;
  min?: number;
  max?: number;
  step?: number;
  disabled?: boolean;
  /** 是否在右侧常显数值 */
  showValue?: boolean;
  /** 数值格式化（如 `${v}px` / `v.toFixed(2)`） */
  format?: (v: number) => ReactNode;
  /** 占满父级宽度（默认固定 --slider-track-w，避免在 flex 上下文塌陷） */
  fluid?: boolean;
  /** 无障碍标签 */
  "aria-label"?: string;
  className?: string;
}

export function Slider({
  value,
  onChange,
  min = 0,
  max = 100,
  step = 1,
  disabled = false,
  showValue = true,
  format,
  fluid = false,
  className = "",
  ...rest
}: SliderProps) {
  return (
    <div className={`ui-slider-row${fluid ? " is-fluid" : ""} ${className}`.trim()}>
      <RadixSlider.Root
        className="ui-slider-root"
        value={[value]}
        min={min}
        max={max}
        step={step}
        disabled={disabled}
        onValueChange={(v) => onChange(v[0])}
        aria-label={rest["aria-label"]}
      >
        <RadixSlider.Track className="ui-slider-track">
          <RadixSlider.Range className="ui-slider-range" />
        </RadixSlider.Track>
        <RadixSlider.Thumb className="ui-slider-thumb" />
      </RadixSlider.Root>
      {showValue && (
        <span className="ui-slider-value">{format ? format(value) : value}</span>
      )}
    </div>
  );
}
