/** 会话右上角综合工具卡（plan-156-619 S7；plan-156-620 重做）。
 *
 * 【定位】
 * 绝对定位覆盖在 `.chat-panel` 右上角，**不占列宽**、不参与 flex 布局；
 * 内容列只在右缘真的会被卡片遮住时才按需左移（宽度由宿主与偏好决定，见 useContentColumnGeometry）。
 *
 * 【三形态】
 * · expanded：完整卡片，栏目可分别折叠；
 * · capsule ：单行胶囊，只显示优先级最高的一项（进程 > 工作流 > 子代理 > 计划 > git），
 *             尾部 `+N` 表示还有几个栏目有内容；
 * · hidden  ：五项全无，或连胶囊都会遮挡会话内容（小窗）。
 *
 * 【plan-156-620 结构调整】
 * · 去掉「会话工具」顶栏——第一个栏目的标题行右侧直接放「⋯」与「收起」（参考动态组卡片）；
 * · 操作区放在滚动容器之外：旧实现的折叠策略浮层被卡片 overflow 裁掉，点了「没反应」；
 * · 字号 / 图标槽位跟随设置（--fs-md / --flow-icon），与消息流一致。
 */
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { IconCheck, IconChevronDown, IconCpu, IconGitBranch, IconListChecks, IconMinimize, IconMoreHorizontal, IconRingProgress } from "../../icons";
import { IxWorkflow } from "../../iconkit/StackIcons";
import { IconButton, Popover, Tooltip } from "../../ui";
import { api } from "../../../api/client";
import { useChatStore } from "../../../store/chat";
import {
  CAPSULE_PRIORITY, SECTION_LABEL, TOOL_CARD_SECTIONS, useSectionsOf, useToolCardStore,
  type ToolCardSection,
} from "../../../store/toolCard";
import { pickForm, type ToolCardFit, type ToolCardForm } from "./useToolCardFit";
import { useToolCardData, type ToolCardData } from "./useToolCardData";
import {
  GitSection, PlanSection, ProcessSection, SubagentSection, WorkflowSection,
} from "./ToolCardSections";

/** 某栏目是否有内容（决定是否渲染 + 胶囊优先级）。 */
function hasContent(data: ToolCardData, section: ToolCardSection): boolean {
  return data[section] != null;
}

/** 胶囊摘要：一行字的「图标 + 主信息」，随栏目变化。 */
function CapsuleSummary({ section, data }: { section: ToolCardSection; data: ToolCardData }) {
  if (section === "process" && data.process) {
    const { done, total } = data.process;
    return (
      <>
        <span className="tcard-ic">
          <IconRingProgress value={total > 0 ? done / total : 0} size={15} strokeWidth={1.6}
            indicatorColor="var(--accent)" trackColor="color-mix(in srgb, var(--text-1) 14%, transparent)" />
        </span>
        <span className="tcard-cap-num">{done}/{total}</span>
        {data.process.currentTitle && <span className="tcard-cap-text">{data.process.currentTitle}</span>}
      </>
    );
  }
  if (section === "workflow" && data.workflow) {
    return (
      <>
        <span className="tcard-ic"><IxWorkflow size={15} /></span>
        <span className="tcard-cap-num">{data.workflow.done}/{data.workflow.total}</span>
        <span className="tcard-cap-text">{data.workflow.title}</span>
      </>
    );
  }
  if (section === "subagent" && data.subagent) {
    const running = data.subagent.running.length;
    return (
      <>
        <span className="tcard-ic"><IconCpu size={15} /></span>
        <span className="tcard-cap-num">{running || data.subagent.finished.length}</span>
        <span className="tcard-cap-text">{running > 0 ? "子代理执行中" : "子代理已完成"}</span>
      </>
    );
  }
  if (section === "plan" && data.plan) {
    return (
      <>
        <span className="tcard-ic"><IconListChecks size={15} /></span>
        <span className="tcard-cap-text">{data.plan.title}</span>
      </>
    );
  }
  if (section === "git" && data.git) {
    return (
      <>
        <span className="tcard-ic"><IconGitBranch size={15} /></span>
        <span className="tcard-cap-num">{data.git.fileCount}</span>
        <span className="tcard-cap-text">个文件未提交</span>
      </>
    );
  }
  return null;
}

/** 栏目头右侧的紧凑计数（折叠态也能看出概况）。 */
function sectionMeta(section: ToolCardSection, data: ToolCardData): string {
  if (section === "git") return data.git ? `${data.git.fileCount}` : "";
  if (section === "plan") return data.plan?.awaiting ? "待确认" : "";
  if (section === "process") return data.process ? `${data.process.done}/${data.process.total}` : "";
  if (section === "subagent") {
    if (!data.subagent) return "";
    if (data.subagent.running.length > 0) return `${data.subagent.running.length} 运行中`;
    return `${data.subagent.finished.length}`;
  }
  if (section === "workflow") return data.workflow ? `${data.workflow.done}/${data.workflow.total}` : "";
  return "";
}

