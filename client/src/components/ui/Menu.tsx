/** Menu —— 统一菜单（Radix DropdownMenu，plan-282-1416）
 *
 * 收敛此前散落的裸 `.context-menu`（Sidebar / TitleBar 手写）与 `ui/ContextMenu`：
 *  - 定位/碰撞翻转/键盘导航/Esc 关闭由 Radix 承担；
 *  - 高亮走 data-highlighted 底色（不出现品牌色描边，符合 focus.css 约定）；
 *  - 支持 danger 项、分组标签、分隔线、勾选态。
 */
import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import type { ReactNode } from "react";

export interface MenuEntry {
  /** 唯一 key（缺省用下标） */
  key?: string;
  label: ReactNode;
  icon?: ReactNode;
  onSelect?: () => void;
  danger?: boolean;
  disabled?: boolean;
  /** 勾选态（当前选中项） */
  active?: boolean;
  /** 分组标签：该项作为不可点击的组标题渲染 */
  groupLabel?: boolean;
  /** 该项之后插入分隔线 */
  divider?: boolean;
}

interface MenuProps {
  trigger: ReactNode;
  entries: MenuEntry[];
  align?: "start" | "center" | "end";
  side?: "top" | "right" | "bottom" | "left";
  sideOffset?: number;
  /** 触发器是否作为子元素包裹（默认 asChild） */
  className?: string;
}

export function Menu({ trigger, entries, align = "end", side = "bottom", sideOffset = 4, className = "" }: MenuProps) {
  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>{trigger}</DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content className={`ui-menu-content ${className}`.trim()} align={align} side={side} sideOffset={sideOffset}>
          {entries.map((e, i) => {
            const k = e.key ?? String(i);
            if (e.groupLabel) return <DropdownMenu.Label key={k} className="ui-menu-label">{e.label}</DropdownMenu.Label>;
            return (
              <div key={k}>
                <DropdownMenu.Item
                  className={[
                    "ui-menu-item",
                    e.danger ? "is-danger" : "",
                    e.active ? "is-active" : "",
                  ].filter(Boolean).join(" ")}
                  disabled={e.disabled}
                  onSelect={e.onSelect}
                >
                  {e.icon}
                  <span style={{ minWidth: 0 }}>{e.label}</span>
                </DropdownMenu.Item>
                {e.divider && <DropdownMenu.Separator className="ui-menu-sep" />}
              </div>
            );
          })}
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}
