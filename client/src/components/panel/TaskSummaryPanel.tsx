/** 任务摘要面板（v3）：任务拆分步骤、产物（v12 按任务分组）、产物文件审查清单、浏览过的文件。 */
import { useEffect, useMemo, useState } from "react";
import { useChatStore } from "../../store/chat";
import { usePanelStore } from "../../store/panel";
import type { ArtifactOut } from "../../api/client";
import { IconFileRead, IconRefresh, IconExternalLink, IconCheck, IconRotateCcw, IconSpinner, IconX, IconCheckCircle, IconPause, IconChevronDown, IconChevronUp } from "../icons";

export function TaskSummaryPanel() {
  const tasks = useChatStore((s) => s.tasks);
  const currentSessionId = useChatStore((s) => s.currentSessionId);
  const refreshTasks = useChatStore((s) => s.refreshTasks);
  const messages = useChatStore((s) => s.messages);
  const reviewedFiles = useChatStore((s) => s.reviewedFiles);
  const markFileReviewed = useChatStore((s) => s.markFileReviewed);
  // v12: Artifact 表聚合（含 title/summary/files），按任务分组展示
  const storeArtifacts = useChatStore((s) => s.artifacts);
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
  const artifactFiles = useMemo(() => {
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

  const unReviewed = artifactFiles.filter((a) => !reviewedFiles[a.path]);
  const allReviewed = artifactFiles.length > 0 && unReviewed.length === 0;

  // v12: 产物按任务分组（task_id -> artifacts，去重保序）
  const artifactsByTask = useMemo(() => {
    const map = new Map<number, ArtifactOut[]>();
    storeArtifacts.forEach((a) => {
      if (a.task_id == null) return;
      const list = map.get(a.task_id) ?? [];
      list.push(a);
      map.set(a.task_id, list);
    });
    return map;
  }, [storeArtifacts]);

  // v12: 无归属任务（task_id 为 null）的产物进「其他产物」
  const orphanArtifacts = useMemo(
    () => storeArtifacts.filter((a) => a.task_id == null),
    [storeArtifacts],
  );

  // v9/v12: 默认展示「最新 turn」的任务；v12 可下拉切换到历史 turn（切走后固定查看该 turn）。
  const turnsWithTasks = useMemo(() => {
    const ids = new Set<number>();
    tasks.forEach((t) => { if (t.turn_id != null) ids.add(t.turn_id as number); });
    return Array.from(ids).sort((a, b) => b - a);
  }, [tasks]);
  const latestTurn = turnsWithTasks[0] ?? null;
  const [selectedTurn, setSelectedTurn] = useState<number | null>(null);
  const activeTurn = selectedTurn ?? latestTurn;

  const currentTurnTasks = useMemo(() => {
    if (activeTurn == null) return tasks;
    return tasks.filter((t) => t.turn_id === activeTurn);
  }, [tasks, activeTurn]);

  const stats = useMemo(() => {
    const total = currentTurnTasks.length;
    // v12: cancelled 独立计数（回滚/取消后不再误计为已完成）
    const done = currentTurnTasks.filter((t) => t.status === "done").length;
    const cancelled = currentTurnTasks.filter((t) => t.status === "cancelled").length;
    // in_progress 是子代理任务的"运行中"状态（agent_runtime 使用），统一计入进行中
    const running = currentTurnTasks.filter((t) => t.status === "running" || t.status === "in_progress").length;
    return { total, done, cancelled, running };
  }, [currentTurnTasks]);

  // v12: 子任务（parent 在本列表内）缩进展示，不计入步骤序号
  const isChild = useMemo(() => {
    const ids = new Set(currentTurnTasks.map((t) => t.id));
    const set = new Set<number>();
    currentTurnTasks.forEach((t) => {
      if (t.parent_task_id != null && t.id !== t.parent_task_id && ids.has(t.parent_task_id)) set.add(t.id);
    });
    return set;
  }, [currentTurnTasks]);

  // v12: 任务详情展开（描述/验收标准）
  const [detailOpen, setDetailOpen] = useState<number | null>(null);

  const openFile = (path: string) => {
    setPreviewPath(path);
    openTab("files");
  };

  // v12: 单个产物块（标题/摘要/文件 chips）
  const ArtifactItem = ({ art }: { art: ArtifactOut }) => {
    const files = art.files ?? [];
    return (
      <div className="ts-artifact">
        <div className="ts-artifact-title" title={art.summary ?? undefined}>
          {art.title || `产物 #${art.id}`}
          <span className="ts-artifact-count">{files.length} 文件</span>
        </div>
        {files.length > 0 && (
          <div className="ts-artifact-files">
            {files.map((f) => (
              <button key={f} className="ts-artifact-file" title={f} onClick={() => openFile(f)}>
                {f.split("/").pop()}
              </button>
            ))}
          </div>
        )}
      </div>
    );
  };

  return (
    <>
    <style>{`
      .ts-artifact { margin-top: 6px; }
      .ts-artifact-title {
        display: flex; align-items: center; gap: 6px;
        font-size: 12px; color: var(--text-2); font-weight: 600;
        white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
      }
      .ts-artifact-count { font-size: 11px; color: var(--text-3); font-weight: 400; flex-shrink: 0; }
      .ts-artifact-files { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 4px; }
      .ts-artifact-file {
        max-width: 180px; padding: 1px 7px; font-size: 11px;
        background: var(--bg-muted); color: var(--text-2);
        border: 1px solid var(--border); border-radius: var(--radius-sm);
        cursor: pointer; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
      }
      .ts-artifact-file:hover { border-color: var(--accent); color: var(--accent); }
      .ts-turn-select {
        margin-left: 4px; font-size: 11px; padding: 1px 4px;
        background: var(--bg-muted); color: var(--text-2);
        border: 1px solid var(--border); border-radius: var(--radius-sm);
      }
      .rp-task.child { margin-left: 22px; padding-top: 1px; padding-bottom: 1px; }
      .rp-task.child .rp-task-step { display: none; }
      .rp-task.child .rp-task-label { font-size: 11px; color: var(--text-3); font-weight: 400; }
      .rp-task-label { min-width: 0; overflow: hidden; text-overflow: ellipsis; }
      .rp-task-chevron { display: inline-flex; margin-left: 4px; color: var(--text-3); flex-shrink: 0; }
      .rp-task-detail { margin-top: 3px; font-size: 11px; color: var(--text-3); line-height: 1.6; }
      .rp-task-detail b { color: var(--text-2); font-weight: 600; }
      .rp-task.child .rp-task-chevron { display: none; }
      .rp-task { cursor: pointer; }
      .rp-task.child { cursor: default; }
    `}</style>
    <div className="rp-body">
      {currentTurnTasks.length > 0 && (
        <div className="ts-stats">
          <span className="ts-stat"><b>{stats.done}</b>/{stats.total} 完成</span>
          {stats.running > 0 && <span className="ts-stat running"><IconSpinner size={11} /> {stats.running} 进行中</span>}
          {stats.cancelled > 0 && <span className="ts-stat cancelled"><IconPause size={11} /> {stats.cancelled} 已取消</span>}
          {/* v12: 历史 turn 切换（默认最新 turn） */}
          {turnsWithTasks.length > 1 && (
            <select
              className="ts-turn-select"
              value={activeTurn != null ? String(activeTurn) : ""}
              title="切换查看任务所属 turn"
              onChange={(e) => {
                const v = e.target.value;
                setSelectedTurn(v === "" || v === String(latestTurn) ? null : Number(v));
              }}
            >
              {turnsWithTasks.map((tid) => (
                <option key={tid} value={String(tid)}>
                  {tid === latestTurn ? `最新 turn #${tid}` : `turn #${tid}`}
                </option>
              ))}
            </select>
          )}
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
            <span className="ts-count">{currentTurnTasks.filter((t) => !isChild.has(t.id)).length} 步</span>
          </div>
          <div className="ts-task-scroll">
            {currentTurnTasks.map((t, idx) => {
              // in_progress -> running：统一"进行中"展示（agent_runtime 的子代理任务状态）
              const st = t.status === "in_progress" ? "running" : t.status;
              const child = isChild.has(t.id);
              const open = detailOpen === t.id;
              const no = child ? null : idx + 1;
              return (
                <div key={t.id}>
                  <div
                    className={`rp-task ${st}${child ? " child" : ""}`}
                    onClick={() => setDetailOpen(open ? null : t.id)}
                    title={child ? undefined : "点击展开任务详情"}
                  >
                    <span className={`rp-task-status ${st}`}>
                      {st === "done" ? <IconCheck size={11} /> : st === "running" ? <IconSpinner size={11} /> : st === "failed" ? <IconX size={11} /> : st === "cancelled" ? <IconPause size={11} /> : "·"}
                    </span>
                    <div className="rp-task-main">
                      <div className="rp-task-title">
                        {no != null && <span className="rp-task-step">#{no}</span>}
                        <span className="rp-task-label">{t.title}</span>
                        {!child && (
                          <span className="rp-task-chevron">
                            {open ? <IconChevronUp size={12} /> : <IconChevronDown size={12} />}
                          </span>
                        )}
                      </div>
                      {(open && !child) && (
                        <div className="rp-task-detail">
                          {t.description && <div><b>描述</b>：{t.description}</div>}
                          {t.acceptance_criteria && <div><b>验收标准</b>：{t.acceptance_criteria}</div>}
                        </div>
                      )}
                      {t.note && <div className="rp-task-note">{t.note}</div>}
                      {(() => {
                        const arts = artifactsByTask.get(t.id);
                        if (!arts || arts.length === 0) return null;
                        return (
                          <div className="ts-artifacts">
                            {arts.map((a) => <ArtifactItem key={a.id} art={a} />)}
                          </div>
                        );
                      })()}
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* v12: 无归属任务的产物（task_id 为 null，如回滚后残留） */}
      {orphanArtifacts.length > 0 && (
        <div className="ts-section">
          <div className="ts-section-title">
            <span>其他产物</span>
            <span className="ts-count">{orphanArtifacts.length} 项</span>
          </div>
          <div className="ts-task-scroll">
            {orphanArtifacts.map((a) => <ArtifactItem key={a.id} art={a} />)}
          </div>
        </div>
      )}

      {/* 待审查文件清单 */}
      {artifactFiles.length > 0 && (
        <div className="ts-section">
          <div className="ts-section-title">
            <span>产物文件审查 {unReviewed.length ? `（${unReviewed.length} 未审查）` : ""}</span>
            <div className="ts-section-actions">
              <button className="ts-mini-btn" disabled={allReviewed} onClick={() => artifactFiles.forEach((a) => markFileReviewed(a.path, true))}>
                <IconCheck size={11} /> 全部通过
              </button>
            </div>
          </div>
          {unReviewed.length === 0 && <div className="ts-empty" style={{ display: "flex", alignItems: "center", gap: 4 }}><IconCheckCircle size={13} color="var(--success)" /> 全部文件已审查</div>}
          {artifactFiles.map((a) => (
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
    </>
  );
}
