/** 任务卡片（zcode 风格）：计划标题 + 进程计数 + 勾选清单。
 * 完成项绿色✓+删除线灰化；进行中旋转指示+加粗并显示实时工具活动；
 * 失败项红✕可展开原因并重试。 */
import { useMemo, useState } from "react";
import { useChatStore } from "../../store/chat";
import { usePanelStore } from "../../store/panel";
import type { TaskOut } from "../../api/client";
import { IconCheck, IconCheckSquare, IconPause, IconX } from "../icons";

function latestTurnTasks(tasks: TaskOut[]): TaskOut[] {
  const withTurn = tasks.filter((task) => task.turn_id != null && !task.is_hidden);
  if (withTurn.length === 0) return tasks.filter((task) => !task.is_hidden);
  const turnIds = [...new Set(withTurn.map((task) => task.turn_id as number))].sort((a, b) => b - a);
  // P0 修复 C：最新 turn 若只有 request 任务（如"继续"新建的 turn），
  // 向前找最近一个有 group/step 任务的 turn 展示，避免卡片只剩 0/1。
  const isStep = (task: TaskOut) =>
    task.kind === "step" || (task.parent_task_id != null && task.kind !== "group");
  let displayTurn = turnIds[0];
  const latestOnly = withTurn.filter((task) => task.turn_id === turnIds[0]);
  const latestHasContent = latestOnly.some(
    (task) => task.kind === "group" || isStep(task),
  );
  if (!latestHasContent) {
    for (const turnId of turnIds.slice(1)) {
      const candidates = withTurn.filter((task) => task.turn_id === turnId);
      if (candidates.some((task) => task.kind === "group" || isStep(task))) {
        displayTurn = turnId;
        break;
      }
    }
  }
  return withTurn.filter((task) => task.turn_id === displayTurn);
}

/** 当前展示的清单是否来自更早的 turn（用于"来自上一步任务"标注）。 */
function isFallbackTurn(tasks: TaskOut[], current: TaskOut[]): boolean {
  const withTurn = tasks.filter((task) => task.turn_id != null && !task.is_hidden);
  if (withTurn.length === 0 || current.length === 0) return false;
  const latestTurn = Math.max(...withTurn.map((task) => task.turn_id as number));
  return current[0].turn_id !== latestTurn;
}

function statusClass(status: string): string {
  return status === "in_progress" ? "running" : status || "pending";
}

function StatusIcon({ status }: { status: string }) {
  const state = statusClass(status);
  if (state === "done") return <span className="todo-item-status done"><IconCheck size={12} /></span>;
  if (state === "running") return <span className="todo-item-status running"><span className="todo-spinner" /></span>;
  if (state === "failed") return <span className="todo-item-status failed"><IconX size={11} /></span>;
  if (state === "cancelled") return <span className="todo-item-status cancelled"><IconPause size={10} /></span>;
  return <span className="todo-item-status pending" />;
}

/** 单个步骤行：状态图标 + 标题 + 实时活动/重试。 */
function StepRow({ step }: { step: TaskOut }) {
  const [expanded, setExpanded] = useState(false);
  const activity = useChatStore((state) => (step.agent_id ? state.agentActivity[step.agent_id] : undefined));
  const retryTask = useChatStore((state) => state.retryTask);
  const isRunning = useChatStore((state) => state.isRunning);
  const requestScrollTo = useChatStore((state) => state.requestScrollTo);
  const state = statusClass(step.status);
  const failed = state === "failed";

  return (
    <div className={`todo-float-step ${state}`}>
      <button
        className={`todo-float-item ${state}`}
        type="button"
        onClick={() => {
          // v2.2: 任务卡点击穿透——滚动到该步骤子代理的首条消息
          if (failed) setExpanded((v) => !v);
          else if (step.agent_id) requestScrollTo({ threadId: step.agent_id });
        }}
        title={failed ? "点击查看失败原因" : (step.agent_id ? "点击定位到执行消息" : step.title)}
      >
        <StatusIcon status={step.status} />
        <span className="todo-item-text">{step.title}</span>
        {state === "running" && activity ? (
          <span className="todo-item-activity">{activity}</span>
        ) : null}
        {failed ? (
          <span
            className="todo-item-retry"
            role="button"
            tabIndex={0}
            onClick={(e) => {
              e.stopPropagation();
              void retryTask(step.id);
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.stopPropagation();
                void retryTask(step.id);
              }
            }}
          >
            重试
          </span>
        ) : null}
      </button>
      {failed && expanded && step.note ? <div className="todo-item-note">{step.note}</div> : null}
      {state === "running" && !isRunning ? null : null}
    </div>
  );
}

