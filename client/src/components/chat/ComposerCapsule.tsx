/** ComposerCapsule —— 输入框上方的统一胶囊：任务进度 / 工作流 / 产物三段，按有无内容动态出现。
 *
 * 【信息架构】
 *   任务进度：仅有未完成的 AI 任务时出现（数据源 chat/taskProgress.ts，与右侧任务面板同源）；
 *   工作流：本会话有待处理、进行中或暂停的工作流时出现（数据见 workflow/WorkflowCapsule.tsx）；
 *   产物：仅有未审核的文件变更时出现；三者都没有 → 不渲染（不是“有数据就常驻”）。
 *   段的顺序固定为 任务进度 → 工作流 → 产物，段的出现 / 消失不改变其它段的先后。
 *
 * 【形态】
 *   一个胶囊、一个浮层：浮层在胶囊正上方居中，内容随当前段切换，同一时间只展示一段（见 ui/useCapsulePopover）。
 *   hover / 聚焦即展开，点击钉住（再点、点外部或 Esc 解除）；钉住后划过其它段不切换，便于逐个处理。
 *
 * 【合并的原因】
 *   此前任务进度与产物各自一个浮层、工作流是另一个胶囊：划过段与段之间时旧浮层不收起，
 *   两个浮层会同时出现。合并后浮层只有一个，切换口径统一。 */
import { useEffect, useMemo, type FocusEvent } from "react";
import { useChatStore } from "../../store/chat";
import { usePanelStore } from "../../store/panel";
import { api, type FileChangeOut } from "../../api/client";
import { IconCheck, IconExternalLink, IconFileStack, IconRingProgress, IconRotateCcw } from "../icons";
import { IxWorkflow } from "../iconkit/StackIcons";
import { Badge, Button, Tooltip } from "../ui";
import { useCapsulePopover } from "../ui/useCapsulePopover";
import { FileIcon } from "../iconkit/FileIcon";
import { StatusIcon, normalizeStatus } from "../iconkit/StatusIcon";
import { WorkflowPopBody, useWorkflowCapsule } from "../workflow/WorkflowCapsule";
import { splitFilePath } from "./FileBadge";
import { useProgressRows } from "./taskProgress";

type Segment = "progress" | "workflow" | "files";

const POP_LABEL: Record<Segment, string> = { progress: "任务进度", workflow: "工作流", files: "未审核变更" };

interface ChangeGroup {
  turnId: number;
  label: string;
  files: FileChangeOut[];
  additions: number;
  deletions: number;
}

type ProgressRows = ReturnType<typeof useProgressRows>["rows"];

/** 增删统计（+N −N） */
function Diff({ add, del }: { add: number; del: number }) {
  if (add <= 0 && del <= 0) return null;
  return (
    <span className="tcap-diff">
      {add > 0 && <span className="add">+{add}</span>}
      {del > 0 && <span className="del">−{del}</span>}
    </span>
  );
}

/** 任务进度浮层：头部计数与细进度条 + 步骤清单（步骤状态图标有过渡） */
function ProgressPopBody({ rows, useTodo }: { rows: ProgressRows; useTodo: boolean }) {
  const done = rows.filter((r) => r.status === "done").length;
  const pct = rows.length > 0 ? (done / rows.length) * 100 : 0;
  return (
    <>
      <div className="tcap-pop-head">
        <span className="tcap-pop-title">{useTodo ? "执行清单" : "任务步骤"}</span>
        <span className="tcap-pop-count">{done}/{rows.length}</span>
      </div>
      <div className="tcap-bar" aria-hidden><span style={{ width: `${pct}%` }} /></div>
      <div className="tcap-pop-body">
        {rows.map((row) => (
          <div className={`tcap-step is-${row.status}`} key={row.key}>
            <StatusIcon status={normalizeStatus(row.status)} size={15} />
            <Tooltip content={row.title} side="top" delay={500}>
              <span className="tcap-step-title">{row.title}</span>
            </Tooltip>
            {row.note && <span className="tcap-step-note">{row.note}</span>}
          </div>
        ))}
      </div>
    </>
  );
}

