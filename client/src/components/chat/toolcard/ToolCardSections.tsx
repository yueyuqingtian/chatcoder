/** 工具卡五个栏目的展示体（plan-156-619 S7c；plan-156-620 重做样式与交互）。
 *
 * 分工：本文件只有「怎么显示 / 点了去哪」，取数与派生在 `useToolCardData.ts`，
 * 容器与折叠在 `SessionToolCard.tsx`。
 *
 * 交互（plan-156-620）：每一行都通向右侧面板里的详情——
 *  · Git 文件 → Git 标签并展开该文件差异；分支行 → Git 标签（提交 / 推送）；
 *  · 计划 → 文件标签预览计划文档；
 *  · 子代理 → 子代理标签查看执行过程；
 *  · 工作流 → 工作流标签并定位到该运行；
 *  · 进程步骤由子会话执行时，行尾「子会话」标记切换到该子会话。
 * 文本被省略时，悬停或键盘聚焦该行显示完整内容（ui/Tooltip，不用原生 title）。
 *
 * 视觉约定（项目规则）：
 *  · 不使用左侧装饰性竖线表达层级；层次靠间距 / 字重 / 底色；
 *  · 行首图标统一放在 `.tcard-ic` 固定槽位（--flow-icon，与消息流工具行同规格），
 *    状态切换（转圈 → 勾）不改变文字起点。
 */
import { memo, useLayoutEffect, useRef, useState, type ReactNode, type RefObject } from "react";
import { IconArrowRight, IconCheckCircle, IconCpu, IconGitBranch, IconListChecks, IconSpinner } from "../../icons";
import { IxWorkflow } from "../../iconkit/StackIcons";
import { FileIcon } from "../../iconkit/FileIcon";
import { StatusIcon } from "../../iconkit/StatusIcon";
import { Button, Tooltip } from "../../ui";
import { useChatStore } from "../../../store/chat";
import { usePanelStore } from "../../../store/panel";
import { useWorkflowStore } from "../../../store/workflow";
import type { ProgressRow } from "../taskProgress";
import {
  GIT_LIST_LIMIT, SUBAGENT_COLLAPSE_AT,
  openGitPanel, openPlanDoc, revealPanelTab,
  type GitSectionData, type PlanSectionData, type ProcessSectionData,
  type SubagentSectionData, type WorkflowSectionData,
} from "./useToolCardData";

/** 文本是否被省略（单行 ellipsis 或多行 clamp）；尺寸变化时重测。 */
function useOverflow(ref: RefObject<HTMLElement | null>, text: string): boolean {
  const [over, setOver] = useState(false);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const check = () => setOver(el.scrollWidth > el.clientWidth + 1 || el.scrollHeight > el.clientHeight + 1);
    check();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(check);
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref, text]);
  return over;
}

interface RowProps {
  icon: ReactNode;
  text: string;
  /** 允许两行（进程步骤），否则单行省略 */
  wrap?: boolean;
  strong?: boolean;
  className?: string;
  /** 行尾附加（状态码 / 计数 / 子会话标记） */
  trailing?: ReactNode;
  /** 固定提示（如 Git 完整路径）；缺省时仅在文本被省略时提示全文 */
  tip?: string;
  onClick?: () => void;
}

/** 卡片里的一行：可点击时是按钮（可聚焦），省略时悬停 / 聚焦提示全文。 */
const Row = memo(function Row({ icon, text, wrap, strong, className = "", trailing, tip, onClick }: RowProps) {
  const textRef = useRef<HTMLSpanElement>(null);
  const over = useOverflow(textRef, text);
  const tipText = tip ?? (over ? text : null);
  const textCls = "tcard-row-text" + (wrap ? " tcard-row-wrap" : "") + (strong ? " tcard-row-strong" : "");
  const inner = (
    <>
      <span className="tcard-ic">{icon}</span>
      <span ref={textRef} className={textCls}>{text}</span>
      {trailing}
    </>
  );
  const el = onClick ? (
    <button type="button" className={`tcard-row tcard-row-btn ${className}`} onClick={onClick}>{inner}</button>
  ) : (
    // 被省略的不可点击行也要能被键盘聚焦，才能看到完整内容
    <div className={`tcard-row ${className}`} tabIndex={over ? 0 : undefined}>{inner}</div>
  );
  return <Tooltip content={tipText} disabled={!tipText} side="left">{el}</Tooltip>;
});

/** 子会话标题（进程行标记的提示） */
function useSubsessionTitle(id: number): string {
  return useChatStore((s) => {
    for (const list of Object.values(s.subsessionsByParent)) {
      const hit = list.find((c) => c.id === id);
      if (hit) return hit.title || `子会话 #${id}`;
    }
    return `子会话 #${id}`;
  });
}

/** 进程行尾的「子会话」标记：点击切到该子会话。 */
const OwnerChip = memo(function OwnerChip({ id }: { id: number }) {
  const title = useSubsessionTitle(id);
  const switchSession = useChatStore((s) => s.switchSession);
  return (
    <Tooltip content={`由子会话「${title}」执行，点击切换`} side="left">
      <button
        type="button"
        className="tcard-chip"
        onClick={(e) => { e.stopPropagation(); void switchSession(id); }}
      >
        <IconCpu size={11} />
        <span className="tcard-chip-text">子会话</span>
      </button>
    </Tooltip>
  );
});

