/** v19: 主消息流子代理卡片——工具行风格（IconCpu + 「子代理」+ 任务名 + 状态）。
 * 点击在右侧面板打开该子代理的完整会话（与主消息流同款渲染）。
 * hover 仅显示右箭头，不变色（与工具行规范一致）。
 */
import { memo } from "react";
import { usePanelStore } from "../../store/panel";
import { IconCpu, IconSpinner, IconX, IconCheck, IconChevronRight } from "../icons";

export interface SubagentMetaLite {
  agentId: number;
  name: string;
  status: string; // running / done / failed / pending / in_progress
  /** plan-330-1648 M7: 失败/取消原因（有值时在行下方展示，避免“只有红叉没有原因”） */
  error?: string | null;
}

export const SubagentCard = memo(function SubagentCard({ meta }: { meta: SubagentMetaLite }) {
  const openSubagent = usePanelStore((s) => s.openSubagent);
  const running = meta.status === "running" || meta.status === "in_progress";
  const failed = meta.status === "failed" || meta.status === "cancelled";
  const done = meta.status === "done";

  const errText = failed && meta.error ? String(meta.error).trim() : "";

  return (
    <div className="tc-node tc-subagent">
      <div
        className="tc-row has-output tc-subagent-row"
        title={errText
          ? `${meta.name}（失败：${errText}）点击查看完整会话`
          : `${meta.name}（点击查看完整会话）`}
        onClick={() => openSubagent(meta.agentId, meta.name)}
      >
        <span className="tc-icon"><IconCpu size={13} /></span>
        <span className={"tc-verb" + (running ? " text-shine" : "")}>
          {running ? "子代理执行中" : done ? "子代理已完成" : "子代理失败"}
        </span>
        <span className="tc-query" title={meta.name}>{meta.name}</span>
        {running && <span className="tc-status wait"><IconSpinner size={11} /></span>}
        {done && <span className="tc-status ok"><IconCheck size={11} /></span>}
        {failed && <span className="tc-status fail"><IconX size={11} /></span>}
        <span className="tc-chevron"><IconChevronRight size={11} /></span>
      </div>
      {/* plan-330-1648 M7: 失败原因（单行截断，完整内容见 title / 右面板） */}
      {errText && (
        <div
          className="tc-subagent-error"
          title={errText}
          style={{
            fontSize: 11, lineHeight: 1.5, padding: "1px 0 3px 22px",
            color: "var(--danger, #e5484d)", whiteSpace: "nowrap",
            overflow: "hidden", textOverflow: "ellipsis",
          }}
        >
          {errText}
        </div>
      )}
    </div>
  );
});
