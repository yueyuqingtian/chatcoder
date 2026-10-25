/** 工具卡栏目的展示体（plan-156-619 S7c；plan-20261019-toolcard-v2 重做）。
 *
 * 分工：本文件只有「怎么显示 / 点了去哪」，取数与派生在 `useToolCardData.ts`，
 * 容器与折叠在 `SessionToolCard.tsx`，Git 栏自成一体见 `GitToolSection.tsx`。
 *
 * 交互：
 *  · 计划 → 文件标签预览计划文档；进程步骤由子代理执行时，行尾「子代理」标记打开其执行过程
 *    （升级前的历史步骤 label 为「历史子会话」，点击切到那个子会话）；
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
import { IconBot, IconChevronLeft, IconCpu, IconListChecks, IconStop, IconTerminal } from "../../icons";
import { IxWorkflow } from "../../iconkit/StackIcons";
import { HoverCard, IconButton, Tooltip } from "../../ui";
import { useChatStore } from "../../../store/chat";
import { usePanelStore } from "../../../store/panel";
import { useWorkflowStore } from "../../../store/workflow";
import { bgTaskElapsedSec, useBgTaskStore, type BgTask } from "../../../store/bgTasks";
import { bgCommandLine, formatBgElapsed } from "../../../utils/bgFormat";
import type { ProgressRow } from "../taskProgress";
import { subscribeTick } from "../useRunningTicker";
import { foldLabel, type FoldKind } from "./processFold";
import {
  openPlanDoc, revealPanelTab,
  type BgTaskSectionData, type PlanSectionData, type ProcessSectionData, type SubagentSectionData,
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

/** 步骤状态图标（进程 / 子代理栏）：各态共用同一环径（24 网格 r=8、线宽 2、16px 渲染），切换时不跳大小。
 *  完成 = 绿色圆环 + 对勾；进行中 = 强调色转圈；待办 = 灰色空心圆；中断 = 警示色圆环 + 暂停竖线；
 *  失败（仅子代理）= 错误色圆环 + 叉。
 *  描边而非实心：整列步骤里大量「已完成」时，实心绿圆会糊成一片抢走正在做的那一项。 */
export const StepIcon = memo(function StepIcon({ status }: { status: ProgressRow["status"] | "failed" }) {
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
      {status === "failed" && <path d="M9 9l6 6M15 9l-6 6" />}
    </svg>
  );
});

/** 子代理结果 → 步骤图标态（已停止与进程栏的「中断」同形） */
const SUBAGENT_STEP: Record<"done" | "failed" | "stopped", "done" | "failed" | "interrupted"> = {
  done: "done", failed: "failed", stopped: "interrupted",
};

/** 历史子会话标题（进程行标记的提示）；查不到退编号，不猜名字。
 *  plan-562-2318：同时回报是否存在——此前查不到也返回编号，调用方无从区分
 *  「有历史子会话」与「只是个孤立的 owner id」，于是渲染成可点击、点了跳到不存在的会话。 */
function useSubsessionTitle(id: number): { title: string; exists: boolean } {
  // 两个原子 selector（返回布尔 / 字符串）而不是返回对象，避免每次渲染都是新引用
  const exists = useChatStore((s) => {
    for (const list of Object.values(s.subsessionsByParent)) {
      if (list.some((c) => c.id === id)) return true;
    }
    return false;
  });
  const title = useChatStore((s) => {
    for (const list of Object.values(s.subsessionsByParent)) {
      const hit = list.find((c) => c.id === id);
      if (hit?.title) return hit.title;
    }
    return "";
  });
  return { title: title || `历史子会话 #${id}`, exists };
}

