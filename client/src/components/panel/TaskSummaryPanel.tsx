/** 任务摘要面板（v3）：任务拆分步骤、产物文件审查清单、浏览过的文件。 */
import { useEffect, useMemo, useState } from "react";
import { useChatStore } from "../../store/chat";
import { usePanelStore } from "../../store/panel";
import { IconFileRead, IconRefresh, IconExternalLink, IconCheck, IconRotateCcw, IconSpinner, IconX, IconCheckCircle } from "../icons";

export function TaskSummaryPanel() {
  const tasks = useChatStore((s) => s.tasks);
  const currentSessionId = useChatStore((s) => s.currentSessionId);
  const refreshTasks = useChatStore((s) => s.refreshTasks);
  const messages = useChatStore((s) => s.messages);
  const reviewedFiles = useChatStore((s) => s.reviewedFiles);
  const markFileReviewed = useChatStore((s) => s.markFileReviewed);
  const setPreviewPath = usePanelStore((s) => s.setPreviewPath);
  const openTab = usePanelStore((s) => s.openTab);
  const [visitedFiles, setVisitedFiles] = useState<string[]>([]);

  // 从消息中提取 AI 浏览过的文件（fs_read 参数）
  useEffect(() => {
    const files = new Set<string>();
    messages.forEach((m) => {
      const c = m.content as Record<string, unknown>;
      if (m.msg_type === "tool_call" && c.tool === "fs_read") {
        const p = (c.args as Record<string, unknown>)?.path;
        if (typeof p === "string" && p) files.add(p);
      }
    });
    setVisitedFiles([...files].slice(-20).reverse());
  }, [currentSessionId, messages]);

  // 提取产物文件清单（artifact 消息的 files）及对应 turn
  const artifacts = useMemo(() => {
    const seen = new Set<string>();
    const list: Array<{ path: string; turnId: number | null }> = [];
    messages.forEach((m) => {
      const c = m.content as Record<string, unknown>;
      if (m.msg_type === "artifact") {
        const fileList = Array.isArray(c.files) ? c.files.map(String) : [];
        for (const f of fileList) {
          if (!seen.has(f)) { seen.add(f); list.push({ path: f, turnId: m.turn_id ?? null }); }
        }
      }
    });
    return list;
  }, [messages]);

  const unReviewed = artifacts.filter((a) => !reviewedFiles[a.path]);
  const allReviewed = artifacts.length > 0 && unReviewed.length === 0;

  // v9: 只展示「最新 turn」的任务步骤——新任务开始后自动清理历史任务步骤，
  // 聚焦展示新任务的拆分步骤与执行情况（历史 turn 的任务不再占用面板）。
  const currentTurnTasks = useMemo(() => {
    const withTurn = tasks.filter((t) => t.turn_id != null);
    if (withTurn.length === 0) return tasks;
    const latest = Math.max(...withTurn.map((t) => t.turn_id as number));
    return tasks.filter((t) => t.turn_id === latest);
  }, [tasks]);

  const stats = useMemo(() => {
    const total = currentTurnTasks.length;
    const done = currentTurnTasks.filter((t) => t.status === "done" || t.status === "cancelled").length;
    // in_progress 是子代理任务的"运行中"状态（agent_runtime 使用），统一计入进行中
    const running = currentTurnTasks.filter((t) => t.status === "running" || t.status === "in_progress").length;
    return { total, done, running };
  }, [currentTurnTasks]);

  const openFile = (path: string) => {
    setPreviewPath(path);
    openTab("files");
  };

  return (
    <div className="rp-body">
      {currentTurnTasks.length > 0 && (
        <div className="ts-stats">
          <span className="ts-stat"><b>{stats.done}</b>/{stats.total} 完成</span>
          {stats.running > 0 && <span className="ts-stat running"><IconSpinner size={11} /> {stats.running} 进行中</span>}
          <button className="ts-refresh" onClick={() => refreshTasks()} title="刷新"><IconRefresh size={12} /></button>
        </div>
      )}

      {/* 任务步骤（当前 turn 的拆分步骤，含状态进度与产物数） */}
      {currentTurnTasks.length === 0 ? (
        <div className="rp-empty">暂无任务，发送需求后由 AI 自动拆解</div>
      ) : (
        <div className="ts-section">
          <div className="ts-section-title">
            <span>任务步骤</span>
            <span className="ts-count">{currentTurnTasks.length} 步</span>
          </div>
          <div className="ts-task-scroll">
            {currentTurnTasks.map((t, idx) => {
              // in_progress -> running：统一"进行中"展示（agent_runtime 的子代理任务状态）
              const st = t.status === "in_progress" ? "running" : t.status;
              return (
                <div key={t.id} className={`rp-task ${st}`}>
                  <span className={`rp-task-status ${st}`}>
                    {st === "done" ? <IconCheck size={11} /> : st === "running" ? <IconSpinner size={11} /> : st === "failed" ? <IconX size={11} /> : "·"}
                  </span>
                  <div className="rp-task-main">
                    <div className="rp-task-title">
                      <span className="rp-task-step">#{idx + 1}</span>{t.title}
                    </div>
                    {t.note && <div className="rp-task-note">{t.note}</div>}
                    {t.artifact_ids && t.artifact_ids.length > 0 && (
                      <div className="ts-artifacts">{t.artifact_ids.length} 个产物文件</div>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* 待审查文件清单 */}
      {artifacts.length > 0 && (
        <div className="ts-section">
          <div className="ts-section-title">
            <span>产物文件审查 {unReviewed.length ? `（${unReviewed.length} 未审查）` : ""}</span>
            <div className="ts-section-actions">
              <button className="ts-mini-btn" disabled={allReviewed} onClick={() => artifacts.forEach((a) => markFileReviewed(a.path, true))}>
                <IconCheck size={11} /> 全部通过
              </button>
            </div>
          </div>
          {unReviewed.length === 0 && <div className="ts-empty" style={{ display: "flex", alignItems: "center", gap: 4 }}><IconCheckCircle size={13} color="var(--success)" /> 全部文件已审查</div>}
          {artifacts.map((a) => (
            <div key={a.path} className={`ts-file-review-row${reviewedFiles[a.path] ? " reviewed" : ""}`}>
              <span
                className="artifact-file-review"
                style={{ width: 15, height: 15, borderRadius: 4, border: "1.5px solid var(--text-3)", display: "inline-flex", alignItems: "center", justifyContent: "center", cursor: "pointer", flexShrink: 0 }}
                onClick={() => markFileReviewed(a.path, !reviewedFiles[a.path])}
              >
                {reviewedFiles[a.path] && <IconCheck size={11} />}
              </span>
              <span className="ts-file-review-name" title={a.path} onClick={() => openFile(a.path)}>{a.path}</span>
              <div className="ts-file-review-ops">
                <button className="ts-mini-btn" onClick={() => openFile(a.path)} title="查看变更"><IconExternalLink size={11} /></button>
                {a.turnId != null && (
                  <button className="ts-mini-btn danger" title="撤销该 turn 变更（先预览确认）" onClick={() => useChatStore.getState().requestRollbackPreview(a.turnId!)}>
                    <IconRotateCcw size={11} />
                  </button>
                )}
              </div>
            </div>
          ))}
        </div>
      )}

      {visitedFiles.length > 0 && (
        <div className="ts-section">
          <div className="ts-section-title"><IconFileRead size={12} /> 浏览过的文件</div>
          <div className="ts-files">
            {visitedFiles.map((f) => (
              <button key={f} className="ts-file" onClick={() => openFile(f)} title={f}>
                {f.split("/").pop()}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
