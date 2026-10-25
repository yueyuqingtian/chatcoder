/** v19: 主消息流子代理卡片——工具行风格（IconCpu + 「子代理」+ 任务名 + 状态）。
 * 点击在右侧面板打开该子代理的完整会话（与主消息流同款渲染）。
 * hover 仅显示右箭头，不变色（与工具行规范一致）。
 */
import { memo } from "react";
import { usePanelStore } from "../../store/panel";
import { IconBot, IconSpinner, IconX, IconCheck, IconChevronRight, IconPause } from "../icons";
import { Tooltip } from "../ui";
import { subagentState, type SubagentState } from "./subagentStatus";

/** 状态词与右面板摘要、工具卡同一口径（subagentStatus.ts，plan-20261025-progress-keep）：
 *  已取消 / 已终止是「已停止」而不是失败，排队中的是「待开始」——此前一律写「失败」，与摘要的「已停止」自相矛盾 */
const STATE_TEXT: Record<SubagentState, string> = {
  running: "执行中", done: "已完成", failed: "失败", stopped: "已停止", pending: "待开始",
};

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
  const state = subagentState(meta.status);
  const running = state === "running";
  const failed = state === "failed";
  const stopped = state === "stopped";
  const done = state === "done";

  const errText = (failed || stopped) && meta.error ? String(meta.error).trim() : "";
  // plan-20261022-subagent-dispatch-fail：派发被拒时没有对应会话，整行静态展示
  const dispatchFailed = meta.dispatchFailed === true;
  const clickable = !dispatchFailed && meta.agentId > 0;

  // plan-20261025-shell（需求 1）：与工具行同一写法——「子代理 任务名 · 状态」，图标与其它行同为 16px 槽位；
  // 状态只在行尾（转圈 / 勾 / 叉 / 暂停 + 一个弱色状态词），动作词不再随状态改写，整列起点与宽度稳定
  const stateText = running ? "执行中" : dispatchFailed ? "派发失败" : STATE_TEXT[state];
  const tip = dispatchFailed
    ? `${meta.name}（派发失败：${errText || "未知原因"}）`
    : errText
      ? `${meta.name}（${stopped ? "已停止" : "失败"}：${errText}）点击查看完整会话`
      : `${meta.name}（点击查看完整会话）`;
  return (
    <div className="tc-node tc-subagent">
      <Tooltip content={tip}>
        <div
          className={"tc-row tc-subagent-row" + (clickable ? " has-output" : "")}
          data-tool-state={running ? "running" : failed ? "fail" : "done"}
          onClick={clickable ? () => openSubagent(meta.agentId, meta.name) : undefined}
        >
          <span className="tc-icon"><IconBot size={16} /></span>
          <span className={"tc-verb" + (running ? " text-shine" : "")}>子代理</span>
          <span className="tc-query">{meta.name}</span>
          <span className="tc-sep" aria-hidden>·</span>
          <span className="tc-meta">{stateText}</span>
          {running && <span className="tc-status wait"><IconSpinner size={11} /></span>}
          {done && <span className="tc-status ok"><IconCheck size={11} /></span>}
          {failed && <span className="tc-status fail"><IconX size={11} /></span>}
          {stopped && <span className="tc-status stop"><IconPause size={11} /></span>}
          {clickable && <span className="tc-chevron"><IconChevronRight size={11} /></span>}
        </div>
      </Tooltip>
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