export interface SessionToolCardProps {
  /** 几何缺口（来自 useContentColumnGeometry）：决定能否展开 / 能否显示胶囊。 */
  fit: ToolCardFit;
  /** 形态变化回调：父层据此告诉几何层需要让出多少右侧空间（宽度冻结，只改位置）。
   *  胶囊形态附带实测宽度——折叠后胶囊窄于上限时，右侧够用就不左移内容列。 */
  onFormChange?: (form: ToolCardForm, capsuleW?: number) => void;
}

export function SessionToolCard({ fit, onFormChange }: SessionToolCardProps) {
  const sessionId = useChatStore((s) => s.currentSessionId);
  const data = useToolCardData(sessionId);

  const wantExpanded = useToolCardStore((s) => sessionId != null && s.expandedBySession[sessionId] === true);
  const setExpanded = useToolCardStore((s) => s.setExpanded);
  const toggleSection = useToolCardStore((s) => s.toggleSection);
  const sections = useSectionsOf(sessionId);
  const globalExpanded = useToolCardStore((s) => s.globalSectionsExpanded);
  const setGlobalDefault = useToolCardStore((s) => s.setGlobalDefault);
  const hydrateGlobalDefault = useToolCardStore((s) => s.hydrateGlobalDefault);
  const hydrated = useToolCardStore((s) => s.hydrated);
  const markHydrated = useToolCardStore((s) => s.markHydrated);

  const [menuOpen, setMenuOpen] = useState(false);
  const menuBtnRef = useRef<HTMLButtonElement>(null);
  // plan-156-621：菜单键盘导航——打开即聚焦首项，方向键在选项间轮转
  const menuItemsRef = useRef<Array<HTMLButtonElement | null>>([]);

  /** 有内容的栏目（按固定展示顺序）。 */
  const present = useMemo(
    () => TOOL_CARD_SECTIONS.filter((s) => hasContent(data, s)),
    [data],
  );

  const form = useMemo<ToolCardForm>(
    () => (sessionId == null ? "hidden" : pickForm(fit, wantExpanded, present.length > 0)),
    [sessionId, fit, wantExpanded, present.length],
  );

  // 形态变化通知父层：宽度冻结，只有位置随需求变化。
  // plan-156-621：用 useLayoutEffect——此前是 useEffect，卡片本体已按新形态绘制、
  // 内容列左移却晚一帧，展开瞬间会看到「卡片先压住正文再让位」的闪动。
  // plan-20261019：胶囊形态先量真实宽度再上报——折叠后胶囊是内容自适应宽度，
  // 按上限 260px 让位会让消息列白白左移；同一 layout 周期内量完，不会先移再回弹。
  const capsuleRef = useRef<HTMLButtonElement>(null);
  const capsuleWRef = useRef(0);

  useLayoutEffect(() => {
    if (form !== "capsule") {
      capsuleWRef.current = 0;
      onFormChange?.(form);
      return;
    }
    const el = capsuleRef.current;
    if (!el) return;
    const report = () => {
      const w = Math.ceil(el.offsetWidth);
      if (!(w > 0) || Math.abs(w - capsuleWRef.current) < 1) return;
      capsuleWRef.current = w;
      onFormChange?.("capsule", w);
    };
    report();
    // 计数 / 文案变化会改胶囊宽度：跟着上报，内容列只让出真正需要的那一段
    const ro = typeof ResizeObserver !== "undefined" ? new ResizeObserver(report) : null;
    ro?.observe(el);
    return () => ro?.disconnect();
  }, [form, onFormChange]);
  // 收起 / 隐藏时关掉浮层，避免它悬空残留
  useEffect(() => { if (form !== "expanded") setMenuOpen(false); }, [form]);
  // plan-156-621：菜单打开后把焦点移到首项（此前 Tab 要穿过触发按钮才能到达选项）
  useEffect(() => {
    if (menuOpen) menuItemsRef.current.find(Boolean)?.focus();
  }, [menuOpen]);

  /** plan-156-621：方向键在两项间轮转——此前 role=menuitemradio 只有语义、没有键盘行为。 */
  const onMenuKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    e.preventDefault();
    const items = menuItemsRef.current.filter((x): x is HTMLButtonElement => Boolean(x));
    if (items.length === 0) return;
    const idx = items.indexOf(document.activeElement as HTMLButtonElement);
    const next = e.key === "ArrowDown"
      ? (idx + 1) % items.length
      : (idx <= 0 ? items.length - 1 : idx - 1);
    items[next].focus();
  };

  /** 全局默认策略只从后端读一次（store 记忆），不随卡片挂载重复请求。 */
  useEffect(() => {
    if (hydrated) return;
    markHydrated();
    void api.getGlobalSettings()
      .then((g) => {
        if (typeof g.tool_card_sections_expanded === "boolean") hydrateGlobalDefault(g.tool_card_sections_expanded);
      })
      .catch(() => { /* 读不到就用默认展开 */ });
  }, [hydrated, markHydrated, hydrateGlobalDefault]);

  if (form === "hidden" || sessionId == null) return null;

  /** 胶囊：优先级最高的有内容栏目 + 其余数量。 */
  if (form === "capsule") {
    const top = CAPSULE_PRIORITY.find((s) => present.includes(s)) ?? present[0];
    const rest = present.length - 1;
    const canExpand = fit.expandedShortfall <= 0;
    return (
      <Tooltip
        content={canExpand ? "展开会话工具" : "窗口较窄，放大窗口后可展开"}
        side="bottom"
      >
        <button
          ref={capsuleRef}
          type="button"
          className="tcard-capsule"
          onClick={() => { if (canExpand) setExpanded(sessionId, true); }}
          aria-label="展开会话工具"
          aria-disabled={!canExpand}
        >
          <CapsuleSummary section={top} data={data} />
          {rest > 0 && <span className="tcard-cap-more">+{rest}</span>}
        </button>
      </Tooltip>
    );
  }

  const applyGlobal = (expanded: boolean) => {
    setMenuOpen(false);
    setGlobalDefault(expanded);
    void api.setGlobalSettings({ tool_card_sections_expanded: expanded }).catch(() => {
      /* 落库失败仅影响下次启动 */
    });
  };

  // expanded：完整卡片
  return (
    <div className="tcard-card" role="region" aria-label="会话工具">
      {/* 浮层锚点：与「⋯」同位，但留在滚动容器之外——菜单挂在卡片体里会被 overflow 裁掉
          （plan-156-620 踩过的坑）。锚点本身不接收指针事件，只提供定位基准。 */}
      <div className="tcard-menu-anchor">
        <Popover
          open={menuOpen}
          onClose={() => setMenuOpen(false)}
          placement="bottom-end"
          className="tcard-menu"
          role="menu"
          aria-label="折叠策略"
          ignoreRefs={[menuBtnRef]}
          onKeyDown={onMenuKeyDown}
        >
          <div className="tcard-menu-caption">栏目默认状态</div>
          {([true, false] as const).map((value) => (
            <button
              type="button"
              key={String(value)}
              ref={(el) => { menuItemsRef.current[value ? 0 : 1] = el; }}
              className={"tcard-menu-item" + (globalExpanded === value ? " is-on" : "")}
              role="menuitemradio"
              aria-checked={globalExpanded === value}
              onClick={() => applyGlobal(value)}
            >
              <span className="tcard-menu-label">{value ? "全部展开" : "全部折叠"}</span>
              <span className="tcard-menu-check">{globalExpanded === value && <IconCheck size={13} />}</span>
            </button>
          ))}
        </Popover>
      </div>

      {/* 滚动时菜单的锚点已经移走，留一份悬浮菜单会错位——直接关掉 */}
      <div
        className="tcard-card-body"
        onScroll={menuOpen ? () => setMenuOpen(false) : undefined}
      >
        {/* 操作区：与第一个栏目标题行同高，随栏目一起滚动，滚出后被卡片体裁切。
            它按内容坐标贴在顶部，不依赖第一个栏目是哪一类（Git / 进程 / 计划都成立）。 */}
        <div className="tcard-card-ops">
          {/* 项目规则：图标按钮统一 ui/IconButton（自带 Tooltip），不新增私有按钮类 */}
          <IconButton
            ref={menuBtnRef}
            size="xs"
            icon={<IconMoreHorizontal size={15} />}
            title="折叠策略"
            active={menuOpen}
            aria-haspopup="menu"
            aria-expanded={menuOpen}
            onClick={() => setMenuOpen((v) => !v)}
          />
          <IconButton
            size="xs"
            icon={<IconMinimize size={14} />}
            title="收起"
            aria-label="收起为胶囊"
            onClick={() => setExpanded(sessionId, false)}
          />
        </div>
        {present.map((section, index) => {
          const open = sections[section];
          return (
            <section className={"tcard-sec" + (index === 0 ? " is-first" : "")} key={section}>
              <button
                type="button"
                className="tcard-sec-head"
                aria-expanded={open}
                onClick={() => toggleSection(sessionId, section)}
              >
                <span className="tcard-sec-label">{SECTION_LABEL[section]}</span>
                <span className="tcard-sec-meta">{sectionMeta(section, data)}</span>
                <IconChevronDown size={13} className={"tcard-sec-caret" + (open ? " is-open" : "")} />
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
