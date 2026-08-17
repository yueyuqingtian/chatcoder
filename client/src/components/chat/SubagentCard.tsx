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
}

export const SubagentCard = memo(function SubagentCard({ meta }: { meta: SubagentMetaLite }) {
  const openSubagent = usePanelStore((s) => s.openSubagent);
  const running = meta.status === "running" || meta.status === "in_progress";
  const failed = meta.status === "failed" || meta.status === "cancelled";
  const done = meta.status === "done";

  return (
    <div className="tc-node tc-subagent">
      <div
        className="tc-row has-output tc-subagent-row"
        title={`${meta.name}（点击查看完整会话）`}
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
    </div>
  );
});
