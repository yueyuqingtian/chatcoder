/** ArtifactBar（v4 r3 完全重写）：§3.3.4 turn 末产物横向条。
 * 📄 N 个文件已更改 ⟳  +XXX −YYY  ↗
 * 点击 → 右侧面板打开文件预览
 */
import { useMemo } from "react";
import type { MessageOut } from "../../api/client";
import { usePanelStore } from "../../store/panel";
import { IconFileText, IconExternalLink } from "../icons";

export function ArtifactList({ msgs }: { msgs: MessageOut[] }) {
  const { setPreviewPath, openPanel, openTab } = usePanelStore();

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

  const openFirst = () => {
    const f = stats.files[0];
    if (f) {
      setPreviewPath(f);
      openPanel();
      openTab("files");
    }
  };

  return (
    <div className="artifact-bar" onClick={openFirst} title="点击查看变更详情">
      <IconFileText size={13} />
      <span className="artifact-bar-count">{stats.totalFiles} 个文件已更改</span>
      {(stats.additions > 0 || stats.deletions > 0) && (
        <span className="artifact-bar-diff">
          <span className="add">+{stats.additions}</span>
          <span className="del">−{stats.deletions}</span>
        </span>
      )}
      <button className="artifact-bar-open" title="查看详情" onClick={(e) => { e.stopPropagation(); openFirst(); }}>
        <IconExternalLink size={12} />
      </button>
    </div>
  );
}