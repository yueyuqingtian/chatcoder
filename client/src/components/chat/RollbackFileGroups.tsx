/** plan-20261109-rollback-grouped-fix：两个撤销入口共用分组清单，避免计数与原因展示不一致。 */
import { useState, type ReactNode } from "react";
import { Button, Collapse, DialogSection, InsetCard } from "../ui";
import { IconChevronRight, IconFileText } from "../icons";
import { splitFilePath } from "./FileBadge";

export interface RollbackFileRow {
  path: string;
  action?: string;
  conflict: boolean;
  reason?: string | null;
}

export function RollbackFileGroups<T extends RollbackFileRow>({ files, renderDetail, showAction = true }: {
  files: T[];
  renderDetail?: (file: T) => ReactNode;
  showAction?: boolean;
}) {
  const [expanded, setExpanded] = useState<string | null>(null);
  const groups = [
    { title: "保留文件", rows: files.filter((f) => f.conflict) },
    { title: "撤销改动", rows: files.filter((f) => !f.conflict) },
  ].filter((group) => group.rows.length > 0);

  return (
    <div className="rb-file-groups">
      {groups.map((group) => (
        <DialogSection key={group.title} title={group.title} aside={group.rows.length}>
          <InsetCard>
            {group.rows.map((file) => {
              const { dir, name } = splitFilePath(file.path);
              const canExpand = !file.conflict && renderDetail != null;
              const content = <>
                <IconFileText size={16} />
                <span className="rb-file-main">
                  <span className="rb-file-path"><span>{dir}</span>{name}</span>
                  {file.conflict && <span className="rb-file-reason">{file.reason || "无法校验文件状态"}</span>}
                </span>
                {showAction && <span className={`rb-file-state${file.conflict ? " is-kept" : ""}`}>
                  {file.conflict ? "保留" : file.action === "delete" ? "删除新增" : "恢复"}
                </span>}
                {canExpand && <span className="rb-file-caret"><IconChevronRight size={12} /></span>}
              </>;
              return (
                <div className="rb-file-item" key={file.path}>
                  {canExpand ? (
                    <Button variant="ghost" className="rb-file-row" aria-expanded={expanded === file.path}
                      onClick={() => setExpanded(expanded === file.path ? null : file.path)}>{content}</Button>
                  ) : <div className="rb-file-row">{content}</div>}
                  {canExpand && <Collapse open={expanded === file.path} lazy>{() => renderDetail(file)}</Collapse>}
                </div>
              );
            })}
          </InsetCard>
        </DialogSection>
      ))}
    </div>
  );
}
