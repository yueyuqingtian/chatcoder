/** ArtifactList（v6）：turn 产物文件清单。
 * 展示修改/新增/删除的文件，支持展开查看 diff、单文件审查、批量审查、单条撤销（turn 级回滚）。
 */
import { memo, useMemo, useState } from "react";
import type { MessageOut } from "../../api/client";
import { usePanelStore } from "../../store/panel";
import { useChatStore } from "../../store/chat";
import { IconFileText, IconExternalLink, IconChevronDown, IconChevronUp, IconRotateCcw, IconCheck } from "../icons";

export const ArtifactList = memo(function ArtifactList({ msgs, turnId }: { msgs: MessageOut[]; turnId?: number | null }) {
  const setPreviewPath = usePanelStore((s) => s.setPreviewPath);
  const openPanel = usePanelStore((s) => s.openPanel);
  const openTab = usePanelStore((s) => s.openTab);
  const reviewedFiles = useChatStore((s) => s.reviewedFiles);
  const markFileReviewed = useChatStore((s) => s.markFileReviewed);
  const requestRollbackPreview = useChatStore((s) => s.requestRollbackPreview);
  const [expanded, setExpanded] = useState(false);

  const stats = useMemo(() => {
    let totalFiles = 0;
    let additions = 0;
    let deletions = 0;
    const files: string[] = [];
    for (const m of msgs) {
      const c = m.content as Record<string, unknown>;
      const fileList = Array.isArray(c.files) ? c.files.map(String) : [];
      totalFiles += fileList.length || 1;
      files.push(...fileList);
      if (typeof c.additions === "number") additions += c.additions;
      if (typeof c.deletions === "number") deletions += c.deletions;
    }
    return { totalFiles, additions, deletions, files };
  }, [msgs]);

  if (msgs.length === 0) return null;

  const openFile = (f: string) => {
    setPreviewPath(f);
    openPanel();
    openTab("files");
  };

  const openAll = () => {
    const f = stats.files[0];
    if (f) openFile(f);
  };

  const allReviewed = stats.files.every((f) => reviewedFiles[f]);

  return (
    <div className="artifact-bar-wrap">
      <div className="artifact-bar" onClick={() => setExpanded((v) => !v)}>
        <IconFileText size={13} />
        <span className="artifact-bar-count">{stats.totalFiles} 个文件已更改</span>
        {(stats.additions > 0 || stats.deletions > 0) && (
          <span className="artifact-bar-diff">
            <span className="add">+{stats.additions}</span>
            <span className="del">−{stats.deletions}</span>
          </span>
        )}
        <button className="artifact-bar-open" title="展开文件清单" onClick={(e) => { e.stopPropagation(); setExpanded((v) => !v); }}>
          {expanded ? <IconChevronUp size={12} /> : <IconChevronDown size={12} />}
        </button>
      </div>

      {expanded && (
        <div className="artifact-files">
          <div className="artifact-files-head">
            <span className="artifact-files-title">文件变更</span>
            <div className="artifact-files-actions">
              <button className="artifact-file-btn" onClick={openAll} title="打开文件预览">
                <IconExternalLink size={12} /> 查看
              </button>
              <button
                className="artifact-file-btn"
                onClick={() => stats.files.forEach((f) => markFileReviewed(f, true))}
                disabled={allReviewed}
                title="全部标记为已审查"
              >
                <IconCheck size={12} /> 全部审查
              </button>
            </div>
          </div>
          {stats.files.length === 0 && <div className="artifact-files-empty">未记录具体文件路径</div>}
          {stats.files.map((f, i) => (
            <div key={f + i} className="artifact-file-row">
              <span
                className={`artifact-file-review${reviewedFiles[f] ? " reviewed" : ""}`}
                onClick={() => markFileReviewed(f, !reviewedFiles[f])}
                title={reviewedFiles[f] ? "已审查，点击取消" : "标记为已审查"}
              >
                {reviewedFiles[f] && <IconCheck size={11} />}
              </span>
              <span className="artifact-file-name" title={f} onClick={() => openFile(f)}>{f}</span>
              <div className="artifact-file-ops">
                <button className="artifact-file-btn" onClick={() => openFile(f)} title="查看变更">
                  <IconExternalLink size={11} />
                </button>
                {turnId != null && (
                  <button
                    className="artifact-file-btn danger"
                    onClick={() => requestRollbackPreview(turnId)}
                    title="回滚该 turn 变更（先预览确认）"
                  >
                    <IconRotateCcw size={11} />
                  </button>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
});
