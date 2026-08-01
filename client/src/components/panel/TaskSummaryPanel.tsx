/** 任务摘要面板（v2）：turn 进展、子代理、产物、AI 浏览过的文件。 */
import { useEffect, useMemo, useState } from "react";
import { useChatStore } from "../../store/chat";
import { usePanelStore } from "../../store/panel";
import { IconFileRead, IconRefresh } from "../icons";

export function TaskSummaryPanel() {
  const { tasks, currentSessionId, refreshTasks } = useChatStore();
  const { setPreviewPath, openTab } = usePanelStore();
  const [visitedFiles, setVisitedFiles] = useState<string[]>([]);

  // 从消息中提取 AI 浏览过的文件（fs_read 参数）
  useEffect(() => {
    const files = new Set<string>();
    useChatStore.getState().messages.forEach((m) => {
      const c = m.content as Record<string, unknown>;
      if (m.msg_type === "tool_call" && c.tool === "fs_read") {
        const p = (c.args as Record<string, unknown>)?.path;
        if (typeof p === "string" && p) files.add(p);
      }
    });
    setVisitedFiles([...files].slice(-20).reverse());
  }, [currentSessionId]);

  const stats = useMemo(() => {
    const total = tasks.length;
    const done = tasks.filter((t) => t.status === "done" || t.status === "cancelled").length;
    const running = tasks.filter((t) => t.status === "running").length;
    return { total, done, running };
  }, [tasks]);

  const openFile = (path: string) => {
    setPreviewPath(path);
    openTab("files");
  };

  return (
    <div className="rp-body">
      {tasks.length > 0 && (
        <div className="ts-stats">
          <span className="ts-stat"><b>{stats.done}</b>/{stats.total} 完成</span>
          {stats.running > 0 && <span className="ts-stat running">◌ {stats.running} 进行中</span>}
          <button className="ts-refresh" onClick={() => refreshTasks()} title="刷新"><IconRefresh size={12} /></button>
        </div>
      )}

      {tasks.length === 0 ? (
        <div className="rp-empty">暂无任务，发送需求后由 AI 自动拆解</div>
      ) : (
        tasks.map((t) => (
          <div key={t.id} className={`rp-task ${t.status}`}>
            <span className={`rp-task-status ${t.status}`}>
              {t.status === "done" ? "✓" : t.status === "running" ? "◌" : t.status === "failed" ? "✕" : "·"}
            </span>
            <div className="rp-task-main">
              <div className="rp-task-title">{t.title}</div>
              {t.note && <div className="rp-task-note">{t.note}</div>}
              {t.artifact_ids && t.artifact_ids.length > 0 && (
                <div className="ts-artifacts">{t.artifact_ids.length} 个产物</div>
              )}
            </div>
          </div>
        ))
      )}

      {visitedFiles.length > 0 && (
        <div className="ts-files">
          <div className="ts-files-title"><IconFileRead size={12} /> 浏览过的文件</div>
          {visitedFiles.map((f) => (
            <button key={f} className="ts-file" onClick={() => openFile(f)} title={f}>
              {f.split("/").pop()}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
