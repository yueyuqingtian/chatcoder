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

  const stats = useMemo(() => {
    const total = tasks.length;
    const done = tasks.filter((t: TaskOut) => t.status === "done" || t.status === "cancelled").length;
    return { total, done };
  }, [tasks]);

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
          {tasks.map((t: TaskOut) => (
            <div
              key={t.id}
              className="todo-float-item"
              onClick={handleClick}
            >
              <span className={`status-icon ${t.status}`}>
                {t.status === "done" && <IconCheck size={12} />}
                {t.status === "failed" && "✗"}
                {t.status === "blocked" && "⏸"}
              </span>
              <span className="todo-item-text">{t.title || t.description || `任务 ${t.id}`}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
