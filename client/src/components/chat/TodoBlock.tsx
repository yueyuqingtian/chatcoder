/** 对话内嵌清单卡片（Codex 风格 checkbox 清单）。
 * 展示模型通过 todo_write 维护的执行清单投影；
 * 清单已持久化到任务区块时由悬浮任务卡展示，本组件隐藏避免重复。
 * P0 修复 C：即使落库失败（persisted=false）也只以统一卡片式折叠展示兜底，
 * 与 TodoFloat 视觉一致（圆角卡片 + 进度条 + 折叠），不再裸显原始清单。 */
import { useState } from "react";
import { useChatStore } from "../../store/chat";
import { useUiStore } from "../../store/ui";
import { IconCheck, IconChevronDown, IconChevronRight } from "../icons";

export function TodoBlock() {
  const todos = useChatStore((s) => s.todos);
  const persisted = useChatStore((s) => s.todoPersisted);
  // v1.1: 常规设置"消息流显示 todos"关闭时不渲染
  const showTodos = useUiStore((s) => s.showTodos);
  const [collapsed, setCollapsed] = useState(false);

  if (!showTodos || !todos || todos.length === 0 || persisted) return null;

  const doneCount = todos.filter((t) => t.status === "completed").length;
  const percent = todos.length ? Math.round((doneCount / todos.length) * 100) : 0;

  return (
    <div className="todo-block" role="status" aria-live="polite">
      <button className="todo-block-head" type="button" onClick={() => setCollapsed((v) => !v)} title="折叠/展开">
        <span className="todo-block-title">
          {collapsed ? <IconChevronRight size={11} /> : <IconChevronDown size={11} />}
          执行清单
        </span>
        <span className="todo-block-count">{doneCount}/{todos.length}</span>
      </button>
      {!collapsed ? (
        <>
          <div className="todo-block-progress">
            <div className="todo-block-progress-fill" style={{ width: `${percent}%` }} />
          </div>
          <div className="todo-block-list">
            {todos.map((todo, index) => (
              <div className={`todo-block-item ${todo.status}`} key={`${index}-${todo.content}`}>
                <span className={`todo-block-box ${todo.status}`}>
                  {todo.status === "completed" ? <IconCheck size={10} /> : null}
                  {todo.status === "in_progress" ? <span className="todo-spinner" /> : null}
                </span>
                <span className="todo-block-text">
                  {todo.status === "in_progress" && todo.activeForm ? todo.activeForm : todo.content}
                </span>
              </div>
            ))}
          </div>
        </>
      ) : null}
    </div>
  );
}
