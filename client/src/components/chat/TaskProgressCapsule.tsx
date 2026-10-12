/** TaskProgressCapsule —— 输入框上方的任务进度 / 文件变更悬浮胶囊（plan-282-1421 第12项；
 *  plan-20261009-ui U6 视觉重做，逻辑不变）。
 *
 * 【信息架构 · 智能动态】
 *   仅有未完成 AI 任务 → 只显进度；仅有未审核文件 → 只显变更；两者都有 → 分块显示；
 *   两者都没有 → **不渲染**（不是"有数据就常驻"）。
 *
 * 【任务进度只由 AI 更新】
 *   数据源统一走 `chat/taskProgress.ts`（AI 的 todo 清单优先，其次引擎步骤），
 *   与右侧任务面板同源，保证两处显示一致。
 *
 * 【形态】
 *   悬浮在输入卡上方（absolute，不占布局、不贴边框）；hover / focus 弹浮层展示
 *   任务清单与文件变更（审核 / 查看 diff / 整组回滚）；点击钉住便于逐个审核。
 *
 * plan-282-1434（B2）：进度与变更拆成**两个独立浮层**——各自独立聚焦/展开/钉住。
 * plan-282-1441（#3）：聚焦**零颜色与边框变化**；浮层在**胶囊正上方居中**（用独立的 translate 属性居中，
 *   避免被入场动画终态的 transform 覆盖造成偏移与文字发虚）。
 * plan-20261009-ui U6：
 *   · 胶囊：16px 迷你进度环（进行中取信息蓝、被中断取警示色）+ `3/7` + 当前步骤 / 文件叠层图标 + 待审数 + 增删统计；
 *   · 浮层：标准浮层规格（圆角 14 / 内边距 6）+ 头部细进度条；步骤用状态图标（状态切换有过渡）；
 *     变更用文件类型图标，操作按钮统一 24px 并走 Tooltip（不再用原生 title）；
 *   · 浮层进出场走 usePresence（有退场动画）。 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useChatStore } from "../../store/chat";
import { usePanelStore } from "../../store/panel";
import { api, type FileChangeOut } from "../../api/client";
import { IconCheck, IconChevronUp, IconExternalLink, IconFileStack, IconRingProgress, IconRotateCcw } from "../icons";
import { Tooltip } from "../ui";
import { usePresence } from "../ui/usePresence";
import { FileIcon } from "../iconkit/FileIcon";
import { StatusIcon, normalizeStatus } from "../iconkit/StatusIcon";
import { splitFilePath } from "./FileBadge";
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
      // plan-282-1493：wrapRef.current 为 null（触发段已从 DOM 卸载，如内容清空后胶囊整体隐藏）
      // 时也视为“点击外部”，否则钉住状态会残留，段重新出现时浮层自动弹出。
      const t = e.target as Node;
      if (!wrapRef.current || !wrapRef.current.contains(t)) {
        // 浮层本体渲染在胶囊容器级（不在触发段内）：点在浮层里不算外部
        if ((t as HTMLElement).closest?.(".tcap-pop")) return;
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

  /** 与右侧任务面板同源（#3）：两处显示的步骤必然一致。
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
  const ringTone = displayRows.some((r) => r.status === "running")
    ? "var(--accent)"
    : displayRows.some((r) => r.status === "interrupted") ? "var(--warning)" : "var(--text-2)";

  /** 两个独立浮层（B2）：各自开合。
   *  plan-282-1441（#3）：浮层统一在胶囊**正上方居中**——定位基数是胶囊本身（见 .tcap-pop 的
   *  `left: 50%` + `translate: -50% 0`），不再随触发分段偏移。 */
  const progressPop = usePopoverControl();
  const filesPop = usePopoverControl();
  const progressPresence = usePresence(hasProgress && progressPop.open);
  const filesPresence = usePresence(hasFiles && filesPop.open);

  // plan-282-1493：触发段消失（如审核完最后一个文件 / 任务收尾）时，同步清空对应浮层的
  // open/pinned 状态——两者是组件级 state，不随段一起消失；若残留，AI 再次写盘让段回归时
  // 浮层会带着旧状态“自动弹出”（用户未聚焦即展开）。
  useEffect(() => {
    if (!hasFiles) {
      filesPop.setOpen(false);
      filesPop.setPinned(false);
    }
  }, [hasFiles, filesPop.setOpen, filesPop.setPinned]);
  useEffect(() => {
    if (!hasProgress) {
      progressPop.setOpen(false);
      progressPop.setPinned(false);
    }
  }, [hasProgress, progressPop.setOpen, progressPop.setPinned]);

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
      className="tcap-wrap"
      onMouseLeave={() => {
        progressPop.scheduleCloseUnlessPinned();
        filesPop.scheduleCloseUnlessPinned();
      }}
    >
      <div className="tcap">
        {hasProgress && (
          <div
            className="tcap-seg-wrap"
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
              className={"tcap-seg" + (progressPop.open ? " is-open" : "")}
              aria-expanded={progressPop.open}
              aria-label={`任务进度 ${done}/${displayRows.length}`}
              onClick={() => {
                // 点击 = 钉住（便于在右侧面板逐个审核）；再点或点外部 / Esc 解除
                progressPop.cancelClose();
                progressPop.setPinned((p) => !p);
                progressPop.setOpen(true);
              }}
            >
              {/* plan-128-518：这里是步骤进度环（x/y），保持原样；仅执行中的颜色与左侧会话转圈统一为 --success */}
              <IconRingProgress value={done / displayRows.length} size={16} strokeWidth={1.4} indicatorColor={ringTone} trackColor="color-mix(in srgb, var(--text-1) 14%, transparent)" />
              <span className="tcap-num">{done}/{displayRows.length}</span>
              {/* 不挂原生 title：hover 本段已经弹出浮层（含完整步骤清单与全文） */}
              {currentTitle && <span className="tcap-title">{currentTitle}</span>}
              <IconChevronUp size={12} className="tcap-caret" />
            </button>
          </div>
        )}

        {hasProgress && hasFiles && <span className="tcap-divider" />}

        {hasFiles && (
          <div
            className="tcap-seg-wrap"
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
              className={"tcap-seg" + (filesPop.open ? " is-open" : "")}
              aria-expanded={filesPop.open}
              aria-label={`未审核变更 ${fileCount} 个文件`}
              onClick={() => {
                filesPop.cancelClose();
                filesPop.setPinned((p) => !p);
                filesPop.setOpen(true);
              }}
            >
              <IconFileStack size={15} />
              <span className="tcap-num">{fileCount} 待审</span>
              <Diff add={fileAdds} del={fileDels} />
              <IconChevronUp size={12} className="tcap-caret" />
            </button>
          </div>
        )}

        {/* 浮层放在**胶囊内部**渲染：胶囊即其包含块，居中才是"胶囊正上方"（#3）。
            绝对定位不参与 flex 布局，不影响胶囊尺寸。 */}
        {hasProgress && progressPresence.mounted && (
          <div
            className="tcap-pop mo-pop"
            data-state={progressPresence.state}
            role="dialog"
            aria-label="任务进度"
            onMouseEnter={progressPop.cancelClose}
          >
            <div className="tcap-pop-head">
              <span className="tcap-pop-title">{useTodo ? "执行清单" : "任务步骤"}</span>
              <span className="tcap-pop-count">{done}/{displayRows.length}</span>
            </div>
            <div className="tcap-bar" aria-hidden><span style={{ width: `${(done / displayRows.length) * 100}%` }} /></div>
            <div className="tcap-pop-body">
              {displayRows.map((row) => (
                <div className={`tcap-step is-${row.status}`} key={row.key}>
                  <StatusIcon status={normalizeStatus(row.status)} size={15} />
                  <Tooltip content={row.title} side="top" delay={500}>
                    <span className="tcap-step-title">{row.title}</span>
                  </Tooltip>
                  {row.note && <span className="tcap-step-note">{row.note}</span>}
                </div>
              ))}
            </div>
          </div>
        )}

        {hasFiles && filesPresence.mounted && (
          <div
            className="tcap-pop mo-pop"
            data-state={filesPresence.state}
            role="dialog"
            aria-label="未审核变更"
            onMouseEnter={filesPop.cancelClose}
          >
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
                        <button type="button" className="tcap-op" aria-label="该组全部标记为已审核" onClick={() => reviewFiles(g.turnId, g.files.map((f) => f.path), true)}>
                          <IconCheck size={13} />
                        </button>
                      </Tooltip>
                      <Tooltip content="回滚该组变更（先预览确认）" side="top">
                        <button type="button" className="tcap-op is-danger" aria-label="回滚该组变更" onClick={() => void requestRollbackPreview(g.turnId)}>
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
                        <button type="button" className="tcap-file-name" onClick={() => void openDiff(g.turnId, f)}>
                          <span className="tcap-file-base">{name}</span>
                          {dir && <span className="tcap-file-dir">{dir}</span>}
                        </button>
                        <Diff add={f.additions ?? 0} del={f.deletions ?? 0} />
                        <span className="tcap-ops tcap-file-ops">
                          <Tooltip content="标记为已审核" side="top">
                            <button type="button" className="tcap-op" aria-label="标记为已审核" onClick={() => reviewFiles(g.turnId, [f.path], true)}>
                              <IconCheck size={13} />
                            </button>
                          </Tooltip>
                          <Tooltip content="查看差异" side="top">
                            <button type="button" className="tcap-op" aria-label="查看差异" onClick={() => void openDiff(g.turnId, f)}>
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
          </div>
        )}
      </div>
    </div>
  );
}