export function TodoFloat() {
  const tasks = useChatStore((state) => state.tasks);
  const pendingSplit = useChatStore((state) => state.pendingSplit);
  const confirmTaskSplit = useChatStore((state) => state.confirmTaskSplit);
  const openPanel = usePanelStore((state) => state.openPanel);
  const openTab = usePanelStore((state) => state.openTab);
  const taskCardVisible = usePanelStore((state) => state.taskCardVisible);
  const toggleTaskCard = usePanelStore((state) => state.toggleTaskCard);

  const currentTasks = useMemo(() => latestTurnTasks(tasks), [tasks]);
  const fallbackTurn = useMemo(() => isFallbackTurn(tasks, currentTasks), [tasks, currentTasks]);
  const requestTask = useMemo(
    () => currentTasks.find((task) => task.kind === "request" || (task.kind == null && task.parent_task_id == null)),
    [currentTasks],
  );
  const isStep = (task: TaskOut) =>
    task.kind === "step" || (task.parent_task_id != null && task.kind !== "group");
  const proposedGroup = useMemo(
    () => currentTasks.find((task) => task.kind === "group" && task.status === "proposed"),
    [currentTasks],
  );
  const groups = useMemo(() => {
    const groupTasks = currentTasks.filter((task) => task.kind === "group" && task.status !== "proposed" && !task.is_hidden);
    if (groupTasks.length > 0) {
      return groupTasks.map((group) => ({
        id: group.id,
        title: group.title || "任务步骤",
        steps: currentTasks.filter((task) => isStep(task) && task.parent_task_id === group.id && !task.is_hidden),
      })).filter((group) => group.steps.length > 0);
    }
    const steps = currentTasks.filter((task) => isStep(task) && !task.is_hidden);
    if (steps.length > 0) return [{ id: "steps", title: "任务步骤", steps }];
    // 未拆分的简单请求本身就是一个可执行小点。
    return requestTask ? [{ id: "request", title: "任务步骤", steps: [requestTask] }] : [];
  }, [currentTasks]);

  const allSteps = useMemo(() => groups.flatMap((group) => group.steps), [groups]);
  const doneCount = useMemo(
    () => allSteps.filter((step) => statusClass(step.status) === "done").length,
    [allSteps],
  );

  const isProposal = Boolean(proposedGroup || pendingSplit);
  const proposalSteps = useMemo(() => {
    if (!proposedGroup) return currentTasks.filter((task) => task.status === "proposed" && (task.kind === "step" || task.parent_task_id != null));
    return currentTasks.filter((task) => task.kind === "step" && task.parent_task_id === proposedGroup.id && !task.is_hidden);
  }, [currentTasks, proposedGroup]);

  // v2.2 (对齐 zcode 3.9): 提案步骤可编辑——删除/重命名/上下移/新增，确认时回传编辑后的列表
  const [editingSteps, setEditingSteps] = useState<Array<{ task_id?: number; title: string }> | null>(null);
  const isEditing = editingSteps !== null;

  const startEditing = () => {
    setEditingSteps(proposalSteps.map((step) => ({ task_id: step.id, title: step.title })));
  };
  const moveStep = (index: number, dir: -1 | 1) => {
    setEditingSteps((prev) => {
      if (!prev) return prev;
      const next = [...prev];
      const target = index + dir;
      if (target < 0 || target >= next.length) return prev;
      [next[index], next[target]] = [next[target], next[index]];
      return next;
    });
  };
  const updateStep = (index: number, title: string) => {
    setEditingSteps((prev) => (prev ? prev.map((s, i) => (i === index ? { ...s, title } : s)) : prev));
  };
  const removeStep = (index: number) => {
    setEditingSteps((prev) => (prev ? prev.filter((_, i) => i !== index) : prev));
  };
  const addStep = () => {
    setEditingSteps((prev) => [...(prev ?? []), { title: "" }]);
  };
  const submitProposal = (accepted: boolean) => {
    if (accepted && editingSteps) {
      const steps = editingSteps.filter((s) => s.title.trim());
      void confirmTaskSplit(true, steps);
    } else {
      void confirmTaskSplit(accepted);
    }
  };

  if (!taskCardVisible || (!isProposal && groups.length === 0)) return null;

  const goSummary = () => {
    openPanel();
    openTab("task-summary");
  };

  return (
    <div className="todo-float" role="status" aria-live="polite">
      <div className="todo-float-head">
        <span className="todo-float-title">
          <IconCheckSquare size={13} />
          {isProposal ? "拆分方案待确认" : (requestTask?.title || "任务步骤")}
          {fallbackTurn && !isProposal ? <span className="todo-float-from-prev">来自上一步任务</span> : null}
        </span>
        <button className="todo-float-close" onClick={toggleTaskCard} title="隐藏任务卡" aria-label="隐藏任务卡">
          <IconX size={13} />
        </button>
      </div>

      {isProposal ? (
        <>
          <section className="todo-float-section">
            <div className="todo-float-section-title">计划</div>
            <div className="todo-float-list">
              {isEditing ? (
                editingSteps!.map((step, index) => (
                  <div className="todo-proposal-edit-row" key={`${index}-${step.task_id ?? "new"}`}>
                    <span className="todo-item-index">{index + 1}</span>
                    <input
                      className="todo-proposal-input"
                      value={step.title}
                      autoFocus={step.title === ""}
                      onChange={(e) => updateStep(index, e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") {
                          e.preventDefault();
                          (document.activeElement as HTMLElement | null)?.blur();
                        }
                      }}
                      placeholder="步骤描述…"
                    />
                    <button className="todo-proposal-op" onClick={() => moveStep(index, -1)} disabled={index === 0} title="上移" type="button">↑</button>
                    <button className="todo-proposal-op" onClick={() => moveStep(index, 1)} disabled={index === editingSteps!.length - 1} title="下移" type="button">↓</button>
                    <button className="todo-proposal-op danger" onClick={() => removeStep(index)} title="删除" type="button">×</button>
                  </div>
                ))
              ) : (
                proposalSteps.map((step, index) => (
                  <button className="todo-float-item" key={step.id} onClick={goSummary} type="button">
                    <span className="todo-item-index">{index + 1}</span>
                    <StatusIcon status={step.status} />
                    <span className="todo-item-text">{step.title}</span>
                    {step.depends_on && step.depends_on.length > 0 ? (
                      <span className="todo-item-dep">
                        ← {step.depends_on.map((d) => d + 1).join("、")}
                      </span>
                    ) : null}
                  </button>
                ))
              )}
            </div>
            {isEditing && (
              <button className="todo-proposal-add" onClick={addStep} type="button">+ 添加步骤</button>
            )}
          </section>
          <div className="todo-float-actions">
            {isEditing ? (
              <>
                <button className="todo-proposal-primary" onClick={() => submitProposal(true)} type="button">按此步骤执行</button>
                <button onClick={() => setEditingSteps(null)} type="button">取消</button>
              </>
            ) : (
              <>
                <button className="todo-proposal-primary" onClick={() => submitProposal(true)} type="button">接受拆分</button>
                <button onClick={startEditing} type="button">调整</button>
                <button onClick={() => submitProposal(false)} type="button">直接执行</button>
              </>
            )}
          </div>
        </>
      ) : (
        <>
          <div className="todo-float-progress-row">
            <span className="todo-float-progress-label">进程</span>
            <span className="todo-float-progress-count">{doneCount}/{allSteps.length}</span>
            <div className="todo-float-progress-bar">
              <div
                className="todo-float-progress-fill"
                style={{ width: allSteps.length ? `${Math.round((doneCount / allSteps.length) * 100)}%` : "0%" }}
              />
            </div>
          </div>
          {groups.map((group) => (
            <section className="todo-float-section" key={group.id}>
              <div className="todo-float-section-title">{group.title === "任务步骤" ? "计划" : group.title}</div>
              <div className="todo-float-list">
                {group.steps.map((step) => (
                  <StepRow step={step} key={step.id} />
                ))}
              </div>
            </section>
          ))}
        </>
      )}
    </div>
  );
}
