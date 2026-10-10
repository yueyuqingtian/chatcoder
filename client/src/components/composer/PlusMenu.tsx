/** 输入框「+」浮窗（plan-20261009-ui U2，参考图4）。
 *
 * 结构：执行模式（智能体 / 计划 / 只读 + 自定义）→ 目标 → 分隔 → 插件 ›、技能 ›、连接器 ›。
 * 交互：
 *  - 「+」按钮打开时图标弹性旋转成 ×（IconPlusX）；工具条上的模式胶囊也能打开同一个浮窗；
 *  - 树节点悬停 120ms 打开右侧二级浮窗（ExtensionPicker），离开有 220ms 宽限期，穿过两层之间的间隙不误关；
 *    点击 / 方向键右也能打开（键盘与触控板用户）；
 *  - Esc 先关二级，再关一级；点外部整体关闭。 */
import { useEffect, useRef, useState, type ReactNode, type RefObject } from "react";
import { Popover, Badge } from "../ui";
import { IconCheck, IconChevronRight, IconTarget, IconX } from "../icons";
import { ExtensionPicker, extKindIcon, type ExtItem } from "./ExtensionPicker";
import type { ExtKind } from "../../store/navIntent";

export interface ModeOption {
  name: string;
  label: string;
  desc: string;
  icon: ReactNode;
  /** 右侧快捷键提示（计划模式：Shift+Tab） */
  kbd?: string;
}

const EXT_ROWS: Array<{ kind: ExtKind; label: string }> = [
  { kind: "plugin", label: "插件" },
  { kind: "skill", label: "技能" },
  { kind: "connector", label: "连接器" },
];

export function PlusMenu({
  open, onClose, anchorRefs, modes, mode, onModeChange,
  goal, onGoal, onCancelGoal, exts, onPickExt,
}: {
  open: boolean;
  onClose: () => void;
  /** 不视为「外部」的触发器（+ 按钮与模式胶囊） */
  anchorRefs: Array<RefObject<HTMLElement | null>>;
  modes: ModeOption[];
  mode: string;
  onModeChange: (name: string) => void;
  goal: { visible: boolean; active: boolean; text: string | null };
  onGoal: () => void;
  onCancelGoal: () => void;
  exts: Record<ExtKind, ExtItem[]>;
  onPickExt: (kind: ExtKind, item: ExtItem) => void;
}) {
  const [sub, setSub] = useState<ExtKind | null>(null);
  const openTimer = useRef<number | null>(null);
  const closeTimer = useRef<number | null>(null);

  const clearTimers = () => {
    if (openTimer.current) { window.clearTimeout(openTimer.current); openTimer.current = null; }
    if (closeTimer.current) { window.clearTimeout(closeTimer.current); closeTimer.current = null; }
  };
  useEffect(() => { if (!open) { clearTimers(); setSub(null); } }, [open]);
  useEffect(() => () => clearTimers(), []);

  const hoverRow = (kind: ExtKind) => {
    if (closeTimer.current) { window.clearTimeout(closeTimer.current); closeTimer.current = null; }
    if (sub === kind) return;
    if (openTimer.current) window.clearTimeout(openTimer.current);
    // 已有二级浮窗时切换要快（用户在三行之间移动），首次打开稍等以免路过误开
    openTimer.current = window.setTimeout(() => setSub(kind), sub ? 40 : 120);
  };
  const leaveRow = () => {
    if (openTimer.current) { window.clearTimeout(openTimer.current); openTimer.current = null; }
    if (closeTimer.current) window.clearTimeout(closeTimer.current);
    closeTimer.current = window.setTimeout(() => setSub(null), 220);
  };

  return (
    <Popover
      open={open}
      onClose={onClose}
      placement="top-start"
      className="cc-plus-pop"
      role="menu"
      aria-label="输入选项"
      ignoreRefs={anchorRefs}
    >
      <div className="pop-label">执行模式</div>
      {modes.map((m) => {
        const active = m.name === mode;
        return (
          <button
            key={m.name}
            type="button"
            role="menuitemradio"
            aria-checked={active}
            className={"pop-item cc-mode-item" + (active ? " is-selected" : "")}
            onMouseEnter={() => setSub(null)}
            onClick={() => { onModeChange(m.name); onClose(); }}
          >
            <span className="pop-item-icon">{m.icon}</span>
            <span className="pop-item-main">
              <span className="pop-item-title">{m.label}</span>
              <span className="pop-item-desc">{m.desc}</span>
            </span>
            <span className="pop-item-end">
              {m.kbd && !active && <kbd className="pop-kbd">{m.kbd}</kbd>}
              {active && <span className="pop-item-check"><IconCheck size={14} /></span>}
            </span>
          </button>
        );
      })}

      {goal.visible && (
        <div className="cc-goal-row" onMouseEnter={() => setSub(null)}>
          <button type="button" role="menuitem" className="pop-item cc-mode-item" onClick={() => { onGoal(); onClose(); }}>
            <span className="pop-item-icon"><IconTarget size={16} /></span>
            <span className="pop-item-main">
              <span className="pop-item-title">{goal.active ? "修改目标" : "目标"}</span>
              <span className="pop-item-desc">{goal.active && goal.text ? goal.text : "设定目标，每轮结束自动续跑直到达成"}</span>
            </span>
            <span className="pop-item-end">{goal.active && <Badge size="xs" tone="success">进行中</Badge>}</span>
          </button>
          {goal.active && (
            <button type="button" className="ui-icon-btn cc-goal-cancel" aria-label="取消目标" onClick={() => { onCancelGoal(); onClose(); }}>
              <IconX size={14} />
            </button>
          )}
        </div>
      )}

      <div className="pop-sep" />

      {EXT_ROWS.map(({ kind, label }) => {
        const items = exts[kind];
        const isOpen = sub === kind;
        return (
          <div key={kind} className="cc-ext-row" onMouseEnter={() => hoverRow(kind)} onMouseLeave={leaveRow}>
            <button
              type="button"
              role="menuitem"
              aria-haspopup="dialog"
              aria-expanded={isOpen}
              className={"pop-item" + (isOpen ? " is-hl" : "")}
              onClick={() => setSub(isOpen ? null : kind)}
              onKeyDown={(e) => { if (e.key === "ArrowRight") { e.preventDefault(); setSub(kind); } }}
            >
              <span className="pop-item-icon">{extKindIcon(kind, 16)}</span>
              <span className="pop-item-title">{label}</span>
              <span className="pop-item-end">
                {items.length > 0 && <span className="cc-ext-count">{items.length}</span>}
                <IconChevronRight size={14} />
              </span>
            </button>
            <ExtensionPicker
              open={open && isOpen}
              kind={kind}
              items={items}
              onPick={(it) => { onPickExt(kind, it); onClose(); }}
              onClose={() => setSub(null)}
              onLeaveMenu={onClose}
            />
          </div>
        );
      })}
    </Popover>
  );
}