/** 进程行尾的执行者标记（plan-549-2281）。
 *
 * 并行执行已统一到子代理，所以新数据的 owner 是子代理 id——点击打开右面板的子代理执行过程；
 * 升级前落库的步骤 owner 存的是子会话 id（ownerKind="subsession"），历史数据仍要能用：
 * 点击切到那个历史子会话，不因为入口下线就看不了。
 * 两者都不确定时（既无子代理元信息也无子会话行）只当作普通子代理角标，不做点击——
 * 跳到一个不存在的目标比不可点更让人困感。
 * plan-562-2318：注释与实现此前相反（旧代码在都不确定时仍渲染可点按钮去 switchSession）；
 * 现在只有确认存在的历史子会话才可点击，否则一律渲染为不可点的子代理角标。 */
const OwnerChip = memo(function OwnerChip({ id, kind }: { id: number; kind?: "subagent" | "subsession" | null }) {
  const agentName = useChatStore((s) => s.subagentMeta[id]?.name ?? "");
  const agentStatus = useChatStore((s) => String(s.subagentMeta[id]?.status ?? ""));
  const legacy = useSubsessionTitle(id);
  const switchSession = useChatStore((s) => s.switchSession);
  const openSubagent = usePanelStore((s) => s.openSubagent);

  // plan-20261025-shell（需求 4）：执行者标记由「图标 + 子代理」文字胶囊改为一枚 16px 图标——
  // 进程栏每行只有一行高，文字胶囊会挤掉步骤标题；名称与操作说明放进悬停提示。
  // 图标色表达该子代理的状态：执行中 = 强调色，失败 / 取消 = 错误色，其余中性。
  // 类型未知时：先看当前会话有没有这个子代理，没有才当作历史子会话
  // plan-562-2318：历史子会话必须确实存在才按子会话渲染，否则退回子代理角标（不绑 onClick）
  const resolved: "subagent" | "subsession" = kind === "subsession" && legacy.exists ? "subsession" : "subagent";
  if (resolved === "subagent") {
    const tone = agentStatus === "running" || agentStatus === "in_progress" ? "run"
      : agentStatus === "failed" || agentStatus === "cancelled" ? "fail" : undefined;
    const tip = agentName ? `由子代理「${agentName}」执行，点击查看过程` : `由子代理 #${id} 执行`;
    // 没有子代理元信息时目标不明：同一枚图标，但渲染成不可点的静态角标
    if (!agentName) {
      return (
        <Tooltip content={tip} side="left">
          <span className="tcard-owner" data-tone={tone} aria-label={`子代理 #${id}`}>
            <IconBot size={16} />
          </span>
        </Tooltip>
      );
    }
    return (
      <Tooltip content={tip} side="left">
        <button
          type="button"
          className="tcard-owner"
          data-tone={tone}
          aria-label={`查看子代理「${agentName}」的执行过程`}
          onClick={(e) => { e.stopPropagation(); openSubagent(id, agentName); }}
        >
          <IconBot size={16} />
        </button>
      </Tooltip>
    );
  }
  return (
    <Tooltip content={`由历史子会话「${legacy.title}」执行，点击切换`} side="left">
      <button
        type="button"
        className="tcard-owner"
        aria-label={`切换到历史子会话「${legacy.title}」`}
        onClick={(e) => { e.stopPropagation(); void switchSession(id); }}
      >
        <IconCpu size={16} />
      </button>
    </Tooltip>
  );
});

/** 折叠行：一行「‹ 已完成 4 项」，悬停 / 点击弹出被折叠条目的全文列表。
 *  hint 只出现在浮窗头部（plan-486-2114）：折叠只折首尾连续段，区间提示让用户确认折的是哪几步；
 *  主行文案不带区间，避免把单行折叠行撑宽、挤掉步骤标题。
 */
