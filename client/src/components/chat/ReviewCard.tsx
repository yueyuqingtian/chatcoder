/** ReviewCard（v13 对齐 ZCode 变更集卡片）：
 * 头部：⌄ N 个文件已更改 +x -y …… 右侧「撤销」
 * 文件行：类型徽标 + 文件名(粗体) + 目录路径(灰) + +x -y + [审查] + [打开]
 * 底部：全部通过 / 取消全部 + 已审进度
 * 点击文件行 → 右侧面板 Monaco DiffEditor 展示 before/after。
 */
import { memo, useState } from "react";
import type { FileChangeOut } from "@chatcoder/shared";
import { api } from "../../api/client";
import { useChatStore } from "../../store/chat";
import { usePanelStore } from "../../store/panel";
import { IconCheck, IconChevronDown, IconRotateCcw } from "../icons";
import { FileBadge, splitFilePath } from "./FileBadge";

export const ReviewCard = memo(function ReviewCard({ turnId, isRunning, onRollback }: {
  turnId: number | null;
  isRunning: boolean;
  onRollback?: () => void;
}) {
  const changes = useChatStore((s) => (turnId != null ? s.turnChanges[turnId] : undefined));
  const reviewFiles = useChatStore((s) => s.reviewFiles);
  const setPreviewPath = usePanelStore((s) => s.setPreviewPath);
  const setDiffPreview = usePanelStore((s) => s.setDiffPreview);
  const openPanel = usePanelStore((s) => s.openPanel);
  const openTab = usePanelStore((s) => s.openTab);
  const [expanded, setExpanded] = useState(false);

  if (isRunning || turnId == null) return null;
  const files = changes ?? [];
  if (files.length === 0) return null;

  const reviewedCount = files.filter((f) => f.reviewed).length;
  const allReviewed = reviewedCount === files.length;
  const totalAdd = files.reduce((sum, f) => sum + (f.additions ?? 0), 0);
  const totalDel = files.reduce((sum, f) => sum + (f.deletions ?? 0), 0);

  const openDiff = async (f: FileChangeOut) => {
    setPreviewPath(f.path);
    openPanel();
    openTab("files");
    try {
      const d = await api.getFileDiff(turnId, f.path);
      setDiffPreview({ path: d.path, before: d.before, after: d.after, truncated: d.truncated });
    } catch { /* diff 拉取失败则回退为普通文件预览 */ }
  };

  const setAll = (reviewed: boolean) => {
    reviewFiles(turnId, files.map((f) => f.path), reviewed);
  };

  return (
    <div className="review-card">
      <div className="review-card-head" onClick={() => setExpanded((v) => !v)}>
        <span className={`review-card-caret${expanded ? " open" : ""}`}><IconChevronDown size={13} /></span>
        <span className="review-card-title">
          {files.length} 个文件已更改
          {(totalAdd > 0 || totalDel > 0) && (
            <span className="review-diff">
              {totalAdd > 0 && <span className="add">+{totalAdd}</span>}
              {totalDel > 0 && <span className="del">-{totalDel}</span>}
            </span>
          )}
        </span>
        {!allReviewed && <span className="review-pending-count">{files.length - reviewedCount} 待审核</span>}
        {onRollback && (
          <button
            className="review-rollback"
            onClick={(e) => { e.stopPropagation(); onRollback(); }}
            title="撤销本轮全部改动"
          >
            <IconRotateCcw size={12} /> 撤销
          </button>
        )}
      </div>

      {expanded && (
        <div className="review-body">
          {files.map((f) => {
            const { dir, name } = splitFilePath(f.path);
            return (
              <div key={f.path} className="review-file" onClick={() => openDiff(f)}>
                <FileBadge path={f.path} />
                <span className="review-file-name">{name}</span>
                <span className="review-file-dir" title={f.path}>{dir}</span>
                {(f.additions > 0 || f.deletions > 0) && (
                  <span className="review-diff">
                    {f.additions > 0 && <span className="add">+{f.additions}</span>}
                    {f.deletions > 0 && <span className="del">-{f.deletions}</span>}
                  </span>
                )}
                <span className="review-file-actions" onClick={(e) => e.stopPropagation()}>
                  <button
                    className={"review-btn" + (f.reviewed ? " done" : "")}
                    onClick={() => reviewFiles(turnId, [f.path], !f.reviewed)}
                    title={f.reviewed ? "已审核，点击取消" : "标记为已审核"}
                  >
                    {f.reviewed ? <><IconCheck size={11} /> 已审</> : "审查"}
                  </button>
                  <button className="review-btn" onClick={() => openDiff(f)}>打开</button>
                </span>
              </div>
            );
          })}

          <div className="review-footer">
            <div className="review-footer-actions">
              <button className="review-btn" onClick={() => setAll(true)} disabled={allReviewed}>全部通过</button>
              <button className="review-btn" onClick={() => setAll(false)} disabled={reviewedCount === 0}>取消全部</button>
            </div>
            <span className="review-progress">已审 {reviewedCount}/{files.length}</span>
          </div>
        </div>
      )}
    </div>
  );
});