/** 未审核变更浮层：按回合分组，组级与文件级的审核 / 回滚 / 查看差异 */
function FilesPopBody({ groups, fileCount, onReview, onRollback, onOpenDiff }: {
  groups: ChangeGroup[];
  fileCount: number;
  onReview: (turnId: number, paths: string[]) => void;
  onRollback: (turnId: number) => void;
  onOpenDiff: (turnId: number, f: FileChangeOut) => void;
}) {
  return (
    <>
      <div className="tcap-pop-head">
        <span className="tcap-pop-title">未审核变更</span>
        <span className="tcap-pop-count">{fileCount} 个文件</span>
      </div>
      <div className="tcap-pop-body">
        {groups.map((g) => (
          <div className="tcap-group" key={g.turnId}>
            <div className="tcap-group-head">
              <Tooltip content={g.label} side="top" delay={500}>
                <span className="tcap-group-label">{g.label}</span>
              </Tooltip>
              <span className="tcap-group-count">{g.files.length} 个文件</span>
              <Diff add={g.additions} del={g.deletions} />
              <span className="tcap-ops">
                <Tooltip content="该组全部标记为已审核" side="top">
                  <button type="button" className="tcap-op" aria-label="该组全部标记为已审核" onClick={() => onReview(g.turnId, g.files.map((f) => f.path))}>
                    <IconCheck size={13} />
                  </button>
                </Tooltip>
                <Tooltip content="回滚该组变更（先预览确认）" side="top">
                  <button type="button" className="tcap-op is-danger" aria-label="回滚该组变更" onClick={() => onRollback(g.turnId)}>
                    <IconRotateCcw size={13} />
                  </button>
                </Tooltip>
              </span>
            </div>
            {g.files.map((f) => {
              const { dir, name } = splitFilePath(f.path);
              return (
                <div className="tcap-file" key={f.path}>
                  <FileIcon name={f.path} size={16} />
                  <button type="button" className="tcap-file-name" onClick={() => onOpenDiff(g.turnId, f)}>
                    <span className="tcap-file-base">{name}</span>
                    {dir && <span className="tcap-file-dir">{dir}</span>}
                  </button>
                  <Diff add={f.additions ?? 0} del={f.deletions ?? 0} />
                  <span className="tcap-ops tcap-file-ops">
                    <Tooltip content="标记为已审核" side="top">
                      <button type="button" className="tcap-op" aria-label="标记为已审核" onClick={() => onReview(g.turnId, [f.path])}>
                        <IconCheck size={13} />
                      </button>
                    </Tooltip>
                    <Tooltip content="查看差异" side="top">
                      <button type="button" className="tcap-op" aria-label="查看差异" onClick={() => onOpenDiff(g.turnId, f)}>
                        <IconExternalLink size={13} />
                      </button>
                    </Tooltip>
                  </span>
                </div>
              );
            })}
          </div>
        ))}
      </div>
    </>
  );
}

