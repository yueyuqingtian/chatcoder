/** 会话右上角综合工具卡（plan-156-619 S7；plan-156-620 重做；plan-20261019-toolcard-v2 再造）。
 *
 * 【定位】
 * 绝对定位覆盖在 `.chat-panel` 右上角，**不占列宽**、不参与 flex 布局。
 *  · 胶囊（收缩态）悬浮在右上角，**不让位**——消息列与输入框保持居中排版，胶囊只盖在右侧留白上；
 *  · 展开卡片体量大，才按需让位：内容列只在右缘真的会被遮住时左移（几何见 useContentColumnGeometry）。
 *
 * 【形态】
 * · expanded：完整卡片停靠在右上角，栏目可分别折叠；
 * · floating：放不下停靠卡片时（窄窗口 / 右面板展开），用户展开的卡片悬浮在内容之上、不让位
 *             （plan-20261025-shell 需求 4：此前会自动收回胶囊甚至隐藏，窄窗口里打不开）；
 * · capsule ：单行胶囊，只显示优先级最高的一项（进程 > 工作流 > 子代理 > 计划 > git），
 *             尾部 `+N` 表示还有几个栏目有内容；窄到完整胶囊会盖住正文时收成只剩图标的小圆钮；
 * · hidden  ：各栏目都没有内容（或几何尚未测量）。
 *
 * 【胶囊 ↔ 卡片的形变（plan-20261019-toolcard-v2）】
 * 两种形态共用同一个外壳 `.tcard-shell`：用户点胶囊 / 收起按钮时，外壳的宽、高、圆角在两种尺寸之间过渡
 * （CSS 关键帧，起止尺寸来自 FLIP 式实测），胶囊内容与卡片内容在壳内交叉淡入淡出，阴影与描边随外壳自然伸缩。
 * 此前是「旧形态原地淡出 + 新形态缩放入场」两块东西叠着换，观感是换了一个东西而不是同一张卡在变形。
 * 内容列的平移（展开需要让位时）同步过渡，由 ChatPanel 在这一小段时间内打开过渡开关。
 * 窗口缩放 / 拖面板造成的形态变化不做过渡（逐帧滞后的正文比瞬切更难看）。
 *
 * 【结构要点】
 * · 去掉「会话工具」顶栏——第一个栏目的标题行右侧直接放「⋯」与「收起」（参考动态组卡片）；
 * · 「⋯」设置浮层走 ui/Floating（portal）：卡片体是滚动容器，挂在体内的浮层会被裁掉；
 * · 字号 / 图标槽位跟随设置（--fs-md / --flow-icon），与消息流一致。
 */
import {
  useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type AnimationEvent, type CSSProperties,
} from "react";
import {
  IconChevronDown, IconCpu, IconGitBranch, IconListChecks, IconMinimize, IconMoreHorizontal, IconRingProgress,
  IconTerminal,
} from "../../icons";
import { IxWorkflow } from "../../iconkit/StackIcons";
import { Collapse, Floating, IconButton, Segmented, Switch, Tooltip } from "../../ui";
import { motionDisabled } from "../../ui/usePresence";
import { useChatStore } from "../../../store/chat";
import {
  CAPSULE_PRIORITY, SECTION_LABEL, TOOL_CARD_SECTIONS,
  hydrateToolCardPrefs, saveToolCardForm, saveToolCardSection,
  useSectionsOf, useToolCardStore, useWantExpanded,
  type ToolCardDefaultForm, type ToolCardSection,
} from "../../../store/toolCard";
import { isCardForm, pickForm, type ToolCardFit, type ToolCardForm } from "./useToolCardFit";
import { useToolCardData, type ToolCardData } from "./useToolCardData";
import { GitToolSection } from "./GitToolSection";
import {
  BgTaskSection, PlanSection, ProcessSection, SubagentSection, ToolCardEdgeContext, WorkflowSection,
} from "./ToolCardSections";

interface InertProps {
  /** React 18 的 DOM 类型未内置 inert，用字符串透传（离场中的内容不可聚焦、不可点击） */
  inert?: string;
}

/** 形变请求在多久内有效（ms）：形态没有真的变化（如放不下展开卡片）时，不让它影响之后无关的形态切换。 */
const MORPH_REQUEST_TTL = 500;
/** 卡片体限高（px）：面板高度减去上下留白（顶部 10 + 底部 14），上限 720、下限 180。 */
const CARD_MAX_H = 720;
const CARD_MIN_MAX_H = 180;
const CARD_V_GAP = 24;

