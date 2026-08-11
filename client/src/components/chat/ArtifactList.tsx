/** ArtifactList（v6/v12）：turn 产物文件清单。
 * 展示修改/新增/删除的文件，支持展开查看 diff、单文件审查、批量审查、单条撤销（turn 级回滚）。
 * v12：由消息 content.artifact_ids 反查 store 产物标题/摘要展示；「查看」展开完整文件清单。
 */
import { memo, useMemo, useState } from "react";
import type { MessageOut } from "../../api/client";
import { usePanelStore } from "../../store/panel";
import { useChatStore } from "../../store/chat";
import { IconFileText, IconExternalLink, IconChevronDown, IconChevronUp, IconRotateCcw, IconCheck } from "../icons";

export const ArtifactList = memo(function ArtifactList({ msgs, turnId, rolledBack = false }: {
  msgs: MessageOut[];
  turnId?: number | null;
  /** v12: 该 turn 已回滚：整体灰置并隐藏回滚入口。 */
  rolledBack?: boolean;
}) {
  const setPreviewPath = usePanelStore((s) => s.setPreviewPath);
  const openPanel = usePanelStore((s) => s.openPanel);
  const openTab = usePanelStore((s) => s.openTab);
  const reviewedFiles = useChatStore((s) => s.reviewedFiles);
  const markFileReviewed = useChatStore((s) => s.markFileReviewed);
  const requestRollbackPreview = useChatStore((s) => s.requestRollbackPreview);
  const artifacts = useChatStore((s) => s.artifacts);
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

  // v12: 由消息 artifact_ids 反查 store 产物（title/summary），查不到时降级为仅文件清单
  const relatedArtifacts = useMemo(() => {
    const ids = new Set<number>();
    for (const m of msgs) {
      const c = m.content as Record<string, unknown>;
      const list = Array.isArray(c.artifact_ids) ? c.artifact_ids.map(Number) : [];
      list.forEach((id) => { if (Number.isFinite(id)) ids.add(id); });
    }
    if (ids.size === 0) return [];
    return artifacts.filter((a) => ids.has(a.id));
  }, [msgs, artifacts]);

  if (msgs.length === 0) return null;

  const openFile = (f: string) => {
    setPreviewPath(f);
    openPanel();
    openTab("files");
  };

  const openAll = () => {
    // v12: 展开文件清单并打开右侧文件面板（不再只打开第一个文件）
    setExpanded(true);
    const f = stats.files[0];
    if (f) openFile(f);
  };

  const allReviewed = stats.files.every((f) => reviewedFiles[f]);

  return (
    <div className={`artifact-bar-wrap${rolledBack ? " rolled-back" : ""}`}>
      <style>{`
        .artifact-list-v12 { padding: 2px 10px 6px; display: flex; flex-direction: column; gap: 4px; }
        .artifact-title-row {
          display: flex; align-items: center; gap: 6px;
          font-size: 12px; color: var(--text-2); font-weight: 600;
          max-width: 100%;
        }
        .artifact-title { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .artifact-title-count { font-size: 11px; color: var(--text-3); font-weight: 400; flex-shrink: 0; }
        .artifact-bar-wrap.rolled-back { opacity: 0.55; pointer-events: none; }
        .artifact-bar-wrap.rolled-back .artifact-bar-count::after {
          content: "（已随回滚撤销）"; color: var(--text-3); font-weight: 400;
        }
      `}</style>
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
              <button className="artifact-file-btn" onClick={openAll} title="展开文件清单并打开文件面板">
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
          {relatedArtifacts.length > 0 && (
            <div className="artifact-list-v12">
              {relatedArtifacts.map((a) => (
                <div key={a.id} className="artifact-title-row" title={a.summary ?? undefined}>
                  <span className="artifact-title">{a.title || `产物 #${a.id}`}</span>
                  {(a.files ?? []).length > 0 && (
                    <span className="artifact-title-count">{(a.files ?? []).length} 文件</span>
                  )}
                </div>
              ))}
            </div>
          )}
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
                {turnId != null && !rolledBack && (
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
