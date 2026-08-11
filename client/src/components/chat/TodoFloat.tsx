/** 右上角任务卡（v10/v12，codex 风格）：替代旧 TodoRing。
 * - 当前会话有任务时显示；只展示「最新 turn」的任务——新任务拆分完成后自动清理历史任务步骤。
 * - 标题栏「折叠右侧面板按钮」左侧的折叠按钮控制整卡显隐（点击后整个卡片消失）。
 * - v12: 任务行点击展开步骤详情（描述/验收标准/失败原因）；子代理任务以子步骤缩进展示，
 *   与主步骤（parent_task_id）形成拆解层级。
 */
import { useMemo, useState } from "react";
import { useChatStore } from "../../store/chat";
import { usePanelStore } from "../../store/panel";
import type { TaskOut } from "../../api/client";
import { IconCheck, IconCheckSquare, IconX, IconPause, IconChevronDown, IconChevronUp } from "../icons";

export function TodoFloat() {
  const tasks = useChatStore((s) => s.tasks);
  const openPanel = usePanelStore((s) => s.openPanel);
  const openTab = usePanelStore((s) => s.openTab);
  const expanded = usePanelStore((s) => s.expanded);
  const taskCardVisible = usePanelStore((s) => s.taskCardVisible);
  const toggleTaskCard = usePanelStore((s) => s.toggleTaskCard);
  // v12: 展开详情的任务 id（点击任务行切换）
  const [detailOpen, setDetailOpen] = useState<number | null>(null);

  // v9: 只展示「最新 turn」的任务——新任务开始后右上角卡片自动清理历史任务步骤，
  // 聚焦当前任务的拆分步骤与执行进度。
  const currentTurnTasks = useMemo(() => {
    const withTurn = tasks.filter((t: TaskOut) => t.turn_id != null);
    if (withTurn.length === 0) return tasks;
    const latest = Math.max(...withTurn.map((t) => t.turn_id as number));
    return tasks.filter((t) => t.turn_id === latest);
  }, [tasks]);

  const stats = useMemo(() => {
    const total = currentTurnTasks.length;
    // v12: cancelled 独立计数（回滚/取消后不再误计为已完成）
    const done = currentTurnTasks.filter((t: TaskOut) => t.status === "done").length;
    const cancelled = currentTurnTasks.filter((t: TaskOut) => t.status === "cancelled").length;
    const running = currentTurnTasks.filter((t: TaskOut) => t.status === "running" || t.status === "in_progress").length;
    return { total, done, cancelled, running };
  }, [currentTurnTasks]);

  // v12: 子任务（parent 在本列表内）不计入步骤序号，作为父步骤的子项缩进展示
  const isChild = useMemo(() => {
    const ids = new Set(currentTurnTasks.map((t) => t.id));
    const set = new Set<number>();
    currentTurnTasks.forEach((t) => {
      if (t.parent_task_id != null && t.id !== t.parent_task_id && ids.has(t.parent_task_id)) set.add(t.id);
    });
    return set;
  }, [currentTurnTasks]);

  // 折叠（标题栏按钮/卡片关闭按钮）后整个卡片消失
  if (!taskCardVisible) return null;
  if (stats.total === 0) return null;

  const pct = stats.total > 0 ? Math.round((stats.done / stats.total) * 100) : 0;

  const handleClick = () => {
    if (!expanded) {
      openPanel();
      openTab("task-summary");
    }
  };

  const stepNo = (t: TaskOut, idx: number) => (isChild.has(t.id) ? null : idx + 1);

  return (
    <div className="todo-float">
      <style>{`
        .todo-float-item.child { padding-left: 26px; }
        .todo-float-item.child .todo-item-step { display: none; }
        .todo-float-item.child .todo-item-text { font-size: 11px; color: var(--text-3); }
        .todo-item-detail {
          padding: 2px 0 2px 26px; font-size: 11px; color: var(--text-3);
          line-height: 1.5; cursor: default;
        }
        .todo-item-detail b { color: var(--text-2); font-weight: 600; }
        .todo-item-detail .note { color: var(--danger); }
        .todo-item-chevron { display: inline-flex; margin-left: 2px; color: var(--text-3); }
        .todo-float-item.child .todo-item-chevron { display: none; }
      `}</style>
      <div className="todo-float-head">
        <span className="todo-float-title">
          <IconCheckSquare size={12} />
          任务进度
        </span>
        <span className="todo-float-count">{stats.done}/{stats.total}</span>
        {stats.cancelled > 0 && <span className="todo-float-cancelled">{stats.cancelled} 已取消</span>}
        <div className="todo-float-progress">
          <div className="todo-float-progress-fill" style={{ width: `${pct}%` }} />
        </div>
        <button
          className="todo-float-close"
          onClick={toggleTaskCard}
          title="隐藏任务卡"
        >
          <IconX size={12} />
        </button>
      </div>
      <div className="todo-float-list">
        {currentTurnTasks.map((t: TaskOut, idx: number) => {
          // in_progress -> running：统一"进行中"展示
          const st = t.status === "in_progress" ? "running" : t.status;
          const open = detailOpen === t.id;
          return (
            <div key={t.id}>
              <div
                className={`todo-float-item ${st}${isChild.has(t.id) ? " child" : ""}`}
                onClick={() => {
                  handleClick();
                  setDetailOpen(open ? null : t.id);
                }}
              >
                <span className={`status-icon ${st}`}>
                  {st === "done" && <IconCheck size={12} />}
                  {st === "running" && <span className="todo-spinner" />}
                  {st === "failed" && <IconX size={11} />}
                  {st === "cancelled" && <IconPause size={10} />}
                </span>
                <span className="todo-item-step">{stepNo(t, idx) != null ? `#${stepNo(t, idx)}` : ""}</span>
                <span className="todo-item-text">{t.title || t.description || `任务 ${t.id}`}</span>
                {!isChild.has(t.id) && (
                  <span className="todo-item-chevron">{open ? <IconChevronUp size={11} /> : <IconChevronDown size={11} />}</span>
                )}
              </div>
              {open && (
                <div className="todo-item-detail" onClick={(e) => e.stopPropagation()}>
                  {t.description && <div><b>描述</b>：{t.description}</div>}
                  {t.acceptance_criteria && <div><b>验收标准</b>：{t.acceptance_criteria}</div>}
                  {t.note && <div className="note"><b>说明</b>：{t.note}</div>}
                  {!t.description && !t.acceptance_criteria && !t.note && <div>（无更多详情）</div>}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}