/** RadioGroup / Radio —— 统一单选框（Radix RadioGroup，方向键切换）
 *
 * 提供两种用法：
 *  - `<RadioGroup options value onChange />`：一次渲染一组；
 *  - `<Radio ...>`：单元格（供自定义排布时使用，需被 RadioGroup 包裹）。
 */
import * as RadixRadio from "@radix-ui/react-radio-group";
import type { ReactNode } from "react";

export interface RadioOption {
  value: string;
  label: ReactNode;
  disabled?: boolean;
  /** 描述行（12px/--text-3），可选 */
  desc?: ReactNode;
}

interface RadioGroupProps {
  value: string;
  onChange: (v: string) => void;
  options: RadioOption[];
  /** 排列方向，默认竖排 */
  orientation?: "horizontal" | "vertical";
  disabled?: boolean;
  className?: string;
  "aria-label"?: string;
}

export function RadioGroup({
  value,
  onChange,
  options,
  orientation = "vertical",
  disabled = false,
  className = "",
  ...rest
}: RadioGroupProps) {
  return (
    <RadixRadio.Root
      className={`ui-radio-group ${className}`.trim()}
      value={value}
      disabled={disabled}
      onValueChange={onChange}
      orientation={orientation}
      aria-label={rest["aria-label"]}
      style={{
        display: "flex",
        flexDirection: orientation === "horizontal" ? "row" : "column",
        gap: orientation === "horizontal" ? "var(--sp-5)" : "var(--sp-3)",
      }}
    >
      {options.map((o) => (
        <label key={o.value} className={`ui-radio-row${o.disabled ? " is-disabled" : ""}`}>
          <RadixRadio.Item className="ui-radio" value={o.value} disabled={o.disabled}>
            <RadixRadio.Indicator style={{ display: "block", width: "100%", height: "100%", borderRadius: "50%" }} />
          </RadixRadio.Item>
          <span style={{ minWidth: 0 }}>
            <span className="ui-check-label">{o.label}</span>
            {o.desc && <span className="ui-card-row-desc" style={{ display: "block" }}>{o.desc}</span>}
          </span>
        </label>
      ))}
    </RadixRadio.Root>
  );
}
