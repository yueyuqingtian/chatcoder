/** v23: 输入框上方的任务进度/文件变更贴条（半弧形 tab，紧贴输入框上沿）。
 * - 左侧：任务进度——仅展示最新 turn 的任务步骤（与任务摘要面板同源口径）；
 * - 右侧：文件变更——会话从开始到现在「未审核通过」的变更，按 turn（消息）分组，
 *   支持单文件审核/打开 diff、分组回滚；
 * - 左右两半分别独立展开；展开体在正常布局流内（上推消息流而非遮挡），限高内滚。 */
import { useMemo, useState } from "react";
import { useChatStore } from "../../store/chat";
import { usePanelStore } from "../../store/panel";
import { api, type FileChangeOut, type TaskOut } from "../../api/client";
import { IconCheck, IconChevronDown, IconExternalLink, IconRotateCcw } from "../icons";
import { FileBadge, splitFilePath } from "./FileBadge";

function isStepTask(t: TaskOut): boolean {
  return t.kind === "step" || (t.parent_task_id != null && t.kind !== "group");
}

function normalizeStatus(status: string): string {
  return status === "in_progress" ? "running" : status || "pending";
}

interface ChangeGroup {
  turnId: number;
  label: string;
  files: FileChangeOut[];
  additions: number;
  deletions: number;
}

