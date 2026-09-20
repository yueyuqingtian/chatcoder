/** Checkbox —— 统一复选框（Radix Checkbox，支持三态）
 *
 * 替换原先散落的原生 checkbox（NavPages / MemoryPanel / ModelsPanel×2 /
 * PolicyPanel×3 / SubagentsPanel）与特例类 .policy-checkbox：
 *  - 16×16、圆角 --r-sm(4)、选中走 --accent；
 *  - 支持 indeterminate（全选/半选）；
 *  - 键盘 Space 切换（Radix 原生），label 与勾选框整体可点。
 */
import * as RadixCheckbox from "@radix-ui/react-checkbox";
import type { ReactNode } from "react";
import { IconCheck, IconMinus } from "../icons";

interface CheckboxProps {
  checked: boolean;
  onChange: (v: boolean) => void;
  /** 三态：true=全选，false=未选，"indeterminate"=半选 */
  indeterminate?: boolean;
  label?: ReactNode;
  disabled?: boolean;
  className?: string;
  "aria-label"?: string;
}

export function Checkbox({
  checked,
  onChange,
  indeterminate = false,
  label,
  disabled = false,
  className = "",
  ...rest
}: CheckboxProps) {
  const state = indeterminate ? "indeterminate" : checked;
  const box = (
    <RadixCheckbox.Root
      className="ui-checkbox"
      checked={state}
      disabled={disabled}
      onCheckedChange={(v) => onChange(v === true)}
      aria-label={rest["aria-label"]}
    >
      <RadixCheckbox.Indicator>
        {indeterminate ? <IconMinus size={11} strokeWidth={2.5} /> : <IconCheck size={11} strokeWidth={2.5} />}
      </RadixCheckbox.Indicator>
    </RadixCheckbox.Root>
  );

  if (!label) return <span className={`${className}`.trim()}>{box}</span>;

  return (
    <label className={`ui-check-row${disabled ? " is-disabled" : ""} ${className}`.trim()}>
      {box}
      <span className="ui-check-label">{label}</span>
    </label>
  );
}
