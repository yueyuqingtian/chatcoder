/** 工具卡五个栏目的展示体（plan-156-619 S7c）。
 *
 * 分工：本文件只有「怎么显示」，取数与派生在 `useToolCardData.ts`，容器与折叠在
 * `SessionToolCard.tsx`。每个栏目一个独立组件、各自的空态返回 null——容器的渲染顺序
 * 因此天然与「有无内容」解耦。
 *
 * 视觉约定（项目规则）：
 *  · 不使用左侧装饰性竖线表达层级；层次靠间距 / 字重 / 底色；
 *  · 圆角只用令牌（卡片 --r-lg、内部控件 --r-md、胶囊 999px）；
 *  · 提示统一 ui/Tooltip，不新增原生 title。
 */
import { memo, useState } from "react";
import { IconCheck, IconCpu, IconGitBranch, IconSpinner } from "../../icons";
import { IxWorkflow } from "../../iconkit/StackIcons";
import { FileIcon } from "../../iconkit/FileIcon";
import { StatusIcon, normalizeStatus } from "../../iconkit/StatusIcon";
import { Tooltip } from "../../ui";
import { useChatStore } from "../../../store/chat";
import {
  GIT_LIST_LIMIT, SUBAGENT_COLLAPSE_AT,
  openGitPanel, openPlanDoc,
  type GitSectionData, type PlanSectionData, type ProcessSectionData,
  type SubagentSectionData, type WorkflowSectionData,
} from "./useToolCardData";

/** 子会话标题（进程行 chip 的悬浮提示与标签） */
function useSubsessionTitle(id: number | null | undefined): string {
  const subsessionsByParent = useChatStore((s) => s.subsessionsByParent);
  if (id == null) return "";
  for (const list of Object.values(subsessionsByParent)) {
    const hit = list.find((c) => c.id === id);
    if (hit) return hit.title || `子会话 #${id}`;
  }
  return `子会话 #${id}`;
}

/** 进程行尾的「子会话」chip：点击切到该子会话（plan-156-619 S6 的落地点）。 */
const OwnerChip = memo(function OwnerChip({ id }: { id: number }) {
  const title = useSubsessionTitle(id);
  const switchSession = useChatStore((s) => s.switchSession);
  return (
    <Tooltip content={`由「${title}」执行，点击切换`} side="left">
      <button
        type="button"
        className="tcard-chip tcard-chip-owner"
        onClick={(e) => { e.stopPropagation(); void switchSession(id); }}
      >
        <IconCpu size={10} />
        <span className="tcard-chip-text">{title}</span>
      </button>
    </Tooltip>
  );
});

export const GitSection = memo(function GitSection({ data }: { data: GitSectionData }) {
  return (
    <>
      {data.clean ? (
        <div className="tcard-empty">工作区干净</div>
      ) : (
        <>
          <div className="tcard-git-tools">
            <span className="tcard-git-branch">
              <IconGitBranch size={12} />
              <span className="tcard-git-branch-name">{data.branch || "detached"}</span>
            </span>
            <span className="tcard-git-count">{data.fileCount} 个文件</span>
            <button type="button" className="tcard-link" onClick={openGitPanel}>查看全部</button>
          </div>
          <div className="tcard-git-list">
            {data.items.slice(0, GIT_LIST_LIMIT).map((f) => (
              <Tooltip content={`${f.path}${f.staged ? "（已暂存）" : ""}`} side="left" key={f.path}>
                <div className="tcard-git-row">
                  <FileIcon name={f.path} size={13} />
                  <span className="tcard-git-name">{f.path.split(/[\\/]/).pop()}</span>
                  <span className="tcard-git-code" data-code={f.code.toUpperCase()}>{f.code.toUpperCase()}</span>
                </div>
              </Tooltip>
            ))}
          </div>
        </>
      )}
    </>
  );
});

export const PlanSection = memo(function PlanSection({ data }: { data: PlanSectionData }) {
  return (
    <button type="button" className="tcard-plan-row" onClick={() => openPlanDoc(data.planDocPath)}>
      <span className="tcard-plan-title">{data.task || "任务执行计划"}</span>
      <span className={"tcard-plan-state" + (data.awaiting ? " is-await" : "")}>
        {data.awaiting ? "待执行" : "执行中"}
      </span>
    </button>
  );
});

export const ProcessSection = memo(function ProcessSection({ data }: { data: ProcessSectionData }) {
  return (
    <>
      <div className="tcard-progress-bar" aria-hidden>
        <span style={{ width: `${data.total > 0 ? (data.done / data.total) * 100 : 0}%` }} />
      </div>
      <div className="tcard-proc-list">
        {data.rows.map((row) => (
          <div className={`tcard-proc-row is-${row.status}`} key={row.key}>
            <StatusIcon status={normalizeStatus(row.status)} size={13} />
            <Tooltip content={row.title} side="left">
              <span className="tcard-proc-title">{row.title}</span>
            </Tooltip>
            {row.owner != null && <OwnerChip id={row.owner} />}
          </div>
        ))}
      </div>
    </>
  );
});

export const SubagentSection = memo(function SubagentSection({ data }: { data: SubagentSectionData }) {
  // 已结束条目超过阈值才折叠（用户指定：> 4 个）；运行中 / 当前轮次永不折叠
  const collapsible = data.finished.length > SUBAGENT_COLLAPSE_AT;
  const [expandedFinished, setExpandedFinished] = useState(false);
  const visibleFinished = collapsible && !expandedFinished
    ? data.finished.slice(0, SUBAGENT_COLLAPSE_AT)
    : data.finished;
  return (
    <div className="tcard-sub-list">
      {data.running.map((s) => (
        <div className="tcard-sub-row" key={`run-${s.id}`}>
          <IconSpinner size={12} />
          <Tooltip content={s.name} side="left"><span className="tcard-sub-name">{s.name}</span></Tooltip>
        </div>
      ))}
      {visibleFinished.map((s) => (
        <div className="tcard-sub-row is-done" key={`done-${s.id}`}>
          <IconCheck size={12} />
          <Tooltip content={s.name} side="left"><span className="tcard-sub-name">{s.name}</span></Tooltip>
        </div>
      ))}
      {collapsible && (
        <button
          type="button"
          className="tcard-sub-more"
          onClick={(e) => { e.stopPropagation(); setExpandedFinished((v) => !v); }}
        >
          {expandedFinished
            ? "收起"
            : `已完成 ${data.finished.length} 项`}
        </button>
      )}
    </div>
  );
});

export const WorkflowSection = memo(function WorkflowSection({ data }: { data: WorkflowSectionData }) {
  return (
    <div className="tcard-wf-row">
      <IxWorkflow size={12} />
      <Tooltip content={data.title} side="left"><span className="tcard-wf-title">{data.title}</span></Tooltip>
      <span className="tcard-wf-count">{data.done}/{data.total}</span>
    </div>
  );
});
