/** 工具卡栏目的展示体（plan-156-619 S7c；plan-20261019-toolcard-v2 重做）。
 *
 * 分工：本文件只有「怎么显示 / 点了去哪」，取数与派生在 `useToolCardData.ts`，
 * 容器与折叠在 `SessionToolCard.tsx`，Git 栏自成一体见 `GitToolSection.tsx`。
 *
 * 交互：
 *  · 计划 → 文件标签预览计划文档；进程步骤由子会话执行时，行尾「子会话」标记切换到该子会话；
 *  · 子代理 → 子代理标签查看执行过程；工作流 → 工作流标签并定位到该运行；
 *  · 每项**一行**（行高固定，省略号截断），被截断时悬停显示完整内容（ui/Tooltip，不用原生 title）；
 *  · 折叠行（进程「已完成 N 项 / 待处理 N 项」、子代理 / 工作流「已完成 N 个」）悬停（或点击固定）
 *    弹出浮窗，列出被折叠的条目全文——浮窗挂到 body（ui/HoverCard），不被卡片的滚动体裁切。
 *
 * 视觉约定（项目规则）：
 *  · 不使用左侧装饰性竖线表达层级；层次靠间距 / 字重 / 底色；
 *  · 行首图标统一放在 `.tcard-ic` 固定槽位（16px），状态切换（转圈 → 勾）不改变文字起点。
 */
import { createContext, memo, useContext, useLayoutEffect, useRef, useState, type ReactNode, type RefObject } from "react";
import { IconChevronLeft, IconCpu, IconListChecks } from "../../icons";
import { IxWorkflow } from "../../iconkit/StackIcons";
import { HoverCard, Tooltip } from "../../ui";
import { useChatStore } from "../../../store/chat";
import { usePanelStore } from "../../../store/panel";
import { useWorkflowStore } from "../../../store/workflow";
import type { ProgressRow } from "../taskProgress";
import { foldLabel, type FoldKind } from "./processFold";
import {
  openPlanDoc, revealPanelTab,
  type PlanSectionData, type ProcessSectionData, type SubagentSectionData,
  type WorkflowItem, type WorkflowSectionData,
} from "./useToolCardData";

/** 卡片外壳（浮窗以它的左缘为基准弹出，而不是以触发行的左缘）。由 SessionToolCard 提供。 */
export const ToolCardEdgeContext = createContext<RefObject<HTMLElement | null> | undefined>(undefined);

/** 文本是否被省略（单行 ellipsis）；尺寸变化时重测。 */
export function useOverflow(ref: RefObject<HTMLElement | null>, text: string): boolean {
  const [over, setOver] = useState(false);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const check = () => setOver(el.scrollWidth > el.clientWidth + 1);
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
  strong?: boolean;
  className?: string;
  /** 行尾附加（计数 / 状态码 / 子会话标记） */
  trailing?: ReactNode;
  /** 固定提示；缺省时仅在文本被省略时提示全文 */
  tip?: string;
  onClick?: () => void;
}

