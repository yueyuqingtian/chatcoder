/** TaskProgressCapsule —— 输入框上方的任务进度 / 文件变更悬浮胶囊（plan-282-1421 第12项）
 *
 * 取代旧的 `.task-strip`（贴在输入框上沿、与其合并边框、存在即常驻）：
 *
 * 【信息架构 · 智能动态】
 *   仅有未完成 AI 任务 → 只显进度；仅有未审核文件 → 只显变更；两者都有 → 分块显示；
 *   两者都没有 → **不渲染**（不是"有数据就常驻"）。
 *
 * 【任务进度只由 AI 更新】
 *   数据源统一走 `chat/taskProgress.ts`（AI 的 todo 清单优先，其次引擎步骤），
 *   与右侧任务摘要面板同源，保证两处显示一致。
 *
 * 【形态】
 *   悬浮在输入卡上方（absolute，不占布局、不贴边框）；hover / focus 弹浮层展示
 *   任务清单与文件变更（审核 / 查看 diff / 整组回滚）。
 *
 * plan-282-1434（B2）：进度与变更拆成**两个独立浮层**——各自独立聚焦/展开/钉住。
 * plan-282-1441（#3）：三处修正——
 *   ① 聚焦**零视觉变化**（不再改边框色/底色）；
 *   ② 浮层提升到**胶囊容器级**并在**胶囊正上方居中**（旧实现相对"某一段"定位，
 *      且居中用的 transform 被入场动画的终态 `transform: none` 覆盖 → 偏移 + 文字发虚）；
 *   ③ 滚动条统一收细到 3px。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useChatStore } from "../../store/chat";
import { usePanelStore } from "../../store/panel";
import { api, type FileChangeOut } from "../../api/client";
import { IconCheck, IconChevronUp, IconExternalLink, IconFileText, IconCheckSquare, IconRotateCcw } from "../icons";
import { FileBadge, splitFilePath } from "./FileBadge";
import { useProgressRows } from "./taskProgress";

interface ChangeGroup {
  turnId: number;
  label: string;
  files: FileChangeOut[];
  additions: number;
  deletions: number;
}

/** 浮层开合控制：两段各自独立（B2），但浮层本体统一渲染在胶囊容器级（#3）。
 *
 *  - 160ms 宽限期：胶囊与浮层之间有 8px 间隙，鼠标穿过时会先离开胶囊，
 *    立即关闭会让指针永远够不到浮层；
 *  - 点击钉住：便于在右侧面板逐个审核文件；点外部 / Esc 解除。 */
