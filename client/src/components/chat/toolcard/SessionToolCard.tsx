/** 会话右上角综合工具卡（plan-156-619 S7c）。
 *
 * 【定位】
 * 绝对定位覆盖在 `.chat-panel` 右上角，**不占列宽**、不参与 flex 布局——
 * 这是「卡片出现不改变消息列宽度」的结构前提（宽度冻结由 useContentColumnGeometry 承担）。
 *
 * 【三形态】
 * · expanded：完整卡片，五栏目可分别折叠；
 * · capsule ：单行胶囊，只显示优先级最高的一项（进程 > 工作流 > 子代理 > 计划 > git），
 *             尾部 `+N` 表示还有几个栏目有内容；
 * · hidden  ：五项全无、或宿主过窄时不渲染。
 *
 * 【空态】
 * 五项全无 → 整卡不存在（不是显示一张空卡）。这与旧的输入框胶囊口径一致。
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { IconChevronDown, IconMoreHorizontal } from "../../icons";
import { IxWorkflow } from "../../iconkit/StackIcons";
import { IconCpu, IconGitBranch, IconRingProgress } from "../../icons";
import { Popover, Tooltip } from "../../ui";
import { api } from "../../../api/client";
import { useChatStore } from "../../../store/chat";
import {
  CAPSULE_PRIORITY, SECTION_LABEL, TOOL_CARD_SECTIONS, useToolCardStore,
  type ToolCardSection,
} from "../../../store/toolCard";
import { CARD_RESERVE_EXPANDED, CARD_RESERVE_NONE } from "../contentColumnGeometry";
import {
  pickForm, useToolCardFit, type ToolCardForm,
} from "./useToolCardFit";
import { useToolCardData, type ToolCardData } from "./useToolCardData";
import {
  GitSection, PlanSection, ProcessSection, SubagentSection, WorkflowSection,
} from "./ToolCardSections";

/** 某栏目是否有内容（决定是否渲染 + 胶囊优先级）。 */
function hasContent(data: ToolCardData, section: ToolCardSection): boolean {
  const d = data[section];
  if (!d) return false;
  if (section === "git") return !data.git!.clean;
  return true;
}

/** 胶囊摘要：一行字的「图标 + 主信息」，随栏目变化。 */
function CapsuleSummary({ section, data }: { section: ToolCardSection; data: ToolCardData }) {
  if (section === "process" && data.process) {
    const { done, total } = data.process;
    return (
      <>
        <IconRingProgress value={total > 0 ? done / total : 0} size={14} strokeWidth={1.4}
          indicatorColor="var(--accent)" trackColor="color-mix(in srgb, var(--text-1) 14%, transparent)" />
        <span className="tcard-cap-num">{done}/{total}</span>
        {data.process.currentTitle && <span className="tcard-cap-text">{data.process.currentTitle}</span>}
      </>
    );
  }
  if (section === "workflow" && data.workflow) {
    return (
      <>
        <IxWorkflow size={13} />
        <span className="tcard-cap-num">{data.workflow.done}/{data.workflow.total}</span>
        <span className="tcard-cap-text">{data.workflow.title}</span>
      </>
    );
  }
  if (section === "subagent" && data.subagent) {
    return (
      <>
        <IconCpu size={13} />
        <span className="tcard-cap-num">{data.subagent.running.length || data.subagent.finished.length}</span>
        <span className="tcard-cap-text">{data.subagent.running.length > 0 ? "子代理执行中" : "子代理已完成"}</span>
      </>
    );
  }
  if (section === "plan" && data.plan) {
    return (
      <>
        <span className="tcard-cap-num">{data.plan.awaiting ? "待执行" : "计划"}</span>
        <span className="tcard-cap-text">{data.plan.task}</span>
      </>
    );
  }
  if (section === "git" && data.git) {
    return (
      <>
        <IconGitBranch size={13} />
        <span className="tcard-cap-num">{data.git.fileCount}</span>
        <span className="tcard-cap-text">{data.git.branch || "未跟踪变更"}</span>
      </>
    );
  }
  return null;
}

export interface SessionToolCardProps {
  /** 宿主要素（`.chat-panel`）：形态判定按它测宽。 */
  hostRef: React.RefObject<HTMLElement | null>;
  /** 内容列左移缺口（>0 表示让不出卡片占位，需要降级为胶囊）。 */
  shortfall?: number;
  /** 形态/占位变化回调：父层据此写 `--card-reserve`（宽度冻结，只改位置）。 */
  onFormChange?: (form: ToolCardForm) => void;
}