export function ComposerCapsule({ onVisibilityChange }: {
  /** 上报胶囊当前是否真的渲染：消息流据此按需让位（未出现不预占位；出现时用高度过渡把消息流顶上去） */
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

  /** 与右侧任务面板同源：两处显示的步骤必然一致（展示口径收在 taskProgress 模块） */
  const progress = useProgressRows();
  const useTodo = progress.useTodo;
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
  // 只在"存在未完成任务"时展示任务段
  const hasProgress = displayRows.length > done;
  const fileCount = groups.reduce((n, g) => n + g.files.length, 0);
  const hasFiles = fileCount > 0;
  const fileAdds = groups.reduce((n, g) => n + g.additions, 0);
  const fileDels = groups.reduce((n, g) => n + g.deletions, 0);
  const currentTitle = displayRows.find((r) => r.status === "running")?.title
    ?? displayRows.find((r) => r.status !== "done")?.title
    ?? "";
  const ringTone = displayRows.some((r) => r.status === "running")
    ? "var(--accent)"
    : displayRows.some((r) => r.status === "interrupted") ? "var(--warning)" : "var(--text-2)";

  const wf = useWorkflowCapsule();
  const segments: Segment[] = [];
  if (hasProgress) segments.push("progress");
  if (wf.visible) segments.push("workflow");
  if (hasFiles) segments.push("files");
  const visible = segments.length > 0;
  // 出现 / 消失都上报；卸载（切会话、切设置页）时收回占位，避免消息流底部留一条死白
  useEffect(() => { onVisibilityChange?.(visible); }, [visible, onVisibilityChange]);
  useEffect(() => () => onVisibilityChange?.(false), [onVisibilityChange]);

  const cap = useCapsulePopover(segments);

  const openDiff = async (turnId: number, f: FileChangeOut) => {
    setPreviewPath(f.path);
    openPanel();
    openTab("files");
    try {
      const d = await api.getFileDiff(turnId, f.path);
      setDiffPreview({ path: d.path, before: d.before, after: d.after, truncated: d.truncated });
    } catch { /* 拉取失败回退普通预览 */ }
  };

  // 焦点在段之间移动时，只有离开整个胶囊才收起
  const leaveUnlessInside = (e: FocusEvent<HTMLDivElement>) => {
    if (e.currentTarget.contains(e.relatedTarget as Node | null)) return;
    cap.leave();
  };

  // 两者皆无 → 不渲染（存在即常驻是被弃用的旧行为）
  if (!visible) return null;

  const segClass = (seg: Segment) => "tcap-seg" + (cap.active === seg ? " is-open" : "");

  return (
    <div className="tcap-wrap" ref={cap.wrapRef} onMouseLeave={cap.leave}>
      <div className="tcap">
        {hasProgress && (
          <div
            className="tcap-seg-wrap is-shrinkable"
            onMouseEnter={() => cap.enter("progress")}
            onFocusCapture={() => cap.enter("progress")}
            onBlurCapture={leaveUnlessInside}
          >
            <button
              type="button"
              className={segClass("progress")}
              aria-expanded={cap.active === "progress"}
              aria-label={`任务进度 ${done}/${displayRows.length}`}
              onClick={() => cap.toggle("progress")}
            >
              {/* plan-128-518：这里是步骤进度环（x/y），保持原样；仅执行中的颜色与左侧会话转圈统一 */}
              <IconRingProgress value={done / displayRows.length} size={16} strokeWidth={1.4} indicatorColor={ringTone} trackColor="color-mix(in srgb, var(--text-1) 14%, transparent)" />
              <span className="tcap-num">{done}/{displayRows.length}</span>
              {/* 不挂原生 title：hover 本段已经弹出浮层（含完整步骤清单与全文） */}
              {currentTitle && <span className="tcap-title">{currentTitle}</span>}
            </button>
          </div>
        )}

        {wf.visible && wf.focus && (
          <div
            className="tcap-seg-wrap"
            onMouseEnter={() => cap.enter("workflow")}
            onFocusCapture={() => cap.enter("workflow")}
            onBlurCapture={leaveUnlessInside}
          >
            <button
              type="button"
              className={segClass("workflow")}
              aria-expanded={cap.active === "workflow"}
              aria-label={`工作流：${wf.title}，${wf.view?.label ?? ""}`}
              onClick={() => cap.toggle("workflow")}
            >
              <IxWorkflow size={14} />
              <span className="tcap-num"><span className="tcap-seg-word">工作流 </span>{wf.progress.done}/{wf.progress.total}</span>
              <span className="tcap-state" data-tone={wf.view?.tone}><span className="tcap-state-text">{wf.stateText}</span></span>
              {wf.conflicts > 0 && <Badge tone="warning" variant="soft" size="xs">冲突 {wf.conflicts}</Badge>}
              {wf.active.length > 1 && (
                <Tooltip content={`本会话还有 ${wf.active.length - 1} 个工作流`} side="top" delay={400}>
                  <span className="tcap-more">+{wf.active.length - 1}</span>
                </Tooltip>
              )}
            </button>
            {wf.focus.status === "paused" && (
              <Button
                className="tcap-resume"
                variant="primary"
                size="xs"
                disabled={!!wf.busy[wf.focus.id]}
                onClick={() => void wf.resume(wf.focus!.id)}
              >
                继续
              </Button>
            )}
          </div>
        )}

        {hasFiles && (
          <div
            className="tcap-seg-wrap"
            onMouseEnter={() => cap.enter("files")}
            onFocusCapture={() => cap.enter("files")}
            onBlurCapture={leaveUnlessInside}
          >
            <button
              type="button"
              className={segClass("files")}
              aria-expanded={cap.active === "files"}
              aria-label={`未审核变更 ${fileCount} 个文件`}
              onClick={() => cap.toggle("files")}
            >
              <IconFileStack size={15} />
              <span className="tcap-num">{fileCount} 待审</span>
              <Diff add={fileAdds} del={fileDels} />
            </button>
          </div>
        )}

        {/* 浮层放在**胶囊内部**渲染：胶囊即其包含块，居中才是“胶囊正上方”。绝对定位不参与 flex 布局。 */}
        {cap.popover.mounted && cap.popover.seg && (
          <div
            className="tcap-pop mo-pop"
            data-state={cap.popover.state}
            role="dialog"
            aria-label={POP_LABEL[cap.popover.seg]}
            onMouseEnter={cap.cancelClose}
          >
            {cap.popover.seg === "progress" && <ProgressPopBody rows={displayRows} useTodo={useTodo} />}
            {cap.popover.seg === "workflow" && (
              <WorkflowPopBody wf={wf} onOpenDetail={() => { cap.dismiss(); wf.openDetail(); }} />
            )}
            {cap.popover.seg === "files" && (
              <FilesPopBody
                groups={groups}
                fileCount={fileCount}
                onReview={(turnId, paths) => reviewFiles(turnId, paths, true)}
                onRollback={(turnId) => void requestRollbackPreview(turnId)}
                onOpenDiff={(turnId, f) => void openDiff(turnId, f)}
              />
            )}
          </div>
        )}
      </div>
    </div>
  );
}
