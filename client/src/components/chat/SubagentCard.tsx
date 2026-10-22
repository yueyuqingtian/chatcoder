/** v19: 主消息流子代理卡片——工具行风格（IconCpu + 「子代理」+ 任务名 + 状态）。
 * 点击在右侧面板打开该子代理的完整会话（与主消息流同款渲染）。
 * hover 仅显示右箭头，不变色（与工具行规范一致）。
 */
import { memo } from "react";
import { usePanelStore } from "../../store/panel";
import { IconCpu, IconSpinner, IconX, IconCheck, IconChevronRight } from "../icons";
import { Tooltip } from "../ui";

export interface SubagentMetaLite {
  agentId: number;
  name: string;
  status: string; // running / done / failed / pending / in_progress
  /** plan-330-1648 M7: 失败/取消原因（有值时在行下方展示，避免“只有红叉没有原因”） */
  error?: string | null;
  /** plan-20261022-subagent-dispatch-fail：该次 spawn 被后端拒绝（并发已满 / 校验不过）——
   *  没有对应的子代理会话可打开，卡片不可点击，只展示失败原因。 */
  dispatchFailed?: boolean;
  /** plan-20261013-wf-order: 所属轮次、工作流程序与派发时刻——工作流节点卡据此定位到其创建位置 */
  turnId?: number | null;
  programId?: number | null;
  spawnMessageId?: number | null;
}

export const SubagentCard = memo(function SubagentCard({ meta }: { meta: SubagentMetaLite }) {
  const openSubagent = usePanelStore((s) => s.openSubagent);
  const running = meta.status === "running" || meta.status === "in_progress";
  const failed = meta.status === "failed" || meta.status === "cancelled";
  const done = meta.status === "done";

  const errText = failed && meta.error ? String(meta.error).trim() : "";
  // plan-20261022-subagent-dispatch-fail：派发被拒时没有对应会话，整行静态展示
  const dispatchFailed = meta.dispatchFailed === true;
  const clickable = !dispatchFailed && meta.agentId > 0;

  return (
    <div className="tc-node tc-subagent">
      <div
        className={"tc-row tc-subagent-row" + (clickable ? " has-output" : "")}
        title={dispatchFailed
          ? `${meta.name}（派发失败：${errText || "未知原因"}）`
          : errText
            ? `${meta.name}（失败：${errText}）点击查看完整会话`
            : `${meta.name}（点击查看完整会话）`}
        onClick={clickable ? () => openSubagent(meta.agentId, meta.name) : undefined}
      >
        <span className="tc-icon"><IconCpu size={13} /></span>
        <span className={"tc-verb" + (running ? " text-shine" : "")}>
          {running ? "子代理执行中" : dispatchFailed ? "子代理派发失败" : done ? "子代理已完成" : "子代理失败"}
        </span>
        <Tooltip content={meta.name}><span className="tc-query">{meta.name}</span></Tooltip>
        {running && <span className="tc-status wait"><IconSpinner size={11} /></span>}
        {done && <span className="tc-status ok"><IconCheck size={11} /></span>}
        {failed && <span className="tc-status fail"><IconX size={11} /></span>}
        {clickable && <span className="tc-chevron"><IconChevronRight size={11} /></span>}
      </div>
      {/* plan-330-1648 M7: 失败原因（单行截断，完整内容见 title / 右面板） */}
      {errText && (
        <Tooltip content={errText}>
          <div className="tc-subagent-error">
            {errText}
          </div>
        </Tooltip>
      )}
    </div>
  );
});