const FoldRow = memo(function FoldRow({ label, children, className = "", hint }: {
  label: string;
  children: ReactNode;
  className?: string;
  hint?: string;
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
        <div className="tcard-fold-head">{label}{hint ? ` · ${hint}` : ""}</div>
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
              trailing={row.owner != null ? <OwnerChip id={row.owner} kind={row.ownerKind} /> : undefined}
            />
          );
        }
        // 折叠行的 key 按「种类」固定：被折叠的项变了，行不会重挂载（悬停中的浮窗不会闪）
        return (
          <FoldRow
            key={`fold-${entry.fold}`}
            label={foldLabel(entry.fold as FoldKind, entry.rows.length)}
            hint={`第 ${entry.range[0] + 1}–${entry.range[1] + 1} 步`}
          >
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
      {/* 尚未开始的折成一行（工作流排队节点可能很多），不混进「已完成」 */}
      {data.pending.length > 0 && (
        <FoldRow label={`待开始 ${data.pending.length} 个`}>
          {data.pending.map((s) => (
            <FoldItem
              key={`wait-${s.id}`}
              className="tcard-proc is-pending"
              icon={<StepIcon status="pending" />}
              text={s.name}
              onClick={() => openSubagent(s.id, s.name)}
            />
          ))}
        </FoldRow>
      )}
      {/* 已结束的全部折成一行；运行中的不折叠（用户口径）。
          逐项保留真实结果：有失败 / 已停止时折叠行写「已结束」，不把它们算进「已完成」（与右面板摘要一致） */}
      {data.finished.length > 0 && (
        <FoldRow label={`${data.finished.every((s) => s.state === "done") ? "已完成" : "已结束"} ${data.finished.length} 个`}>
          {data.finished.map((s) => {
            const step = SUBAGENT_STEP[s.state];
            return (
              <FoldItem
                key={`done-${s.id}`}
                className={`tcard-proc is-${step}`}
                icon={<StepIcon status={step} />}
                text={s.name}
                onClick={() => openSubagent(s.id, s.name)}
              />
            );
          })}
        </FoldRow>
      )}
    </>
  );
});

/** 已运行时间：文本直接写进 DOM，随共享秒级节拍更新——不触发 React 重渲染，也不为每行各开一个定时器。 */
const BgElapsed = memo(function BgElapsed({ task }: { task: BgTask }) {
  const ref = useRef<HTMLSpanElement>(null);
  useLayoutEffect(() => {
    const paint = (now: number) => {
      if (ref.current) ref.current.textContent = formatBgElapsed(bgTaskElapsedSec(task, now));
    };
    paint(Date.now());
    return subscribeTick(paint);
  }, [task]);
  return <span ref={ref} className="tcard-row-hint tcard-bg-elapsed" />;
});

/** 一个运行中的后台命令：行首终端图标 → 命令（首行，省略展示，悬停给全文）→ 运行时间 → 停止。 */
const BgTaskRow = memo(function BgTaskRow({ sessionId, task }: { sessionId: number; task: BgTask }) {
  const stop = useBgTaskStore((s) => s.stop);
  const line = bgCommandLine(task.command) || task.shellId;
  const full = task.command.trim();
  // 多行 / 很长的命令：固定给全文提示；单行放得下时由 Row 自己判断是否被省略
  const tip = full !== line ? full.slice(0, 800) : undefined;
  return (
    <Row
      className="tcard-bg"
      icon={<IconTerminal size={16} />}
      text={line}
      tip={tip}
      trailing={(
        <>
          <BgElapsed task={task} />
          <IconButton
            className="tcard-bg-stop"
            size="xs"
            tone="danger"
            icon={<IconStop size={16} />}
            title={task.stopping ? "正在停止…" : "停止，并通知会话"}
            aria-label="停止这个后台命令"
            disabled={task.stopping}
            onClick={() => { void stop(sessionId, task.shellId); }}
          />
        </>
      )}
    />
  );
});

export const BgTaskSection = memo(function BgTaskSection({ data }: { data: BgTaskSectionData }) {
  return (
    <>
      {data.tasks.map((t) => <BgTaskRow key={t.shellId} sessionId={data.sessionId} task={t} />)}
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
