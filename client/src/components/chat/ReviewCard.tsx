/** ReviewCard（v11）：turn 完成后的「变更审核」卡片。
 *
 * 展示该 turn 写盘变更清单（修改/新增/删除 + 增删行数），支持：
 * - 单条审核：逐文件勾选/取消，乐观更新 + PUT 持久化
 * - 批量审核：「全部通过」/「取消全部」
 * - 点击文件 → 右侧面板「文件」标签页 Monaco DiffEditor 展示 before/after
 * 与 ArtifactList 折叠条并存互不影响；卡片仅在 turn 完成且存在变更时显示，可折叠收起。
 */
import { memo, useState } from "react";
import type { FileChangeOut } from "@chatcoder/shared";
import { api } from "../../api/client";
import { useChatStore } from "../../store/chat";
import { usePanelStore } from "../../store/panel";
import { IconCheck, IconChevronDown, IconChevronUp } from "../icons";

const ACTION_LABEL: Record<string, string> = {
  modified: "修改",
  added: "新增",
  deleted: "删除",
};

export const ReviewCard = memo(function ReviewCard({ turnId, isRunning }: {
  turnId: number | null;
  isRunning: boolean;
}) {
  const changes = useChatStore((s) => (turnId != null ? s.turnChanges[turnId] : undefined));
  const reviewFiles = useChatStore((s) => s.reviewFiles);
  const setPreviewPath = usePanelStore((s) => s.setPreviewPath);
  const setDiffPreview = usePanelStore((s) => s.setDiffPreview);
  const openPanel = usePanelStore((s) => s.openPanel);
  const openTab = usePanelStore((s) => s.openTab);
  const [expanded, setExpanded] = useState(true);

  // 仅在该 turn 完成（非运行中）且存在写盘变更时显示
  if (isRunning || turnId == null) return null;
  const files = changes ?? [];
  if (files.length === 0) return null;

  const reviewedCount = files.filter((f) => f.reviewed).length;
  const allReviewed = reviewedCount === files.length;
  const pendingCount = files.length - reviewedCount;

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
        <span className="review-card-title">
          变更审核
          {!allReviewed && (
            <span className="review-pending">
              <span className="review-pending-dot" />
              <span>{pendingCount} 个待审核</span>
            </span>
          )}
        </span>
        <button
          className="review-collapse"
          onClick={(e) => { e.stopPropagation(); setExpanded((v) => !v); }}
        >
          {expanded ? <IconChevronUp size={13} /> : <IconChevronDown size={13} />}
        </button>
      </div>

      {expanded && (
        <div className="review-body">
          {files.map((f) => (
            <div key={f.path} className="review-file" onClick={() => openDiff(f)}>
              <span
                className={"review-check" + (f.reviewed ? " checked" : "")}
                onClick={(e) => { e.stopPropagation(); reviewFiles(turnId, [f.path], !f.reviewed); }}
                title={f.reviewed ? "已审核，点击取消" : "标记为已审核"}
              >
                {f.reviewed && <IconCheck size={10} />}
              </span>
              <span className="review-file-name" title={f.path}>{f.path}</span>
              <span className={`review-action ${f.action}`}>{ACTION_LABEL[f.action] ?? f.action}</span>
              {(f.additions > 0 || f.deletions > 0) && (
                <span className="review-diff">
                  {f.additions > 0 && <span className="add">+{f.additions}</span>}
                  {f.deletions > 0 && <span className="del">−{f.deletions}</span>}
                </span>
              )}
            </div>
          ))}

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