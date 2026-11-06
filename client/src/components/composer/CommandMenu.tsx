import { Fragment, forwardRef, type ReactNode } from "react";
import { Button, Tooltip } from "../ui";

export interface CommandMenuItem {
  kind: "cmd" | "skill" | "mcp" | "plugin";
  key: string;
  label: string;
  description: string;
  icon: ReactNode;
}

const GROUP_LABELS = { cmd: "快捷命令", skill: "技能（$）", mcp: "连接器", plugin: "插件" };

/** plan-20261104-command-menu：复用 @ 的浮窗视觉与滚动结构，命令执行仍由输入框处理。 */
export const CommandMenu = forwardRef<HTMLDivElement, {
  items: CommandMenuItem[];
  index: number;
  listId: string;
  disabledMcpCount: number;
  onIndex: (index: number) => void;
  onPick: (item: CommandMenuItem) => void;
  onManageExtensions: () => void;
  onClose: () => void;
}>(function CommandMenu({ items, index, listId, disabledMcpCount, onIndex, onPick, onManageExtensions, onClose }, ref) {
  return <div className="mention-menu command-menu" role="dialog" aria-label="命令与拓展"
    onMouseDown={(e) => e.preventDefault()}
    onKeyDown={(e) => { if (e.key === "Escape") { e.stopPropagation(); onClose(); } }}>
    <div className="mention-heading"><strong>命令与拓展</strong><span>输入名称，快速查找</span></div>
    <div className="mention-list" ref={ref} role="listbox" id={listId} aria-label="命令候选">
      {items.map((item, i) => <Fragment key={`${item.kind}-${item.key}`}>
        {(i === 0 || items[i - 1].kind !== item.kind) && <div className="mention-group-heading">{GROUP_LABELS[item.kind]}</div>}
        <Tooltip content={`${item.label}\n${item.description}`}>
          <Button variant="ghost" size="sm" block role="option" aria-selected={i === index} id={`${listId}-${i}`}
            className={`mention-option${i === index ? " active" : ""}`} onMouseEnter={() => onIndex(i)} onClick={() => onPick(item)}>
            <span className="mention-option-icon">{item.icon}</span>
            <span className="mention-option-copy"><strong>{item.label}</strong><span>{item.description}</span></span>
          </Button>
        </Tooltip>
      </Fragment>)}
      {items.length === 0 && <div className="mention-status" role="status">未找到匹配的命令或拓展，试试其他关键词</div>}
    </div>
    {disabledMcpCount > 0 && <div className="mention-status">
      <span>另有 {disabledMcpCount} 个连接器未启用</span><Button size="xs" onClick={onManageExtensions}>管理拓展</Button>
    </div>}
    <div className="mention-footer">↑↓ 选择 <span>Enter / Tab 选择</span><span>Esc 关闭</span></div>
  </div>;
});