function usePopoverControl() {
  const [open, setOpen] = useState(false);
  const [pinned, setPinned] = useState(false);
  const timerRef = useRef<number | null>(null);
  const wrapRef = useRef<HTMLDivElement>(null);

  const cancelClose = useCallback(() => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);
  const scheduleClose = useCallback(() => {
    cancelClose();
    timerRef.current = window.setTimeout(() => {
      timerRef.current = null;
      setOpen(false);
    }, 160);
  }, [cancelClose]);
  const scheduleCloseUnlessPinned = useCallback(() => {
    if (!pinned) scheduleClose();
  }, [pinned, scheduleClose]);

  useEffect(() => () => cancelClose(), [cancelClose]);

  useEffect(() => {
    if (!pinned) return;
    const onDown = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) {
        setPinned(false);
        setOpen(false);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setPinned(false);
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [pinned]);

  return { open, setOpen, pinned, setPinned, cancelClose, scheduleClose, scheduleCloseUnlessPinned, wrapRef };
}

export function TaskProgressCapsule({ onVisibilityChange }: {
  /** plan-282-1492：向 ChatPanel 上报"胶囊当前是否真的渲染了"——
   *  消息流据此按需让位（未出现不预占位；出现时用高度过渡把消息流顶上去）。 */
  onVisibilityChange?: (visible: boolean) => void;
} = {}) {
  const messages = useChatStore((s) => s.messages);
  const turnChanges = useChatStore((s) => s.turnChanges);
  const reviewFiles = useChatStore((s) => s.reviewFiles);
  const requestRollbackPreview = useChatStore((s) => s.requestRollbackPreview);
  const setPreviewPath = usePanelStore((s) => s.setPreviewPath);
  const setDiffPreview = usePanelStore((s) => s.setDiffPreview);
  const openPanel = usePanelStore((s) => s.openPanel);
  const openTab = usePanelStore((s) => s.openTab);

  /** 与右侧任务摘要面板同源（#3）：两处显示的步骤必然一致。
   *  plan-282-1492：展示口径（是否还该显示、要不要标"进行中"）已收进该模块，
   *  胶囊不再自己判断运行态——避免两处再次分叉。 */
  const progress = useProgressRows();
  const useTodo = progress.useTodo;

  // 展示态：仅"该轮仍在执行"时才把下一项标为进行中（停止/结束的轮次已按口径收口）
  const displayRows = progress.rows;

  // —— 未审核文件按 turn 分组 ——
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

  const done = displayRows.filter((r) => r.status === "done").length;
  const unfinished = displayRows.length - done;
  // 只在"存在未完成任务"时展示进度块
  const hasProgress = displayRows.length > 0 && unfinished > 0;
  const fileCount = groups.reduce((n, g) => n + g.files.length, 0);
  const hasFiles = fileCount > 0;

  // plan-282-1492：把可见性同步给面板（未出现不预留空白）——本 effect 必须早于下方
  // `return null` 的早退分支，否则钩子调用顺序会随渲染分支变化。
  const capsuleVisible = hasProgress || hasFiles;
  useEffect(() => { onVisibilityChange?.(capsuleVisible); }, [capsuleVisible, onVisibilityChange]);
  // 卸载（切会话 / 切设置页）时收回占位，避免消息流底部留一条死白
  useEffect(() => () => onVisibilityChange?.(false), [onVisibilityChange]);
  const fileAdds = groups.reduce((n, g) => n + g.additions, 0);
  const fileDels = groups.reduce((n, g) => n + g.deletions, 0);
  const currentTitle = displayRows.find((r) => r.status === "running")?.title
    ?? displayRows.find((r) => r.status !== "done")?.title
    ?? "";

  /** 两个独立浮层（B2）：各自开合。
   *  plan-282-1441（#3）：浮层统一在胶囊**正上方居中**——定位基数是胶囊本身（见 .tc-pop 的
   *  `left: 50%` + `translate: -50% 0`），不再随触发分段偏移。 */
  const progressPop = usePopoverControl();
  const filesPop = usePopoverControl();

  const openDiff = async (turnId: number, f: FileChangeOut) => {
    setPreviewPath(f.path);
    openPanel();
    openTab("files");
    try {
      const d = await api.getFileDiff(turnId, f.path);
      setDiffPreview({ path: d.path, before: d.before, after: d.after, truncated: d.truncated });
    } catch { /* 拉取失败回退普通预览 */ }
  };

  // 两者皆无 → 不渲染（存在即常驻是被弃用的旧行为）
  if (!hasProgress && !hasFiles) return null;

  return (
    <div
      className="task-capsule-wrap"
      onMouseLeave={() => {
        progressPop.scheduleCloseUnlessPinned();
        filesPop.scheduleCloseUnlessPinned();
      }}
    >
      <div className="task-capsule">
        {hasProgress && (
          <div
            className="tc-seg-wrap"
            ref={progressPop.wrapRef}
            onMouseEnter={() => { progressPop.cancelClose(); progressPop.setOpen(true); }}
            onFocusCapture={() => { progressPop.cancelClose(); progressPop.setOpen(true); }}
            onBlurCapture={(e) => {
              if (e.currentTarget.contains(e.relatedTarget as Node | null)) return;
              progressPop.scheduleCloseUnlessPinned();
            }}
          >
            <button
              type="button"
              className={"tc-seg-btn" + (progressPop.open ? " is-open" : "")}
              aria-expanded={progressPop.open}
              aria-label={`任务进度 ${done}/${displayRows.length}`}
              onMouseEnter={() => { progressPop.cancelClose(); progressPop.setOpen(true); }}
              onClick={() => {
                // 点击 = 钉住（便于在右侧面板逐个审核）；再点或点外部 / Esc 解除
                progressPop.cancelClose();
                progressPop.setPinned((p) => !p);
                progressPop.setOpen(true);
              }}
            >
              <IconCheckSquare size={12} />
              <span className="tc-seg-num">{done}/{displayRows.length}</span>
              {/* plan-64-294：不再挂原生 title——hover 本段已经弹出 .tc-pop（内含完整步骤清单与全文），
                  再接管一个浮层就成两个提示框。 */}
              {currentTitle && <span className="tc-seg-title">{currentTitle}</span>}
              <IconChevronUp size={12} className="tc-seg-caret" />
            </button>
          </div>
        )}

        {hasProgress && hasFiles && <span className="tc-divider" />}

        {hasFiles && (
          <div
            className="tc-seg-wrap"
            ref={filesPop.wrapRef}
            onMouseEnter={() => { filesPop.cancelClose(); filesPop.setOpen(true); }}
            onFocusCapture={() => { filesPop.cancelClose(); filesPop.setOpen(true); }}
            onBlurCapture={(e) => {
              if (e.currentTarget.contains(e.relatedTarget as Node | null)) return;
              filesPop.scheduleCloseUnlessPinned();
            }}
          >
            <button
              type="button"
              className={"tc-seg-btn" + (filesPop.open ? " is-open" : "")}
              aria-expanded={filesPop.open}
              aria-label={`未审核变更 ${fileCount} 个文件`}
              onMouseEnter={() => { filesPop.cancelClose(); filesPop.setOpen(true); }}
              onClick={() => {
                filesPop.cancelClose();
                filesPop.setPinned((p) => !p);
                filesPop.setOpen(true);
              }}
            >
              <IconFileText size={12} />
              <span className="tc-seg-num">{fileCount} 待审</span>
              {(fileAdds > 0 || fileDels > 0) && (
                <span className="tc-seg-diff">
                  {fileAdds > 0 && <span className="add">+{fileAdds}</span>}
                  {fileDels > 0 && <span className="del">-{fileDels}</span>}
                </span>
              )}
              <IconChevronUp size={12} className="tc-seg-caret" />
            </button>
          </div>
        )}

      {/* 浮层放在**胶囊内部**渲染：胶囊即其包含块，`--tc-pop-x`（触发段中心相对胶囊的百分比）
          才能正确换算成"胶囊正上方居中"（#3）。绝对定位不参与 flex 布局，不影响胶囊尺寸。 */}
      {progressPop.open && (
        <div className="tc-pop" role="dialog" aria-label="任务进度" onMouseEnter={progressPop.cancelClose}>
          <div className="tc-pop-head">
            <span>{useTodo ? "执行清单" : "任务步骤"}</span>
            <span className="tc-pop-count">{done}/{displayRows.length}</span>
          </div>
          <div className="tc-pop-body">
            {displayRows.map((row) => (
              <div className={`tc-step ${row.status}`} key={row.key}>
                <span className={`tc-step-dot ${row.status}`} />
                <span className="tc-step-title" title={row.title}>{row.title}</span>
                {row.note && <span className="tc-step-note" title={row.note}>{row.note}</span>}
              </div>
            ))}
          </div>
        </div>
      )}

      {filesPop.open && (
        <div className="tc-pop" role="dialog" aria-label="未审核变更" onMouseEnter={filesPop.cancelClose}>
          <div className="tc-pop-head">
            <span>未审核变更</span>
            <span className="tc-pop-count">{fileCount} 文件</span>
          </div>
          <div className="tc-pop-body">
            {groups.map((g) => (
              <div className="tc-group" key={g.turnId}>
                <div className="tc-group-head">
                  <span className="tc-group-label" title={g.label}>{g.label}</span>
                  <span className="tc-group-count">{g.files.length} 文件</span>
                  {(g.additions > 0 || g.deletions > 0) && (
                    <span className="tc-seg-diff">
                      {g.additions > 0 && <span className="add">+{g.additions}</span>}
                      {g.deletions > 0 && <span className="del">-{g.deletions}</span>}
                    </span>
                  )}
                  <span className="tc-group-ops">
                    <button
                      type="button"
                      className="tc-op"
                      title="该组全部标记为已审核"
                      onClick={() => reviewFiles(g.turnId, g.files.map((f) => f.path), true)}
                    >
                      <IconCheck size={11} />
                    </button>
                    <button
                      type="button"
                      className="tc-op danger"
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
                    <div className="tc-file" key={f.path}>
                      <FileBadge path={f.path} size={14} />
                      <span className="tc-file-name" title={f.path} onClick={() => void openDiff(g.turnId, f)}>
                        {name}
                        {dir && <span className="tc-file-dir">{dir}</span>}
                      </span>
                      {(f.additions > 0 || f.deletions > 0) && (
                        <span className="tc-seg-diff">
                          {f.additions > 0 && <span className="add">+{f.additions}</span>}
                          {f.deletions > 0 && <span className="del">-{f.deletions}</span>}
                        </span>
                      )}
                      <span className="tc-group-ops">
                        <button
                          type="button"
                          className="tc-op"
                          title="标记为已审核"
                          onClick={() => reviewFiles(g.turnId, [f.path], true)}
                        >
                          <IconCheck size={11} />
                        </button>
                        <button
                          type="button"
                          className="tc-op"
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
        </div>
      )}
      </div>
    </div>
  );
}