export function TaskStatusPanel() {
  const [expandLeft, setExpandLeft] = useState(false);
  const [expandRight, setExpandRight] = useState(false);
  const tasks = useChatStore((s) => s.tasks);
  const todos = useChatStore((s) => s.todos);
  const messages = useChatStore((s) => s.messages);
  const turnChanges = useChatStore((s) => s.turnChanges);
  const reviewFiles = useChatStore((s) => s.reviewFiles);
  const requestRollbackPreview = useChatStore((s) => s.requestRollbackPreview);
  const setPreviewPath = usePanelStore((s) => s.setPreviewPath);
  const setDiffPreview = usePanelStore((s) => s.setDiffPreview);
  const openPanel = usePanelStore((s) => s.openPanel);
  const openTab = usePanelStore((s) => s.openTab);

  // —— 左侧：最新 turn 的任务步骤（todo 清单优先，缺失时回退任务步骤口径）——
  const { steps, requestTitle } = useMemo(() => {
    const visible = tasks.filter((t) => !t.is_hidden);
    const turnIds = visible.map((t) => t.turn_id).filter((id): id is number => id != null);
    if (turnIds.length === 0) return { steps: [] as TaskOut[], requestTitle: "" };
    const latestTurn = Math.max(...turnIds);
    const current = visible.filter((t) => t.turn_id === latestTurn);
    const request = current.find((t) => t.kind === "request" || (t.kind == null && t.parent_task_id == null));
    const stepList = current.filter((t) => isStepTask(t) && !t.is_hidden);
    return {
      steps: stepList.length > 0 ? stepList : (request ? [request] : []),
      requestTitle: request?.title ?? "",
    };
  }, [tasks]);

  const todoItems = useMemo(() => (Array.isArray(todos) ? todos.filter((t) => t.content) : []), [todos]);
  const useTodo = todoItems.length > 0;
  // 统一进度展示行：todo 清单（计划拆分确认后自动初始化，模型 todo_write 驱动）优先；
  // 否则回退引擎任务步骤，保证非计划任务行为不变。
  const rows = useMemo(() => {
    const raw = useTodo
      ? todoItems.map((t) => ({
          key: t.content,
          title: t.content,
          note: t.activeForm || "",
          status: t.status === "completed" ? "done" : t.status === "in_progress" ? "running" : "pending",
        }))
      : steps.map((s) => ({
          key: String(s.id),
          title: s.title,
          note: s.note || "",
          status: normalizeStatus(s.status),
        }));
    // 兜底：无 running 项时，第一个未完成项按 running 视觉展示，保证转圈/脉冲动画可见
    if (raw.length > 0 && !raw.some((r) => r.status === "running")) {
      const idx = raw.findIndex((r) => r.status !== "done");
      if (idx >= 0) raw[idx] = { ...raw[idx], status: "running" };
    }
    return raw;
  }, [useTodo, todoItems, steps]);

  // —— 右侧：未审核变更按 turn 分组（升序：从会话开始到现在）——
  const groups = useMemo<ChangeGroup[]>(() => {
    const out: ChangeGroup[] = [];
    const turnIds = Object.keys(turnChanges).map(Number).sort((a, b) => a - b);
    for (const turnId of turnIds) {
      const pending = (turnChanges[turnId] ?? []).filter((f) => !f.reviewed);
      if (pending.length === 0) continue;
      const userMsg = messages.find((m) => m.turn_id === turnId && m.sender_type === "user");
      const raw = userMsg ? String((userMsg.content as Record<string, unknown>).text ?? "").trim() : "";
      const firstLine = raw.split("\n")[0] ?? "";
      out.push({
        turnId,
        label: firstLine ? (firstLine.length > 26 ? `${firstLine.slice(0, 26)}…` : firstLine) : `变更 #${turnId}`,
        files: pending,
        additions: pending.reduce((n, f) => n + (f.additions ?? 0), 0),
        deletions: pending.reduce((n, f) => n + (f.deletions ?? 0), 0),
      });
    }
    return out;
  }, [turnChanges, messages]);

  const hasLeft = rows.length > 0;
  const hasRight = groups.length > 0;
  if (!hasLeft && !hasRight) return null;

  const done = rows.filter((r) => r.status === "done").length;
  const runningRow = rows.find((r) => r.status === "running");
  const failedRow = rows.find((r) => r.status === "failed");
  const currentTitle = runningRow?.title ?? failedRow?.title ?? "";
  const fileCount = groups.reduce((n, g) => n + g.files.length, 0);
  const bodyOpen = (expandLeft && hasLeft) || (expandRight && hasRight);
  const bothOpen = expandLeft && hasLeft && expandRight && hasRight;

  const openDiff = async (turnId: number, f: FileChangeOut) => {
    setPreviewPath(f.path);
    openPanel();
    openTab("files");
    try {
      const d = await api.getFileDiff(turnId, f.path);
      setDiffPreview({ path: d.path, before: d.before, after: d.after, truncated: d.truncated });
    } catch { /* diff 拉取失败则回退为普通文件预览 */ }
  };

  return (
    <section className="task-strip">
      <div className="task-strip-frame">
        <div className="task-strip-bar" role="toolbar" aria-label="任务与变更概览">
          {hasLeft && (
            <button
              type="button"
              className={`task-strip-seg${expandLeft ? " active" : ""}`}
              onClick={() => setExpandLeft((v) => !v)}
              title={requestTitle || "任务进度"}
            >
              <span className="task-strip-name">任务进度</span>
              <span className="task-strip-count">{done}/{rows.length}</span>
              {currentTitle && (
                <span className={`task-strip-current${runningRow ? " running" : ""}`}>{currentTitle}</span>
              )}
              <IconChevronDown size={12} className="task-strip-caret" />
            </button>
          )}
          {hasLeft && hasRight && <span className="task-strip-divider" />}
          {hasRight && (
            <button
              type="button"
              className={`task-strip-seg right${expandRight ? " active" : ""}`}
              onClick={() => setExpandRight((v) => !v)}
              title="未审核的文件变更"
            >
              <span className="task-strip-name">文件变更</span>
              <span className="task-strip-count">{fileCount} 待审</span>
              <IconChevronDown size={12} className="task-strip-caret" />
            </button>
          )}
        </div>

        <div className={`task-strip-body${bodyOpen ? " open" : ""}`}>
          <div className={`task-strip-cols${bothOpen ? "" : " single"}`}>
            {expandLeft && hasLeft && (
              <div className="task-strip-col">
                {rows.map((row) => (
                  <div className={`task-strip-step ${row.status}`} key={row.key}>
                    <span className={`task-strip-dot ${row.status}`} />
                    <span className="task-strip-step-title" title={row.title}>{row.title}</span>
                    {row.note && <span className="task-strip-step-note" title={row.note}>{row.note}</span>}
                  </div>
                ))}
              </div>
            )}
            {expandRight && hasRight && (
              <div className="task-strip-col">
                {groups.map((g) => (
                  <div className="task-strip-group" key={g.turnId}>
                    <div className="task-strip-group-head">
                      <span className="task-strip-group-label" title={g.label}>{g.label}</span>
                      <span className="task-strip-group-count">{g.files.length} 文件</span>
                      {(g.additions > 0 || g.deletions > 0) && (
                        <span className="task-strip-diff">
                          {g.additions > 0 && <span className="add">+{g.additions}</span>}
                          {g.deletions > 0 && <span className="del">-{g.deletions}</span>}
                        </span>
                      )}
                      <span className="task-strip-group-ops">
                        <button
                          type="button"
                          className="task-strip-op"
                          title="该组全部标记为已审核"
                          onClick={() => reviewFiles(g.turnId, g.files.map((f) => f.path), true)}
                        >
                          <IconCheck size={11} />
                        </button>
                        <button
                          type="button"
                          className="task-strip-op danger"
                          title="回滚该组变更（先预览确认）"
                          onClick={() => void requestRollbackPreview(g.turnId)}
                        >
                          <IconRotateCcw size={11} />
                        </button>
                      </span>
                    </div>
                    {g.files.map((f) => {
                      const { dir, name } = splitFilePath(f.path);
                      return (
                        <div className="task-strip-file" key={f.path}>
                          <FileBadge path={f.path} size={14} />
                          <span className="task-strip-file-name" title={f.path} onClick={() => void openDiff(g.turnId, f)}>
                            {name}
                            {dir && <span className="task-strip-file-dir">{dir}</span>}
                          </span>
                          {(f.additions > 0 || f.deletions > 0) && (
                            <span className="task-strip-diff">
                              {f.additions > 0 && <span className="add">+{f.additions}</span>}
                              {f.deletions > 0 && <span className="del">-{f.deletions}</span>}
                            </span>
                          )}
                          <span className="task-strip-file-ops">
                            <button
                              type="button"
                              className="task-strip-op"
                              title="标记为已审核"
                              onClick={() => reviewFiles(g.turnId, [f.path], true)}
                            >
                              <IconCheck size={11} />
                            </button>
                            <button
                              type="button"
                              className="task-strip-op"
                              title="查看 diff"
                              onClick={() => void openDiff(g.turnId, f)}
                            >
                              <IconExternalLink size={11} />
                            </button>
                          </span>
                        </div>
                      );
                    })}
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>
    </section>
  );
}