/** 某栏目是否有内容（决定是否渲染 + 胶囊优先级）。 */
function hasContent(data: ToolCardData, section: ToolCardSection): boolean {
  return data[section] != null;
}

/** 胶囊上「还有几个栏目有内容」：Git 栏在没有任何改动 / 待推送时很安静，不计入（否则 git 项目里永远 +1）。 */
function otherCount(data: ToolCardData, present: ToolCardSection[], top: ToolCardSection): number {
  return present.filter((s) => {
    if (s === top) return false;
    if (s === "git" && data.git && data.git.files === 0 && data.git.repo.ahead === 0) return false;
    return true;
  }).length;
}

/** 胶囊摘要：一行字的「图标 + 主信息」，随栏目变化。
 *  compact（plan-20261025-shell）：窄到完整胶囊会盖住半个消息列时只留图标，收成一枚小圆钮。 */
function CapsuleSummary({ section, data, compact = false }: { section: ToolCardSection; data: ToolCardData; compact?: boolean }) {
  if (compact) {
    if (section === "process" && data.process) {
      const { done, total } = data.process;
      return (
        <span className="tcard-ic">
          <IconRingProgress value={total > 0 ? done / total : 0} size={16} strokeWidth={2}
            indicatorColor="var(--accent)" trackColor="color-mix(in srgb, var(--text-1) 14%, transparent)" />
        </span>
      );
    }
    const icon = section === "workflow" ? <IxWorkflow size={16} />
      : section === "subagent" ? <IconCpu size={16} />
      : section === "bgtask" ? <IconTerminal size={16} />
      : section === "plan" ? <IconListChecks size={16} />
      : <IconGitBranch size={16} />;
    return <span className="tcard-ic">{icon}</span>;
  }
  if (section === "process" && data.process) {
    const { done, total, currentTitle } = data.process;
    return (
      <>
        <span className="tcard-ic">
          <IconRingProgress value={total > 0 ? done / total : 0} size={16} strokeWidth={2}
            indicatorColor="var(--accent)" trackColor="color-mix(in srgb, var(--text-1) 14%, transparent)" />
        </span>
        <span className="tcard-cap-num">{done}/{total}</span>
        {currentTitle && <span className="tcard-cap-text">{currentTitle}</span>}
      </>
    );
  }
  if (section === "workflow" && data.workflow) {
    const first = data.workflow.open[0];
    return (
      <>
        <span className="tcard-ic"><IxWorkflow size={16} /></span>
        {first ? (
          <>
            {first.total > 0 && <span className="tcard-cap-num">{first.done}/{first.total}</span>}
            <span className="tcard-cap-text">{first.title}</span>
          </>
        ) : (
          <span className="tcard-cap-text">工作流已完成</span>
        )}
      </>
    );
  }
  if (section === "subagent" && data.subagent) {
    const { running, pending, finished } = data.subagent;
    // 失败 / 已停止的不算「已完成」（与右面板摘要的子代理状态一致）
    const text = running.length > 0 ? "子代理执行中"
      : pending.length > 0 ? "子代理待开始"
      : finished.every((s) => s.state === "done") ? "子代理已完成" : "子代理已结束";
    return (
      <>
        <span className="tcard-ic"><IconCpu size={16} /></span>
        <span className="tcard-cap-num">{running.length || pending.length || finished.length}</span>
        <span className="tcard-cap-text">{text}</span>
      </>
    );
  }
  if (section === "bgtask" && data.bgtask) {
    return (
      <>
        <span className="tcard-ic"><IconTerminal size={16} /></span>
        <span className="tcard-cap-num">{data.bgtask.tasks.length}</span>
        <span className="tcard-cap-text">后台任务运行中</span>
      </>
    );
  }
  if (section === "plan" && data.plan) {
    return (
      <>
        <span className="tcard-ic"><IconListChecks size={16} /></span>
        <span className="tcard-cap-text">{data.plan.title}</span>
      </>
    );
  }
  if (section === "git" && data.git) {
    const { repo, additions, deletions, files, hasLineStats } = data.git;
    const branch = repo.detached ? `游离 ${repo.oid ?? ""}`.trim() : (repo.branch ?? "—");
    return (
      <>
        <span className="tcard-ic"><IconGitBranch size={16} /></span>
        <span className="tcard-cap-text">{branch}</span>
        {files === 0 ? (
          <span className="tcard-cap-num">无更改</span>
        ) : hasLineStats ? (
          <span className="tcard-diffstat"><span className="is-add">+{additions}</span><span className="is-del">-{deletions}</span></span>
        ) : (
          <span className="tcard-cap-num">{files} 个文件</span>
        )}
      </>
    );
  }
  return null;
}

