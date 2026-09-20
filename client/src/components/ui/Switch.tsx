/** Switch —— 统一开关（Radix Switch）
 *
 * 签名与旧实现保持兼容：`checked` + `onChange(v: boolean)`，
 * 因此 settings/shared.tsx 的 Sw、NavPages 的 SwitchRow 可直接改为 re-export，
 * 消除此前三套并行实现（ui/Switch.tsx、.ui-switch、.sp-switch）。
 */
import * as RadixSwitch from "@radix-ui/react-switch";

interface SwitchProps {
  checked: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
  className?: string;
  "aria-label"?: string;
}

export function Switch({ checked, onChange, disabled = false, className = "", ...rest }: SwitchProps) {
  return (
    <RadixSwitch.Root
      className={`ui-switch-root ${className}`.trim()}
      checked={checked}
      disabled={disabled}
      onCheckedChange={onChange}
      aria-label={rest["aria-label"]}
    >
      <RadixSwitch.Thumb className="ui-switch-thumb" />
    </RadixSwitch.Root>
  );
}
