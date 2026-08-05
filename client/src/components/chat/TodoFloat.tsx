/** 右上角悬浮待办窗（v4 r2）：替代旧 TodoRing。
 * 当前会话有任务时显示；与面板开关按钮分离。
 */
import { useMemo, useState } from "react";
import { useChatStore } from "../../store/chat";
import { usePanelStore } from "../../store/panel";
import type { TaskOut } from "../../api/client";
import { IconCheck, IconChevronDown, IconChevronUp } from "../icons";

export function TodoFloat() {
  const { tasks } = useChatStore();
  const { openPanel, openTab, expanded } = usePanelStore();
  const [collapsed, setCollapsed] = useState(false);

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
    return { total, done };
  }, [currentTurnTasks]);

  // 无任务不渲染
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
      <div className="todo-float-head" onClick={() => setCollapsed(!collapsed)}>
        <span>任务进度 {stats.done}/{stats.total}</span>
        <div className="todo-float-progress">
          <div className="todo-float-progress-fill" style={{ width: `${pct}%` }} />
        </div>
        {collapsed ? <IconChevronDown size={12} /> : <IconChevronUp size={12} />}
      </div>
      {!collapsed && (
        <div className="todo-float-list">
          {currentTurnTasks.map((t: TaskOut) => {
            // in_progress -> running：统一"进行中"展示
            const st = t.status === "in_progress" ? "running" : t.status;
            return (
              <div
                key={t.id}
                className="todo-float-item"
                onClick={handleClick}
              >
                <span className={`status-icon ${st}`}>
                  {st === "done" && <IconCheck size={12} />}
                  {st === "running" && <span className="todo-spinner">◌</span>}
                  {st === "failed" && "✗"}
                  {st === "blocked" && "⏸"}
                </span>
                <span className="todo-item-text">{t.title || t.description || `任务 ${t.id}`}</span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