export function SessionToolCard({ hostRef, shortfall = 0, onFormChange }: SessionToolCardProps) {
  const sessionId = useChatStore((s) => s.currentSessionId);
  const data = useToolCardData(sessionId);

  const expandedBySession = useToolCardStore((s) => s.expandedBySession);
  const setExpanded = useToolCardStore((s) => s.setExpanded);
  const toggleSection = useToolCardStore((s) => s.toggleSection);
  const sections = useToolCardStore((s) => s.sectionsOf)(sessionId);
  const globalExpanded = useToolCardStore((s) => s.globalSectionsExpanded);
  const setGlobalDefault = useToolCardStore((s) => s.setGlobalDefault);

  const hostW = useToolCardFit(hostRef, shortfall);
  const wantExpanded = sessionId != null && expandedBySession[sessionId] === true;

  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef<HTMLButtonElement>(null);

  /** 有内容的栏目（按固定展示顺序）。 */
  const present = useMemo(
    () => TOOL_CARD_SECTIONS.filter((s) => hasContent(data, s)),
    [data],
  );

  const form = useMemo<ToolCardForm>(
    () => (present.length === 0 ? "hidden" : pickForm(hostW, shortfall, wantExpanded)),
    [present.length, hostW, shortfall, wantExpanded],
  );

  // 形态变化通知父层写 --card-reserve：宽度冻结，只有位置随占位变化
  useEffect(() => {
    onFormChange?.(form);
  }, [form, onFormChange]);

  /** 首次挂载从后端读一次全局默认策略（只作初始值，不覆盖会话本地记录）。 */
  useEffect(() => {
    void (async () => {
      try {
        const g = await api.getGlobalSettings();
        if (typeof g.tool_card_sections_expanded === "boolean") {
          setGlobalDefault(g.tool_card_sections_expanded);
        }
      } catch { /* 读不到就用默认展开 */ }
    })();
  }, [setGlobalDefault]);

  if (form === "hidden" || sessionId == null) return null;

  /** 胶囊：优先级最高的有内容栏目 + 其余数量。 */
  if (form === "capsule") {
    const top = CAPSULE_PRIORITY.find((s) => present.includes(s)) ?? present[0];
    const rest = present.length - 1;
    return (
      <Tooltip content="展开会话工具" side="left">
        <button
          type="button"
          className="tcard-capsule"
          onClick={() => setExpanded(sessionId, true)}
          aria-label="展开会话工具"
        >
          <CapsuleSummary section={top} data={data} />
          {rest > 0 && (
            <span className="tcard-cap-more" title={present.map((s) => SECTION_LABEL[s]).join(" / ")}>+{rest}</span>
          )}
        </button>
      </Tooltip>
    );
  }

  // expanded：完整卡片
  return (
    <div className="tcard-card" role="region" aria-label="会话工具">
      <div className="tcard-card-head">
        <span className="tcard-card-title">会话工具</span>
        <span className="tcard-card-head-ops">
          <Tooltip content="收起为胶囊" side="bottom">
            <button
              type="button"
              className="tcard-card-op"
              aria-label="收起为胶囊"
              onClick={() => setExpanded(sessionId, false)}
            >
              <IconChevronDown size={13} />
            </button>
          </Tooltip>
          <Tooltip content="折叠策略" side="bottom">
            <button
              type="button"
              ref={menuRef}
              className="tcard-card-op"
              aria-label="折叠策略"
              onClick={() => setMenuOpen((v) => !v)}
            >
              <IconMoreHorizontal size={14} />
            </button>
          </Tooltip>
          <Popover
            open={menuOpen}
            onClose={() => setMenuOpen(false)}
            placement="bottom-end"
            className="tcard-menu"
            role="menu"
            aria-label="折叠策略"
            ignoreRefs={[menuRef]}
          >
            <div
              className={"tcard-menu-item" + (globalExpanded ? " is-on" : "")}
              role="menuitemradio"
              aria-checked={globalExpanded}
              onClick={async () => {
                setMenuOpen(false);
                setGlobalDefault(true);
                try { await api.setGlobalSettings({ tool_card_sections_expanded: true }); } catch { /* 落库失败仅影响下次启动 */ }
              }}
            >
              栏目默认全展开
            </div>
            <div
              className={"tcard-menu-item" + (!globalExpanded ? " is-on" : "")}
              role="menuitemradio"
              aria-checked={!globalExpanded}
              onClick={async () => {
                setMenuOpen(false);
                setGlobalDefault(false);
                try { await api.setGlobalSettings({ tool_card_sections_expanded: false }); } catch { /* 同上 */ }
              }}
            >
              栏目默认折叠
            </div>
          </Popover>
        </span>
      </div>

      <div className="tcard-card-body">
        {present.map((section) => {
          const open = sections[section];
          return (
            <section className="tcard-sec" key={section}>
              <button
                type="button"
                className="tcard-sec-head"
                aria-expanded={open}
                onClick={(e) => { e.stopPropagation(); toggleSection(sessionId, section); }}
              >
                <span className="tcard-sec-label">{SECTION_LABEL[section]}</span>
                <span className="tcard-sec-meta">{sectionMeta(section, data)}</span>
                <IconChevronDown size={12} className={"tcard-sec-caret" + (open ? " is-open" : "")} />
              </button>
              {open && (
                <div className="tcard-sec-body">
                  {section === "git" && data.git && <GitSection data={data.git} />}
                  {section === "plan" && data.plan && <PlanSection data={data.plan} />}
                  {section === "process" && data.process && <ProcessSection data={data.process} />}
                  {section === "subagent" && data.subagent && <SubagentSection data={data.subagent} />}
                  {section === "workflow" && data.workflow && <WorkflowSection data={data.workflow} />}
                </div>
              )}
            </section>
          );
        })}
      </div>
    </div>
  );
}

/** 栏目头右侧的紧凑计数（折叠态也能看出概况）。 */
function sectionMeta(section: ToolCardSection, data: ToolCardData): string {
  if (section === "git") return data.git ? `${data.git.fileCount}` : "";
  if (section === "plan") return data.plan?.awaiting ? "待执行" : "执行中";
  if (section === "process") return data.process ? `${data.process.done}/${data.process.total}` : "";
  if (section === "subagent") {
    if (!data.subagent) return "";
    if (data.subagent.running.length > 0) return `${data.subagent.running.length} 运行中`;
    return `${data.subagent.finished.length} 已完成`;
  }
  if (section === "workflow") return data.workflow ? `${data.workflow.done}/${data.workflow.total}` : "";
  return "";
}

/** 卡片占位宽（展开态）：供父层写 `--card-reserve`。 */
export const TOOL_CARD_RESERVE = CARD_RESERVE_EXPANDED;
export { CARD_RESERVE_NONE };