/** 卡片里的一行：可点击时是按钮（可聚焦），省略时悬停 / 聚焦提示全文。 */
export const Row = memo(function Row({ icon, text, strong, className = "", trailing, tip, onClick }: RowProps) {
  const textRef = useRef<HTMLSpanElement>(null);
  const over = useOverflow(textRef, text);
  const tipText = tip ?? (over ? text : null);
  const textCls = "tcard-row-text" + (strong ? " tcard-row-strong" : "");
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

/** 步骤状态图标（进程栏）：四态共用同一环径（24 网格 r=8、线宽 2、16px 渲染），切换时不跳大小。
 *  完成 = 绿色圆环 + 对勾；进行中 = 强调色转圈；待办 = 灰色空心圆；中断 = 警示色圆环 + 暂停竖线。
 *  描边而非实心：整列步骤里大量「已完成」时，实心绿圆会糊成一片抢走正在做的那一项。 */
export const StepIcon = memo(function StepIcon({ status }: { status: ProgressRow["status"] }) {
  return (
    <svg
      className={`tcard-step is-${status}`}
      width={16}
      height={16}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {status === "running" ? (
        <>
          <circle cx="12" cy="12" r="8" strokeOpacity={0.25} />
          <path className="tcard-step-arc" d="M12 4a8 8 0 0 1 8 8" />
        </>
      ) : (
        <circle cx="12" cy="12" r="8" />
      )}
      {status === "done" && <path d="M8 12l3 3 5-6" />}
      {status === "interrupted" && <path d="M10 9v6M14 9v6" />}
    </svg>
  );
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

/** 折叠行：一行「‹ 已完成 4 项」，悬停 / 点击弹出被折叠条目的全文列表。 */
const FoldRow = memo(function FoldRow({ label, children, className = "" }: {
  label: string;
  children: ReactNode;
  className?: string;
}) {
  const edgeRef = useContext(ToolCardEdgeContext);
  return (
    <HoverCard
      side="left"
      align="start"
      edgeRef={edgeRef}
      aria-label={label}
      trigger={(
        <button type="button" className={`tcard-row tcard-row-btn tcard-row-fold ${className}`}>
          <span className="tcard-ic"><IconChevronLeft size={14} /></span>
          <span className="tcard-row-text">{label}</span>
        </button>
      )}
    >
      <div className="tcard-fold-pop">
        <div className="tcard-fold-head">{label}</div>
        <div className="tcard-fold-list">{children}</div>
      </div>
    </HoverCard>
  );
});

/** 折叠浮窗里的一项：完整文本（可换行），可点击时是按钮。 */
function FoldItem({ icon, text, trailing, className = "", onClick }: {
  icon: ReactNode;
  text: string;
  trailing?: ReactNode;
  className?: string;
  onClick?: () => void;
}) {
  const inner = (
    <>
      <span className="tcard-ic">{icon}</span>
      <span className="tcard-fold-text">{text}</span>
      {trailing}
    </>
  );
  return onClick
    ? <button type="button" className={`tcard-fold-item is-btn ${className}`} onClick={onClick}>{inner}</button>
    : <div className={`tcard-fold-item ${className}`}>{inner}</div>;
}

export const PlanSection = memo(function PlanSection({ data }: { data: PlanSectionData }) {
  return (
    <Row
      icon={<IconListChecks size={16} />}
      text={data.title}
      strong
      tip={`${data.title} · 点击查看计划文档`}
      trailing={data.awaiting ? <span className="tcard-row-hint">待确认</span> : undefined}
      onClick={() => openPlanDoc(data.planDocPath)}
    />
  );
});

export const ProcessSection = memo(function ProcessSection({ data }: { data: ProcessSectionData }) {
  return (
    <div className="tcard-proc-list">
      {data.entries.map((entry) => {
        if (entry.kind === "row") {
          const row = entry.row;
          return (
            <Row
              key={row.key}
              className={`tcard-proc is-${row.status}`}
              icon={<StepIcon status={row.status} />}
              text={row.title}
              trailing={row.owner != null ? <OwnerChip id={row.owner} /> : undefined}
            />
          );
        }
        // 折叠行的 key 按「种类」固定：被折叠的项变了，行不会重挂载（悬停中的浮窗不会闪）
        return (
          <FoldRow key={`fold-${entry.fold}`} label={foldLabel(entry.fold as FoldKind, entry.rows.length)}>
            {entry.rows.map((row) => (
              <FoldItem
                key={row.key}
                className={`tcard-proc is-${row.status}`}
                icon={<StepIcon status={row.status} />}
                text={row.title}
              />
            ))}
          </FoldRow>
        );
      })}
    </div>
  );
});

export const SubagentSection = memo(function SubagentSection({ data }: { data: SubagentSectionData }) {
  const openSubagent = usePanelStore((s) => s.openSubagent);
  return (
    <>
      {data.running.map((s) => (
        <Row
          key={`run-${s.id}`}
          className="tcard-proc is-running"
          icon={<StepIcon status="running" />}
          text={s.name}
          tip={`${s.name} · 点击查看执行过程`}
          onClick={() => openSubagent(s.id, s.name)}
        />
      ))}
      {/* 已完成的全部折成一行；运行中的不折叠（用户口径） */}
      {data.finished.length > 0 && (
        <FoldRow label={`已完成 ${data.finished.length} 个`}>
          {data.finished.map((s) => (
            <FoldItem
              key={`done-${s.id}`}
              className="tcard-proc is-done"
              icon={<StepIcon status="done" />}
              text={s.name}
              onClick={() => openSubagent(s.id, s.name)}
            />
          ))}
        </FoldRow>
      )}
    </>
  );
});

/** 工作流运行的右侧进度：`3/8`；没有节点时不显示。 */
function WorkflowProgress({ item }: { item: WorkflowItem }) {
  return item.total > 0 ? <span className="tcard-row-hint">{item.done}/{item.total}</span> : null;
}

/** 定位到某个运行再打开工作流标签：面板挂载时直接呈现这一份详情。 */
function openWorkflowRun(id: number): void {
  useWorkflowStore.getState().focus(id);
  revealPanelTab("workflow");
}

export const WorkflowSection = memo(function WorkflowSection({ data }: { data: WorkflowSectionData }) {
  return (
    <>
      {data.open.map((w) => (
        <Row
          key={`open-${w.id}`}
          className="tcard-proc is-running"
          icon={<IxWorkflow size={16} />}
          text={w.title}
          tip={`${w.title} · 点击查看工作流详情`}
          trailing={<WorkflowProgress item={w} />}
          onClick={() => openWorkflowRun(w.id)}
        />
      ))}
      {data.finished.length > 0 && (
        <FoldRow label={`已完成 ${data.finished.length} 个`}>
          {data.finished.map((w) => (
            <FoldItem
              key={`done-${w.id}`}
              className={`tcard-proc is-${w.status === "succeeded" ? "done" : "interrupted"}`}
              icon={<StepIcon status={w.status === "succeeded" ? "done" : "interrupted"} />}
              text={w.title}
              trailing={<WorkflowProgress item={w} />}
              onClick={() => openWorkflowRun(w.id)}
            />
          ))}
        </FoldRow>
      )}
    </>
  );
});