/** 进程行状态图标：完成 = 圆勾、进行中 = 转圈、待办 = 箭头、中断 = 警示。 */
function ProcessIcon({ status }: { status: ProgressRow["status"] }) {
  if (status === "done") return <IconCheckCircle size={15} />;
  if (status === "running") return <IconSpinner size={14} />;
  if (status === "interrupted") return <StatusIcon status="interrupted" size={14} />;
  return <IconArrowRight size={14} />;
}

export const GitSection = memo(function GitSection({ data }: { data: GitSectionData }) {
  const openGitFile = usePanelStore((s) => s.openGitFile);
  // plan-156-621：焦点带上项目标识，GitPanel 消费时校验，避免跨项目把旧路径当当前仓库的变更
  const projectId = useChatStore((s) => s.currentProjectId);
  const rest = data.fileCount - GIT_LIST_LIMIT;
  return (
    <>
      {data.items.slice(0, GIT_LIST_LIMIT).map((f) => (
        <Row
          key={f.path}
          icon={<FileIcon name={f.path} size={14} />}
          text={f.path.split(/[\\/]/).pop() || f.path}
          tip={`${f.path}${f.staged ? "（已暂存）" : ""} · 点击查看差异`}
          trailing={<span className="tcard-git-code" data-code={f.code.toUpperCase()}>{f.code.toUpperCase()}</span>}
          onClick={() => openGitFile(f.path, f.staged, projectId)}
        />
      ))}
      <Row
        className="tcard-row-sub"
        icon={<IconGitBranch size={14} />}
        text={`${data.branch || "detached"}${rest > 0 ? ` · 另有 ${rest} 个文件` : ""}`}
        trailing={<span className="tcard-row-hint">提交与推送</span>}
        onClick={openGitPanel}
      />
    </>
  );
});

export const PlanSection = memo(function PlanSection({ data }: { data: PlanSectionData }) {
  return (
    <Row
      icon={<IconListChecks size={15} />}
      text={data.title}
      strong
      tip={`${data.title} · 点击查看计划文档`}
      onClick={() => openPlanDoc(data.planDocPath)}
    />
  );
});

export const ProcessSection = memo(function ProcessSection({ data }: { data: ProcessSectionData }) {
  return (
    <>
      {data.rows.map((row) => (
        <Row
          key={row.key}
          className={`tcard-proc is-${row.status}`}
          icon={<ProcessIcon status={row.status} />}
          text={row.title}
          wrap
          trailing={row.owner != null ? <OwnerChip id={row.owner} /> : undefined}
        />
      ))}
    </>
  );
});

export const SubagentSection = memo(function SubagentSection({ data }: { data: SubagentSectionData }) {
  const openSubagent = usePanelStore((s) => s.openSubagent);
  // 已结束条目超过阈值才折叠（用户指定：> 4 个）；运行中 / 当前轮次永不折叠
  const collapsible = data.finished.length > SUBAGENT_COLLAPSE_AT;
  const [expandedFinished, setExpandedFinished] = useState(false);
  const visibleFinished = collapsible && !expandedFinished
    ? data.finished.slice(0, SUBAGENT_COLLAPSE_AT)
    : data.finished;
  return (
    <>
      {data.running.map((s) => (
        <Row
          key={`run-${s.id}`}
          icon={<IconSpinner size={14} />}
          text={s.name}
          tip={`${s.name} · 点击查看执行过程`}
          onClick={() => openSubagent(s.id, s.name)}
        />
      ))}
      {visibleFinished.map((s) => (
        <Row
          key={`done-${s.id}`}
          className="is-done"
          icon={<IconCheckCircle size={15} />}
          text={s.name}
          tip={`${s.name} · 点击查看执行过程`}
          onClick={() => openSubagent(s.id, s.name)}
        />
      ))}
      {collapsible && (
        <Button
          variant="ghost"
          size="xs"
          className="tcard-more"
          onClick={() => setExpandedFinished((v) => !v)}
        >
          {expandedFinished ? "收起已完成" : `展开其余 ${data.finished.length - SUBAGENT_COLLAPSE_AT} 个已完成`}
        </Button>
      )}
    </>
  );
});

export const WorkflowSection = memo(function WorkflowSection({ data }: { data: WorkflowSectionData }) {
  const openWorkflow = () => {
    // 先定位到该运行，再打开工作流标签：面板挂载时直接呈现这一份详情
    useWorkflowStore.getState().focus(data.id);
    revealPanelTab("workflow");
  };
  return (
    <Row
      icon={<IxWorkflow size={14} />}
      text={data.title}
      tip={`${data.title} · 点击查看工作流详情`}
      trailing={<span className="tcard-row-hint">{data.done}/{data.total}</span>}
      onClick={openWorkflow}
    />
  );
});
