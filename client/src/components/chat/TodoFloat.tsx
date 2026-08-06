/** 右上角任务卡（v10，codex 风格）：替代旧 TodoRing。
 * - 当前会话有任务时显示；只展示「最新 turn」的任务——新任务拆分完成后自动清理历史任务步骤。
 * - 标题栏「折叠右侧面板按钮」左侧的折叠按钮控制整卡显隐（点击后整个卡片消失）。
 */
import { useMemo } from "react";
import { useChatStore } from "../../store/chat";
import { usePanelStore } from "../../store/panel";
import type { TaskOut } from "../../api/client";
import { IconCheck, IconCheckSquare, IconX, IconPause } from "../icons";

export function TodoFloat() {
  const tasks = useChatStore((s) => s.tasks);
  const openPanel = usePanelStore((s) => s.openPanel);
  const openTab = usePanelStore((s) => s.openTab);
  const expanded = usePanelStore((s) => s.expanded);
  const taskCardVisible = usePanelStore((s) => s.taskCardVisible);
  const toggleTaskCard = usePanelStore((s) => s.toggleTaskCard);

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
    const done = currentTurnTasks.filter((t: TaskOut) => t.status === "done" || t.status === "cancelled").length;
    const running = currentTurnTasks.filter((t: TaskOut) => t.status === "running" || t.status === "in_progress").length;
    return { total, done, running };
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

  return (
    <div className="todo-float">
      <div className="todo-float-head">
        <span className="todo-float-title">
          <IconCheckSquare size={12} />
          任务进度
        </span>
        <span className="todo-float-count">{stats.done}/{stats.total}</span>
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
          return (
            <div
              key={t.id}
              className={`todo-float-item ${st}`}
              onClick={handleClick}
            >
              <span className={`status-icon ${st}`}>
                {st === "done" && <IconCheck size={12} />}
                {st === "running" && <span className="todo-spinner" />}
                {st === "failed" && <IconX size={11} />}
                {st === "blocked" && <IconPause size={10} />}
              </span>
              <span className="todo-item-step">#{idx + 1}</span>
              <span className="todo-item-text">{t.title || t.description || `任务 ${t.id}`}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
