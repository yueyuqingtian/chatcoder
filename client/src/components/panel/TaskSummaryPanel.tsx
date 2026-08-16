/** 任务摘要：与 TodoFloat 同构，只显示真实任务区块、步骤状态和真实产物数据。 */
import { useEffect, useMemo, useState } from "react";
import { useChatStore } from "../../store/chat";
import { usePanelStore } from "../../store/panel";
import type { ArtifactOut, TaskOut } from "../../api/client";
import { IconCheck, IconCheckCircle, IconFileRead, IconExternalLink, IconPause, IconRefresh, IconRotateCcw, IconX } from "../icons";

function normalizeStatus(status: string): string {
  return status === "in_progress" ? "running" : status || "pending";
}

function StepStatus({ status }: { status: string }) {
  const state = normalizeStatus(status);
  if (state === "done") return <span className="ts-step-status done"><IconCheck size={12} /></span>;
  if (state === "running") return <span className="ts-step-status running"><span className="todo-spinner" /></span>;
  if (state === "failed") return <span className="ts-step-status failed"><IconX size={11} /></span>;
  if (state === "cancelled") return <span className="ts-step-status cancelled"><IconPause size={10} /></span>;
  return <span className="ts-step-status pending" />;
}

export function TaskSummaryPanel() {
  const tasks = useChatStore((state) => state.tasks);
  const messages = useChatStore((state) => state.messages);
  const pendingSplit = useChatStore((state) => state.pendingSplit);
  const confirmTaskSplit = useChatStore((state) => state.confirmTaskSplit);
  const refreshTasks = useChatStore((state) => state.refreshTasks);
  const reviewedFiles = useChatStore((state) => state.reviewedFiles);
  const markFileReviewed = useChatStore((state) => state.markFileReviewed);
  const artifacts = useChatStore((state) => state.artifacts);
  const setPreviewPath = usePanelStore((state) => state.setPreviewPath);
  const openTab = usePanelStore((state) => state.openTab);
  const [visitedFiles, setVisitedFiles] = useState<string[]>([]);

  useEffect(() => {
    const files = new Set<string>();
    for (const message of messages) {
      const content = message.content as Record<string, unknown>;
      if (message.msg_type !== "tool_call" || content.tool !== "fs_read") continue;
      const args = content.args as Record<string, unknown> | undefined;
      if (typeof args?.path === "string" && args.path) files.add(args.path);
    }
    setVisitedFiles([...files].slice(-20).reverse());
  }, [messages]);

  const currentTasks = useMemo(() => {
    const visible = tasks.filter((task) => !task.is_hidden);
    const turnIds = visible.map((task) => task.turn_id).filter((id): id is number => id != null);
    if (turnIds.length === 0) return visible;
    const latestTurn = Math.max(...turnIds);
    return visible.filter((task) => task.turn_id === latestTurn);
  }, [tasks]);

  const requestTask = currentTasks.find((task) => task.kind === "request" || (task.kind == null && task.parent_task_id == null));
  const isStep = (task: TaskOut) => task.kind === "step" || (task.parent_task_id != null && task.kind !== "group");
  const proposedGroup = currentTasks.find((task) => task.kind === "group" && task.status === "proposed");
  const steps = useMemo(() => {
    if (proposedGroup) return currentTasks.filter((task) => isStep(task) && task.parent_task_id === proposedGroup.id && !task.is_hidden);
    return currentTasks.filter((task) => isStep(task) && !task.is_hidden);
  }, [currentTasks, proposedGroup]);

  const groups = useMemo(() => {
    const realGroups = currentTasks.filter((task) => task.kind === "group" && task.status !== "proposed" && !task.is_hidden);
    if (realGroups.length > 0) {
      return realGroups.map((group) => ({
        id: group.id,
        title: group.title || "任务步骤",
        steps: steps.filter((step) => isStep(step) && step.parent_task_id === group.id),
      })).filter((group) => group.steps.length > 0);
    }
    if (steps.length > 0) return [{ id: "steps", title: proposedGroup ? "建议步骤" : "任务步骤", steps }];
    return requestTask ? [{ id: "request", title: "任务步骤", steps: [requestTask] }] : [];
  }, [currentTasks, steps, proposedGroup]);

  const artifactFiles = useMemo(() => {
    const seen = new Set<string>();
    const result: Array<{ path: string; turnId: number | null }> = [];
    for (const message of messages) {
      const content = message.content as Record<string, unknown>;
      if (message.msg_type !== "artifact" || !Array.isArray(content.files)) continue;
      for (const file of content.files.map(String)) {
        if (!seen.has(file)) {
          seen.add(file);
          result.push({ path: file, turnId: message.turn_id ?? null });
        }
      }
    }
    return result;
  }, [messages]);
  const unReviewed = artifactFiles.filter((file) => !reviewedFiles[file.path]);
  const allReviewed = artifactFiles.length > 0 && unReviewed.length === 0;
  const orphanArtifacts = artifacts.filter((artifact) => artifact.task_id == null);

  const openFile = (path: string) => {
    setPreviewPath(path);
    openTab("files");
  };

  // v13: 进程统计（对齐 zcode 计划面板「进程 8/8 · 已完成 N 项」）
  const allSteps = groups.flatMap((g) => g.steps);
  const doneSteps = allSteps.filter((s) => normalizeStatus(s.status) === "done").length;

  const ArtifactRows = ({ items }: { items: ArtifactOut[] }) => (
    <div className="ts-artifact-list">
      {items.map((artifact) => (
        <div className="ts-artifact" key={artifact.id}>
          <div className="ts-artifact-title">{artifact.title || `产物 #${artifact.id}`}</div>
          {(artifact.files ?? []).map((file) => (
            <button className="ts-artifact-file" key={file} onClick={() => openFile(file)} title={file} type="button">
              {file.split("/").pop()}
            </button>
          ))}
        </div>
      ))}
    </div>
  );

  return (
    <div className="rp-body task-summary-redesign">
      <section className="ts-summary-head">
        <div className="ts-summary-kicker">当前任务</div>
        <div className="ts-summary-title">{requestTask?.title || "暂无任务"}</div>
        {pendingSplit && <div className="ts-summary-hint">任务步骤待确认</div>}
      </section>

      {allSteps.length > 0 && (
        <div className="ts-progress-row">
          <span className="ts-progress-label">进程</span>
          <span className="ts-progress-value">{doneSteps}/{allSteps.length}</span>
        </div>
      )}

      {groups.map((group) => (
        <section className="ts-section ts-real-section" key={group.id}>
          <div className="ts-section-title">{group.title}</div>
          <div className="ts-step-list">
            {group.steps.map((step: TaskOut) => (
              <div className={`ts-step-item ${normalizeStatus(step.status)}`} key={step.id}>
                <StepStatus status={step.status} />
                <button
                  className="ts-step-title ts-step-jump"
                  type="button"
                  disabled={step.agent_id == null}
                  title={step.agent_id != null ? "点击定位到执行消息" : step.title}
                  onClick={() => {
                    // v2.2: 任务卡点击穿透——滚动到该步骤子代理的首条消息
                    if (step.agent_id != null) useChatStore.getState().requestScrollTo({ threadId: step.agent_id });
                  }}
                >
                  {step.title}
                </button>
              </div>
            ))}
          </div>
        </section>
      ))}

      {pendingSplit && (
        <div className="ts-proposal-actions">
          <button className="ts-proposal-primary" onClick={() => confirmTaskSplit(true)} type="button">接受拆分</button>
          <button onClick={() => confirmTaskSplit(false)} type="button">直接执行</button>
        </div>
      )}

      {artifacts.length > 0 && (
        <section className="ts-section">
          <div className="ts-section-title">产物</div>
          <ArtifactRows items={artifacts.filter((artifact) => artifact.task_id != null)} />
          {orphanArtifacts.length > 0 && <ArtifactRows items={orphanArtifacts} />}
        </section>
      )}

      {artifactFiles.length > 0 && (
        <section className="ts-section">
          <div className="ts-section-title">
            <span>文件审查</span>
            <button className="ts-mini-btn" disabled={allReviewed} onClick={() => artifactFiles.forEach((file) => markFileReviewed(file.path, true))} type="button">
              <IconCheck size={11} /> 全部通过
            </button>
          </div>
          {artifactFiles.map((file) => (
            <div className={`ts-file-review-row${reviewedFiles[file.path] ? " reviewed" : ""}`} key={file.path}>
              <button className="artifact-file-review" onClick={() => markFileReviewed(file.path, !reviewedFiles[file.path])} type="button">
                {reviewedFiles[file.path] && <IconCheck size={11} />}
              </button>
              <button className="ts-file-review-name" onClick={() => openFile(file.path)} title={file.path} type="button">{file.path}</button>
              <div className="ts-file-review-ops">
                <button className="ts-mini-btn" onClick={() => openFile(file.path)} title="打开文件" type="button"><IconExternalLink size={11} /></button>
                {file.turnId != null && <button className="ts-mini-btn danger" onClick={() => useChatStore.getState().requestRollbackPreview(file.turnId!)} title="回滚" type="button"><IconRotateCcw size={11} /></button>}
              </div>
            </div>
          ))}
          {allReviewed && <div className="ts-empty"><IconCheckCircle size={13} /> 全部文件已审查</div>}
        </section>
      )}

      {visitedFiles.length > 0 && (
        <section className="ts-section">
          <div className="ts-section-title"><IconFileRead size={12} /> 浏览过的文件</div>
          <div className="ts-files">
            {visitedFiles.map((file) => <button className="ts-file" key={file} onClick={() => openFile(file)} title={file} type="button">{file.split("/").pop()}</button>)}
          </div>
        </section>
      )}

      {currentTasks.length > 0 && <button className="ts-refresh-bottom" onClick={() => refreshTasks()} type="button"><IconRefresh size={12} /> 刷新任务</button>}
    </div>
  );
}