/** 栏目头计数的语气（plan-20261025-shell）：进行中用强调色、全部完成用成功色，其余中性——
 *  对齐参考卡片的「进程 7/7」：一眼看出这一栏是在跑还是已收尾，不必展开。
 *  plan-20261025-progress-keep：已停止 / 待续接 / 已结束仍有未完成项的清单保持中性，不冒充「在跑」；
 *  子代理有失败或已停止的不标成功色（与右面板摘要一致）。 */
function sectionTone(section: ToolCardSection, data: ToolCardData): "run" | "done" | undefined {
  if (section === "process" && data.process) {
    if (data.process.done >= data.process.total) return "done";
    return data.process.phase === "running" || data.process.hasRunning ? "run" : undefined;
  }
  if (section === "subagent" && data.subagent) {
    if (data.subagent.running.length > 0) return "run";
    if (data.subagent.pending.length > 0) return undefined;
    return data.subagent.finished.every((s) => s.state === "done") ? "done" : undefined;
  }
  if (section === "workflow" && data.workflow) return data.workflow.open.length > 0 ? "run" : "done";
  if (section === "bgtask" && data.bgtask) return "run";
  return undefined;
}

/** 栏目头右侧的紧凑计数（折叠态也能看出概况）。 */
function sectionMeta(section: ToolCardSection, data: ToolCardData): string {
  if (section === "plan") return data.plan?.awaiting ? "待确认" : "";
  if (section === "process") {
    if (!data.process) return "";
    const count = `${data.process.done}/${data.process.total}`;
    // 状态词与右面板摘要的总览行一致（待续接 / 已停止），两处说法不再各说各的
    if (data.process.carried) return `待续接 ${count}`;
    if (data.process.phase === "interrupted") return `已停止 ${count}`;
    return count;
  }
  if (section === "subagent") {
    if (!data.subagent) return "";
    if (data.subagent.running.length > 0) return `${data.subagent.running.length} 运行中`;
    if (data.subagent.pending.length > 0) return `${data.subagent.pending.length} 待开始`;
    return `${data.subagent.finished.length}`;
  }
  if (section === "workflow") {
    if (!data.workflow) return "";
    if (data.workflow.open.length > 0) return `${data.workflow.open.length} 运行中`;
    return `${data.workflow.finished.length}`;
  }
  if (section === "bgtask") return data.bgtask ? `${data.bgtask.tasks.length} 运行中` : "";
  return "";
}

/** CSS 时间串（"0.24s" / "240ms"）→ 毫秒；读不到返回 0。 */
function parseCssTime(raw: string): number {
  const first = raw.split(",")[0]?.trim() ?? "";
  const n = Number.parseFloat(first);
  if (!Number.isFinite(n)) return 0;
  return first.endsWith("ms") ? n : n * 1000;
}

interface Morph {
  dir: "expanding" | "collapsing";
  fromW: number;
  fromH: number;
  toW: number;
  toH: number;
}

export interface SessionToolCardProps {
  /** 几何缺口（来自 useContentColumnGeometry）：决定能否展开 / 能否显示胶囊。 */
  fit: ToolCardFit;
  /** 形态变化回调：父层据此告诉几何层需要让出多少右侧空间（宽度冻结，只改位置）。
   *  animate = 这次变化来自用户手动展开 / 收起（内容列的平移要与卡片形变同步过渡）。 */
  onFormChange?: (form: ToolCardForm, animate?: boolean) => void;
}

