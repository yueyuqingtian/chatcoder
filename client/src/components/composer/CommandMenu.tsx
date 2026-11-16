import { Fragment, forwardRef, type ReactNode } from "react";
import { Button, Tooltip } from "../ui";
import { IconBox, IconPlus, IconPuzzle } from "../icons";

export interface CommandMenuItem {
  kind: "cmd" | "skill" | "mcp" | "plugin" | "microapp";
  key: string;
  label: string;
  description: string;
  icon: ReactNode;
}

const GROUP_LABELS = { cmd: "快捷命令", skill: "技能（$）", mcp: "连接器", plugin: "插件", microapp: "微应用" };

/** plan-20261104-command-menu：复用 @ 的浮窗视觉与滚动结构，命令执行仍由输入框处理。
 *  plan-693-2868（P1）：「微应用」分组列已安装微应用；一个都没装时给空态与两个出口——
 *  ①「让 AI 创建」把创建诉求写进输入框（不替用户按发送）；②「去拓展里安装」跳拓展页。
 *  选中某个微应用只注入 chip，**不打开右侧面板**（产品要求：注入 ≠ 打开界面）。 */
export const CommandMenu = forwardRef<HTMLDivElement, {
  items: CommandMenuItem[];
  index: number;
  listId: string;
  disabledMcpCount: number;
  /** 已安装微应用为空：在列表末尾渲染「微应用」分组空态 */
  microAppEmpty: boolean;
  onIndex: (index: number) => void;
  onPick: (item: CommandMenuItem) => void;
  onManageExtensions: () => void;
  onCreateMicroApp: () => void;
  onInstallMicroApp: () => void;
  onClose: () => void;
}>(function CommandMenu({
  items, index, listId, disabledMcpCount, microAppEmpty,
  onIndex, onPick, onManageExtensions, onCreateMicroApp, onInstallMicroApp, onClose,
}, ref) {
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
      {/* plan-693-2868（P1）：微应用分组空态——没有可选项时也要让用户知道这块存在、并能往下走。
          结构与下方「另有 N 个连接器未启用」同一套 .mention-status，不新增装饰性样式。 */}
      {microAppEmpty && <>
        <div className="mention-group-heading">微应用</div>
        <div className="mention-status">
          <span><IconBox size={14} /> 还没有微应用</span>
          <Tooltip content="把创建诉求写进输入框，由 AI 生成微应用">
            <Button size="xs" variant="subtle" icon={<IconPlus size={12} />} onClick={onCreateMicroApp}>让 AI 创建</Button>
          </Tooltip>
          <Tooltip content="打开设置 → 拓展，安装别人做好的微应用">
            <Button size="xs" icon={<IconPuzzle size={12} />} onClick={onInstallMicroApp}>去拓展里安装</Button>
          </Tooltip>
        </div>
      </>}
      {items.length === 0 && <div className="mention-status" role="status">未找到匹配的命令或拓展，试试其他关键词</div>}
    </div>
    {disabledMcpCount > 0 && <div className="mention-status">
      <span>另有 {disabledMcpCount} 个连接器未启用</span><Button size="xs" onClick={onManageExtensions}>管理拓展</Button>
    </div>}
    <div className="mention-footer">↑↓ 选择 <span>Enter / Tab 选择</span><span>Esc 关闭</span></div>
  </div>;
});
