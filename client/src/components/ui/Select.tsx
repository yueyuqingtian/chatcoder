/** Select —— 统一下拉选择
 *
 * 两种导出：
 *  - `Select`（Radix）：推荐用法，支持 Portal/定位/键盘/搜索式高亮，与 Input 同高同圆角；
 *  - `NativeSelect`：原生 select 兜底（极简场景或需要浏览器原生行为时），外观与 Select 对齐。
 *
 * 空值约定（重要）：
 *  Radix 明确禁止 `<Select.Item value="">`（空串是其内部"清除选择/显示 placeholder"的保留值，
 *  传入会直接抛错 "must have a value prop that is not an empty string"）。
 *  而业务侧习惯用 `""` 表示"未选择/继承默认"，因此这里统一做**哨兵值转换**：
 *  对外 value/onChange 始终使用 `""` 语义，对内自动映射为 EMPTY_SENTINEL。
 *
 * 兼容：旧代码 `className="ui-select"` 由 components.css 的 .ui-select 规则承载（过渡期）。
 */
import * as RadixSelect from "@radix-ui/react-select";
import type { SelectHTMLAttributes } from "react";
import { IconCheck, IconChevronDown } from "../icons";
import type { ControlSize } from "./Input";

/** 空串哨兵：Radix 不接受空串 item value，用它代替并在回调时还原为 "" */
const EMPTY_SENTINEL = "__cc_empty__";

export interface SelectOption {
  value: string;
  label: string;
  disabled?: boolean;
}

interface SelectProps {
  value: string;
  onChange: (v: string) => void;
  options: SelectOption[];
  placeholder?: string;
  size?: ControlSize;
  disabled?: boolean;
  /** 触发器额外类名（宽度等布局微调） */
  className?: string;
  /** 触发器内联样式（如 minWidth 限定） */
  style?: React.CSSProperties;
  "aria-label"?: string;
}

export function Select({
  value,
  onChange,
  options,
  placeholder = "请选择",
  size = "md",
  disabled = false,
  className = "",
  style,
  ...rest
}: SelectProps) {
  // 存在 value="" 的选项时（如"跟随主代理"/"继承系统字体"）用哨兵承载；
  // 否则直接传 ""（Radix 将空串视为"无选择"，自动显示 placeholder），
  // 注意不能传 undefined——那会让 Root 退化为非受控组件，父级清空选择无法同步。
  const hasEmptyOption = options.some((o) => o.value === "");
  const radixValue = value === "" && hasEmptyOption ? EMPTY_SENTINEL : value;

  return (
    <RadixSelect.Root
      value={radixValue}
      onValueChange={(v) => onChange(v === EMPTY_SENTINEL ? "" : v)}
      disabled={disabled}
    >
      <RadixSelect.Trigger
        className={`ui-select-trigger${size !== "md" ? ` size-${size}` : ""} ${className}`.trim()}
        aria-label={rest["aria-label"]}
        style={style}
      >
        <RadixSelect.Value className="ui-select-value" placeholder={placeholder} />
        <RadixSelect.Icon className="ui-select-icon">
          <IconChevronDown size={12} />
        </RadixSelect.Icon>
      </RadixSelect.Trigger>
      <RadixSelect.Portal>
        <RadixSelect.Content className="ui-select-content" position="popper" sideOffset={4}>
          <RadixSelect.Viewport>
            {options.map((o, i) => (
              <RadixSelect.Item
                key={o.value === "" ? `${EMPTY_SENTINEL}-${i}` : o.value}
                className="ui-select-item"
                value={o.value === "" ? EMPTY_SENTINEL : o.value}
                disabled={o.disabled}
              >
                <RadixSelect.ItemIndicator className="ui-select-item-indicator">
                  <IconCheck size={11} />
                </RadixSelect.ItemIndicator>
                <RadixSelect.ItemText>{o.label}</RadixSelect.ItemText>
              </RadixSelect.Item>
            ))}
          </RadixSelect.Viewport>
        </RadixSelect.Content>
      </RadixSelect.Portal>
    </RadixSelect.Root>
  );
}

/** 原生 select 兜底：外观与 Select 触发器一致（同高同圆角同五态） */
export function NativeSelect({ className = "", children, ...props }: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select className={`ui-native-select ${className}`.trim()} {...props}>
      {children}
    </select>
  );
}
