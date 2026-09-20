/** 任务摘要：与 TodoFloat 同构，只显示真实任务区块、步骤状态和真实产物数据。 */
import { useEffect, useMemo, useState } from "react";
import { useChatStore } from "../../store/chat";
import { usePanelStore } from "../../store/panel";
import type { ArtifactOut } from "../../api/client";
import { IconCheck, IconCheckCircle, IconFileRead, IconExternalLink, IconPause, IconRefresh, IconRotateCcw, IconX } from "../icons";

import { useProgressRows } from "../chat/taskProgress";

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
  // v38 (plan-482): 方案文档确认入口——等待确认时在任务面板提供确认/停止按钮
  const hasPendingPlan = useChatStore((state) => state.pendingPlan != null);
  const confirmPlanTurn = useChatStore((state) => state.confirmPlanTurn);
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

  /** plan-282-1441（#3）：任务进度统一走共享模块（与输入框上方胶囊**同源**）。
   *  此前这里自有一套口径：只看 max(turn_id)，无步骤时还会回退到 request 任务
   *  （而 request 的标题就是用户消息文本），导致与胶囊显示不一致。
   *  plan-282-1492：展示口径（是否还该显示、要不要标"进行中"）已收进共享模块，
   *  面板不再自算运行态门控。 */
  const progress = useProgressRows();
  const progressRows = progress.rows;
  const progressDone = progress.done;

  /** 当前任务标题：取最近一个 request 任务（仅用于展示标题，不参与进度） */
  const currentTasks = useMemo(() => {
    const visible = tasks.filter((task) => !task.is_hidden);
    const turnIds = visible.map((task) => task.turn_id).filter((id): id is number => id != null);
    if (turnIds.length === 0) return visible;
    const latestTurn = Math.max(...turnIds);
    return visible.filter((task) => task.turn_id === latestTurn);
  }, [tasks]);

  const requestTask = currentTasks.find((task) => task.kind === "request" || (task.kind == null && task.parent_task_id == null));

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

  // 任务进度：plan-282-1441（#3）已改为共享模块 useProgressRows（与输入框上方胶囊同源），
  // 此处不再自算（旧实现只看 max(turn_id) 并回退 request 任务，与胶囊口径不一致）。

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
        {hasPendingPlan && <div className="ts-summary-hint">方案文档待确认</div>}
      </section>

      {progressRows.length > 0 && (
        <section className="ts-section ts-real-section">
          <div className="ts-section-title">任务进度 {progressDone}/{progressRows.length}</div>
          <div className="ts-step-list">
            {progressRows.map((row, i) => (
              <div className={`ts-step-item ${row.status}`} key={`${row.key}-${i}`}>
                <StepStatus status={row.status} />
                <button
                  className="ts-step-title ts-step-jump"
                  type="button"
                  disabled={row.agentId == null}
                  title={row.agentId != null ? "点击定位到执行消息" : row.note || row.title}
                  onClick={() => {
                    if (row.agentId != null) useChatStore.getState().requestScrollTo({ threadId: row.agentId });
                  }}
                >
                  {row.title}
                </button>
                {row.note && <span className="ts-step-note" title={row.note}>{row.note}</span>}
              </div>
            ))}
          </div>
        </section>
      )}

      {hasPendingPlan && (
        <div className="ts-proposal-actions">
          <button className="ts-proposal-primary" onClick={() => confirmPlanTurn(true)} type="button">确认执行</button>
          <button onClick={() => confirmPlanTurn(false)} type="button">停止任务</button>
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