export function SessionToolCard({ fit, onFormChange }: SessionToolCardProps) {
  const sessionId = useChatStore((s) => s.currentSessionId);
  const data = useToolCardData(sessionId);

  const wantExpanded = useWantExpanded(sessionId);
  const setExpanded = useToolCardStore((s) => s.setExpanded);
  const toggleSection = useToolCardStore((s) => s.toggleSection);
  const sections = useSectionsOf(sessionId);
  const defaultForm = useToolCardStore((s) => s.defaultForm);
  const sectionDefaults = useToolCardStore((s) => s.sectionDefaults);

  const [menuOpen, setMenuOpen] = useState(false);
  const menuBtnRef = useRef<HTMLButtonElement>(null);
  const shellRef = useRef<HTMLDivElement>(null);

  /** 有内容的栏目（按固定展示顺序）。 */
  const present = useMemo(
    () => TOOL_CARD_SECTIONS.filter((s) => hasContent(data, s)),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [data.git, data.plan, data.process, data.subagent, data.workflow, data.bgtask],
  );

  const form = useMemo<ToolCardForm>(
    () => (sessionId == null ? "hidden" : pickForm(fit, wantExpanded, present.length > 0)),
    [sessionId, fit, wantExpanded, present.length],
  );

  /** 全局默认只从后端读一次（store 记忆），不随卡片挂载重复请求；首屏先用本地缓存。 */
  useEffect(() => { hydrateToolCardPrefs(); }, []);

  // 卡片体限高：按面板高度实测成绝对像素，直接写在外壳的 CSS 变量上（不走 React 状态，拖窗口时逐帧变化也不重渲染）。
  // 必须是像素而不是百分比：百分比限高会随外壳在形变动画里的高度一起变，卡片体被压小就会冒出滚动条再消失。
  const visible = form !== "hidden" && sessionId != null;
  useLayoutEffect(() => {
    if (!visible) return;
    const shell = shellRef.current;
    const host = shell?.offsetParent as HTMLElement | null | undefined;
    if (!shell || !host) return;
    const apply = () => {
      const h = host.clientHeight;
      if (h > 0) shell.style.setProperty("--tcard-max-h", `${Math.max(CARD_MIN_MAX_H, Math.min(CARD_MAX_H, h - CARD_V_GAP))}px`);
    };
    apply();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(apply);
    ro.observe(host);
    return () => ro.disconnect();
  }, [visible]);

  // ── 形变：胶囊 ↔ 卡片 ──
  const [morph, setMorph] = useState<Morph | null>(null);
  /** 用户发起的形变请求：记下点击瞬间外壳的尺寸（FLIP 的 First），形态真正变化后在 layout 阶段消费 */
  const morphFromRef = useRef<{ w: number; h: number; to: "expanded" | "capsule"; at: number } | null>(null);

  const captureMorph = useCallback((to: "expanded" | "capsule") => {
    const el = shellRef.current;
    if (!el || motionDisabled()) return;
    const r = el.getBoundingClientRect();
    morphFromRef.current = { w: r.width, h: r.height, to, at: performance.now() };
  }, []);

  const requestForm = useCallback((to: "expanded" | "capsule") => {
    if (sessionId == null) return;
    captureMorph(to);
    setExpanded(sessionId, to === "expanded");
  }, [sessionId, captureMorph, setExpanded]);

  // 形态变化通知父层：宽度冻结，只有位置随需求变化。
  // plan-156-621：用 useLayoutEffect——卡片本体已按新形态绘制、内容列左移却晚一帧，
  // 展开瞬间会看到「卡片先压住正文再让位」的闪动。必须排在下面的形变 effect 之前（它要读尚未被消费的请求）。
  useLayoutEffect(() => {
    const req = morphFromRef.current;
    const animate = req != null && performance.now() - req.at < MORPH_REQUEST_TTL
      && isCardForm(form) === (req.to === "expanded");
    onFormChange?.(form, animate);
  }, [form, onFormChange]);

  // 形态变化后：外壳已按新形态排好最终布局（只含新形态的内容），此刻量出终点尺寸，再让离场内容挂上，开始形变。
  // 同一个 layout 周期内完成，浏览器首次绘制已经是形变的起点，不会闪出终态。
  useLayoutEffect(() => {
    const req = morphFromRef.current;
    if (!req) return;
    if (form === "hidden") { morphFromRef.current = null; return; }
    if (performance.now() - req.at > MORPH_REQUEST_TTL) { morphFromRef.current = null; return; }
    if (isCardForm(form) !== (req.to === "expanded")) return; // 形态没按请求变：留着等它过期
    morphFromRef.current = null;
    const el = shellRef.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    setMorph({
      dir: isCardForm(form) ? "expanding" : "collapsing",
      fromW: req.w, fromH: req.h, toW: r.width, toH: r.height,
    });
  }, [form]);

  // 形变结束兜底：animationend 在隐藏窗格 / 减弱动效下可能不触发
  useEffect(() => {
    if (!morph) return;
    const el = shellRef.current;
    const dur = el ? parseCssTime(getComputedStyle(el).animationDuration) : 0;
    const timer = window.setTimeout(() => setMorph(null), dur > 0 ? dur + 120 : 0);
    return () => window.clearTimeout(timer);
  }, [morph]);

  // 首次出现（隐藏 → 可见）才播入场动画；形态切换走形变，不重播
  const [entering, setEntering] = useState(false);
  const wasHiddenRef = useRef(true);
  useLayoutEffect(() => {
    const hidden = form === "hidden" || sessionId == null;
    if (!hidden && wasHiddenRef.current && !motionDisabled()) setEntering(true);
    wasHiddenRef.current = hidden;
  }, [form, sessionId]);
  useEffect(() => {
    if (!entering) return;
    const timer = window.setTimeout(() => setEntering(false), 600);
    return () => window.clearTimeout(timer);
  }, [entering]);

  // 收起 / 隐藏时关掉浮层，避免它悬空残留
  useEffect(() => { if (!isCardForm(form)) setMenuOpen(false); }, [form]);

  const onShellAnimationEnd = (e: AnimationEvent<HTMLDivElement>) => {
    if (e.target !== e.currentTarget) return;
    if (e.animationName === "tcard-morph") setMorph(null);
    else if (e.animationName === "tcard-in") setEntering(false);
  };

  if (form === "hidden" || sessionId == null) return null;

  const isCard = isCardForm(form);
  const floating = form === "floating";
  const showCapsule = form === "capsule" || morph?.dir === "expanding";
  const showCard = isCard || morph?.dir === "collapsing";
  /** plan-20261025-shell：完整胶囊会盖住半个消息列时收成只剩图标的小圆钮（此前直接隐藏，窄窗口里找不到工具卡） */
  const compact = fit.capsuleShortfall > 0;

  const top = CAPSULE_PRIORITY.find((s) => present.includes(s)) ?? present[0];
  const rest = otherCount(data, present, top);

  const applyDefaultForm = (next: ToolCardDefaultForm) => {
    // 默认形态变了：当前会话的卡片也立刻照着变（store 会清掉会话级记录），并走同一套形变
    const current: ToolCardDefaultForm = isCard ? "expanded" : "capsule";
    if (next !== current) captureMorph(next);
    setMenuOpen(false);
    saveToolCardForm(next);
  };

  const shellStyle: CSSProperties | undefined = morph
    ? ({
      ["--tc-from-w" as string]: `${morph.fromW}px`,
      ["--tc-from-h" as string]: `${morph.fromH}px`,
      ["--tc-to-w" as string]: `${morph.toW}px`,
      ["--tc-to-h" as string]: `${morph.toH}px`,
    } as CSSProperties)
    : undefined;

  const capsuleLeaving = morph?.dir === "expanding";
  const cardLeaving = morph?.dir === "collapsing";

  const capsule = (
    <button
      type="button"
      className={"tcard-capsule" + (capsuleLeaving ? " is-leaving" : "") + (compact ? " is-compact" : "")}
      onClick={() => requestForm("expanded")}
      aria-label="展开会话工具"
      tabIndex={capsuleLeaving ? -1 : undefined}
      aria-hidden={capsuleLeaving || undefined}
    >
      <CapsuleSummary section={top} data={data} compact={compact} />
      {rest > 0 && !compact && <span className="tcard-cap-more">+{rest}</span>}
    </button>
  );

  const card = (
    <div
      className={"tcard-card" + (cardLeaving ? " is-leaving" : "")}
      role={cardLeaving ? undefined : "region"}
      aria-label={cardLeaving ? undefined : "会话工具"}
      aria-hidden={cardLeaving || undefined}
      {...(cardLeaving ? ({ inert: "" } as InertProps) : {})}
    >
      {/* 滚动时菜单的锚点已经移走：直接关掉 */}
      <div
        className="tcard-card-body"
        onScroll={menuOpen ? () => setMenuOpen(false) : undefined}
      >
        {/* 操作区：与第一个栏目标题行同高，随栏目一起滚动，滚出后被卡片体裁切。
            它按内容坐标贴在顶部，不依赖第一个栏目是哪一类。 */}
        <div className="tcard-card-ops">
          {/* 项目规则：图标按钮统一 ui/IconButton（自带 Tooltip），不新增私有按钮类 */}
          <IconButton
            ref={cardLeaving ? undefined : menuBtnRef}
            size="xs"
            icon={<IconMoreHorizontal size={16} />}
            title="工具卡设置"
            active={menuOpen}
            aria-haspopup="dialog"
            aria-expanded={menuOpen}
            onClick={() => setMenuOpen((v) => !v)}
          />
          <IconButton
            size="xs"
            icon={<IconMinimize size={16} />}
            title="收起"
            aria-label="收起为胶囊"
            onClick={() => requestForm("capsule")}
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
                <span className="tcard-sec-meta" data-tone={sectionTone(section, data)}>{sectionMeta(section, data)}</span>
                <IconChevronDown size={13} className={"tcard-sec-caret" + (open ? " is-open" : "")} />
              </button>
              {/* plan-20261019-motion-sweep：栏目展开 / 收起走 Collapse（高度过渡 + 淡入淡出，收起结束才卸载） */}
              <Collapse open={open} lazy>
                {() => (
                  <div className="tcard-sec-body">
                    {section === "git" && data.git && <GitToolSection data={data.git} actions={data.actions} />}
                    {section === "plan" && data.plan && <PlanSection data={data.plan} />}
                    {section === "process" && data.process && <ProcessSection data={data.process} />}
                    {section === "subagent" && data.subagent && <SubagentSection data={data.subagent} />}
                    {section === "workflow" && data.workflow && <WorkflowSection data={data.workflow} />}
                    {section === "bgtask" && data.bgtask && <BgTaskSection data={data.bgtask} />}
                  </div>
                )}
              </Collapse>
            </section>
          );
        })}
      </div>
    </div>
  );

  return (
    <ToolCardEdgeContext.Provider value={shellRef}>
      <div
        ref={shellRef}
        className={
          "tcard-shell " + (isCard ? "is-card" : "is-capsule")
          + (floating ? " is-floating" : "")
          + (compact && !isCard ? " is-compact" : "")
          + (morph ? ` is-morphing is-${morph.dir}` : "")
          + (entering && !morph ? " is-entering" : "")
        }
        style={shellStyle}
        onAnimationEnd={onShellAnimationEnd}
      >
        {showCapsule && (capsuleLeaving ? capsule : (
          <Tooltip content={compact ? "展开会话工具（悬浮显示）" : "展开会话工具"} side="bottom">
            {capsule}
          </Tooltip>
        ))}
        {showCard && card}
      </div>

      {/* 「⋯」设置：默认形态 + 逐栏默认展开（修改即保存）。浮层走 portal，不被卡片的滚动体裁切 */}
      <Floating
        open={menuOpen && isCard && !morph}
        anchorRef={menuBtnRef}
        onClose={() => setMenuOpen(false)}
        side="bottom"
        align="end"
        gap={6}
        className="tcard-menu"
        role="dialog"
        aria-label="工具卡设置"
        ignoreRefs={[menuBtnRef]}
      >
        <div className="tcard-menu-cap">默认显示</div>
        <Segmented
          size="sm"
          block
          value={defaultForm}
          onChange={applyDefaultForm}
          aria-label="工具卡默认显示形态"
          options={[{ value: "capsule", label: "胶囊" }, { value: "expanded", label: "展开" }]}
        />
        <div className="tcard-menu-cap">栏目默认展开</div>
        {TOOL_CARD_SECTIONS.map((s) => (
          <div className="tcard-menu-row" key={s}>
            <span className="tcard-menu-label">{SECTION_LABEL[s]}</span>
            <Switch
              checked={sectionDefaults[s]}
              onChange={(v) => saveToolCardSection(s, v)}
              aria-label={`${SECTION_LABEL[s]}默认展开`}
            />
          </div>
        ))}
      </Floating>
    </ToolCardEdgeContext.Provider>
  );
}
