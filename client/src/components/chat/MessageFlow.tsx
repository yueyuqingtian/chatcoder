/** MessageFlow（v20 插件化重写）：消息流公共插件。
 * - source="main"（默认）：主会话全局 store 数据，全功能（虚拟化/搜索/JumpDots/计划卡/流式）。
 * - source="subagent"：子代理线程数据（threadId 读 store 桶 + REST 历史合并去重），
 *   窄面板排版，关闭 JumpDots/搜索/计划卡，操作仅复制。
 * 共享内核 MessageFlowCore：虚拟化 + 贴底滚动 + 跳底按钮 + 入场动画 + 搜索 + JumpDots，
 * 与参考项目「同一渲染引擎 + 数据注入」对齐——中间面板与右面板共用同一注册插件。
 */
import { useContext, useEffect, useLayoutEffect, useMemo, useRef, useState, useCallback, useId, memo, type ReactNode } from "react";
import { elementScroll, measureElement as defaultMeasureElement, useVirtualizer, type VirtualItem, type Virtualizer } from "@tanstack/react-virtual";
import type { TimelineEntry, ToolNode, TurnItem } from "./timeline";
import { createTimelineBuilder, createRoundMerger, msgText, lastPersistedText, getTurnById, tailRoundIndex, pruneModelDividers } from "./timeline";
import { isBusy, subscribe } from "../../perf/bus";
import { registerReconcileTask, RECONCILE_ORDER } from "../../perf/reconcile";
import { recordComponentRender, recordDerivation } from "../../perf/metrics";
import { TurnGroup } from "./TurnGroup";
import { JumpDots } from "./JumpDots";
import { DebugCard } from "./DebugCard";
import { StreamingTail } from "./StreamingTail";
import { IconSearch, IconChevronUp, IconChevronDown, IconX, IconArrowDown } from "../icons";
import { ErrorCard } from "./ErrorCard";
// plan-156-619 S10：后续任务建议卡（消息流底部收尾卡）
import { SuggestionCard } from "./SuggestionCard";
import { useTextHighlight } from "../../hooks/useTextHighlight";
import { MarkdownContent } from "../MarkdownContent";
import { MsgType } from "@chatcoder/shared";
import { useChatStore } from "../../store/chat";
import { useUiStore } from "../../store/ui";
import { useScoped, useScopedGetter, ConvoScopeContext } from "./convoScope";
import { latestWorkflowEndMs, selectSessionWorkflowBusy, useWorkflowStore } from "../../store/workflow";
import { api } from "../../api/client";
import { Empty } from "../ui";
import { StickyBottom } from "./stickyBottom";

/** plan-31-152 S5-4：运动期（拖分隔条/窗口缩放）视口 rect 的提交节流间隔（毫秒）。
 *  逐帧提交会让虚拟器每帧 setState → 整棵可见消息树 React 重渲染（含 markdown/工具树），
 *  这是"拖动左右侧面板卡顿"的主因（空态首页无消息所以不卡）。取 ~120ms（约 8 帧一次）：
 *  既让虚拟器的可见范围跟上宽度变化，又把整树重渲染频率压到拖拽可接受的水平；
 *  运动结束由收敛序列（order 20）立即提交最终 rect 保证精确。 */
const MOTION_RECT_THROTTLE_MS = 120;

/** plan-20261024-jumpfix：跳转后 scrollspy 焦点锁定的兜底时长（毫秒）。
 *  覆盖平滑滚动 + 落点校正的收敛窗口即可，到期即交还控制，避免锁滞留。 */
const JUMP_LOCK_TTL_MS = 1600;

/** plan-20261024-jumpfix：跳转收敛后的落点守护时长（毫秒）。
 *  期间目标项被贴底补滞 / 虚拟化测量补偿 / 内容增长推走时立即拉回；
 *  用户任何滚动输入都会提前结束守护。 */
const JUMP_HOLD_MS = 700;

/* plan-549-2281（第13项）：定位的丝滑缓动参数。
 *
 * 为什么自己用 rAF 插值，而不是给 scrollToIndex 传 behavior:"smooth"：
 * 浏览器平滑滚动会「接管」scrollTop 的写入权限，校正循环（jumpToIndex 里的 step）
 * 写进去的值会被动画覆盖，落点最终停在估算位置——这正是 plan-20261024-jumpfix
 * 记录的「点倒数第二个却落到倒数第一条用户消息」的根因，当时的结论就是「不用 smooth」。
 * 现在改成自己在动画期间写 scrollTop、动画结束才启动校正：两者不重叠，既拿到过渡动画，
 * 又保住落点精度。 */
const JUMP_ANIM_MIN_PX = 32;      // 距离小于此值直接瞬时到位（一帧的事，动画反而是噪音）
const JUMP_ANIM_MAX_DIST_PX = 2400; // 超过约两屏不缓动：途中要跨越大量未测量项，长动画只会更晃
const JUMP_ANIM_BASE_MS = 180;
const JUMP_ANIM_MAX_MS = 520;

/** 缓动曲线：ease-out-cubic（快速起步、末端收敛，长距离定位不至于拖沓）。 */
const jumpEase = (t: number): number => 1 - Math.pow(1 - t, 3);

/** plan-20261021-scroll-comp：条目尺寸变化后要不要补偿 scrollTop（上滑经过用户消息处回弹 / 跳变的根治）。
 *
 *  现象：向上滚动时，视口上方某个历史轮次（刚被挂载进 overscan 的那条）的高度在挂载后继续变化——
 *  变更清单卡片异步到达（+195px）、计划卡渲染后被裁切（−44px）、与缓存尺寸的细小差值……
 *  变化发生在视口**上方**，它之后的所有条目被整体推下 / 拉上，而 scrollTop 没跟着动，
 *  画面就是「往回弹一下」。每个轮次都以用户消息开头，视口顶边越过它时 overscan 窗口前移、
 *  最上方多挂载一个历史轮次，所以回弹恰好落在「我发送消息的位置」。
 *
 *  根因：虚拟器 3.17 的默认口径对「重测」（该条目此前已有缓存尺寸，历史轮次再次挂载都属此类）
 *  在向上滚动期间一律不补偿；原生滚动锚定又因虚拟项 top 被重写而失效（top 变化是锚定的抑制条件）。
 *  于是视口上方条目的任何尺寸变化都直接露成位移。
 *
 *  口径：与默认一致，只去掉「向上滚动不补偿」——首次测量：整块估算高度在视口上方即补偿；
 *  重测：只补偿**整条**已在视口上方的条目。跨过视口顶边的条目不补偿——它里面变化的部分
 *  若在视口上方，由浏览器原生锚定负责（锚点在同一条目内，不受 top 重写影响）。 */
function compensateItemAboveViewport(
  item: VirtualItem,
  _delta: number,
  instance: Virtualizer<HTMLDivElement, Element>,
): boolean {
  const offset = instance.scrollOffset ?? 0;
  return instance.itemSizeCache.has(item.key) ? item.end <= offset : item.start < offset;
}

/* plan-586-2374 §2.1：这一刻要不要跳过该项的测量。
 *
 * 条件 = 「几何运动期」+「项在视口 ± 1 屏之外」，两个同时成立才跳过。
 *   · 运动期是前提：静止期一律真测，行为与改造前完全一致（门控只在 width 逐帧变化时生效）；
 *   · ± 1 屏安全边距（§2.3）：overscan 项离视口可能很近，其内容随宽度增长会挤到视口边缘。
 * 下面几道保守放行都是为了守住「视口内项永远真测」这条视觉不变量：
 * 解析不出下标、从未真测过（估算位置不可信）、拿不到几何——一律走真实测量。 */
function shouldSkipMeasurement(
  index: number,
  instance: Virtualizer<HTMLDivElement, Element>,
  visible: Set<number>,
): boolean {
  if (index < 0) return false;
  if (visible.has(index)) return false; // 集合命中：明确落在视口 ± 1 屏内
  const key = instance.options.getItemKey(index);
  // 首次挂载项还没有真实高度，估算位置不足以判断远近，必须真测
  if (!instance.itemSizeCache.has(key)) return false;
  const rect = instance.scrollRect;
  const offset = instance.scrollOffset;
  if (!rect || offset == null) return false;
  const item = instance.measurementsCache[index];
  if (!item) return false;
  const margin = rect.height; // 一屏安全边距
  return !(item.start < offset + rect.height + margin && item.end > offset - margin);
}

/* plan-20261020-pts：贴底跟随改由 StickyBottom 内核负责（见 ./stickyBottom.ts）。
   此前这里的程序滚动宽限、跟随抑制有效期、两档距底阈值都是给「滚动事件里按距底距离重算跟随」
   打的补丁——那个判定本身会把测量补偿 / 滚动锚定 / 视口塌缩误判成用户离开底部，已整体移除。 */

/** plan-75-334 阶段1：子代理面板不消费「turn 错误抑制列表」——固定传空数组，
 *  避免每次渲染新建 [] 破坏 TurnGroup 的 memo 命中。 */
const EMPTY_TURN_ERRORS: number[] = [];

/** plan-308-1542 需求1：任务执行类错误的**唯一**棂位——消息流末尾错误卡。
 *  此前这类错误会同时写入 store.error（右上角 Toast）与消息流，造成重复报错；
 *  现在 sendTurn 失败 / turn.failed / 重试 / 回滚 / 审核失败等只走本卡。 */
const FlowErrorCard = memo(function FlowErrorCard({
  text, onRetry, onClose,
}: { text: string; onRetry?: () => void; onClose: () => void }) {
  // plan-387-1825 C1：与 turn 内 ERROR 消息共用同一组件，避免两份 markup 各改各的
  return (
    <ErrorCard
      text={text}
      title="任务执行出错"
      variant="trailing"
      onRetry={onRetry}
      onClose={onClose}
    />
  );
});

/** v39: 后台子代理运行期间的等待行（主会话保持“运行中”的视觉表达）。
 *  与 StreamingText 的状态行同款样式（呼吸点 + 文案），语义为「等子代理结束」；
 *  子代理结束会自动创建唤醒轮，届时转为常规流式尾部。 */
const SubagentWaitingLine = memo(function SubagentWaitingLine({ count }: { count: number }) {
  return (
    <div className="turn-group turn-flow streaming-tail">
      <div className="turn-status-line">
        <span className="thinking-breath-dot" />
        <span className="thinking-block-status">
          {count > 1 ? `等待子代理结束…（${count} 个运行中）` : "等待子代理结束…"}
        </span>
      </div>
    </div>
  );
});

/** 工具节点可搜索文本（group 取聚合工具名；其余取各 leaf 工具名） */
function nodeToolText(n: ToolNode): string {
  if (n.kind === "group") return n.tool;
  if (n.kind === "leaf") return n.leaf.tool;
  return n.leaves.map((l) => l.tool).join(" ");
}

/** turn 条目可搜索文本 */
function itemText(it: TurnItem): string {
  if (it.kind === "tools" || it.kind === "workflow" || it.kind === "shot") return "";
  return msgText(it.msg.content);
}

/** v20: message-flow 插件数据契约——宿主（中间面板/右面板）经 PluginSlot props 注入。 */
export interface MessageStreamProps {
  /** 数据源模式：main=主会话全局 store；subagent=子代理线程（按 threadId 取 store 桶） */
  source?: "main" | "subagent";
  /** subagent 模式必填：线程 id（=agentId），用于读取 subagentMessages/Thinking/Streams 与 REST 历史 */
  threadId?: number;
  /** 功能开关（缺省按模式：main 全开；subagent 关闭 JumpDots/搜索/计划卡，操作仅复制） */
  features?: {
    jumpDots?: boolean;
    search?: boolean;
    planCard?: boolean;
    /** plan-156-621：后续任务建议卡。侧边对话复用本组件时关闭——
     *  建议属于主会话那轮收尾，显示在侧边对话里会把用户切离当前会话。 */
    suggestionCard?: boolean;
    /** 消息操作行能力：full=完整（赞踩/重试/回滚）；copy-only=仅复制；none=无操作行 */
    actions?: "full" | "copy-only" | "none";
  };
  /** 滚动目标：当外部点击任务卡时传 { turnId } 或 { threadId }，滚动到对应节点 */
  scrollTarget?: { threadId?: number; turnId?: number } | null;
  clearScrollTarget?: () => void;
  /** 主消息流为空时的提示文案（嵌入场景可自定义，如侧边对话） */
  emptyText?: string;
  /** plan-75-334 阶段3：宿主可见性。false 时子代理尾部不推进流式视觉更新
   *  （恢复可见时以当前缓冲一次性对齐）。主会话不传（默认可见）。 */
  visible?: boolean;
  className?: string;
}

/** 独立非 turn 消息条目（如 system/error 等非结构化消息） */
const StandaloneEntry = memo(function StandaloneEntry({ entry }: { entry: TimelineEntry }) {
  if (entry.kind === "turn") return null;
  // 压缩块摘要（SUMMARY + checkpoint）独立成卡片，其余按普通文本渲染
  if (entry.msg.msg_type === MsgType.Summary && (entry.msg.content as Record<string, unknown>).checkpoint === true) {
    return (
      <div className="turn-group">
        <div className="turn-item turn-item-summary">
          {/* v36 (plan-321-1600 M2): 同 case "summary" —— 补 .turn-agent-text 包裹，
              使其获得与主消息流一致的 markdown 排版。 */}
          <div className="turn-agent-text">
            <MarkdownContent>{msgText(entry.msg.content)}</MarkdownContent>
          </div>
        </div>
      </div>
    );
  }
  // plan-41-229: 系统消息（模型切换 divider 等）落在**首个 turn 之前**时会是 standalone 条目，
  // 旧实现直接落到下方普通正文分支 ⇒ 既无分割线结构也无 .turn-divider-* 类名，
  // 表现为「无样式纯文本」（用户反馈截图）。此处与 TurnGroup 的 divider 分支保持同一套
  // DOM 结构与类名，样式天然一致，不新增 CSS。
  if (entry.msg.msg_type === MsgType.System) {
    return (
      <div className="turn-group">
        <div className="turn-item turn-item-divider">
          <span className="turn-divider-line" />
          <span className="turn-divider-text">{msgText(entry.msg.content)}</span>
          <span className="turn-divider-line" />
        </div>
      </div>
    );
  }
  return (
    <div className="turn-group">
      <div className="turn-item turn-item-text">
        <div className="turn-agent-text">
          <MarkdownContent>{msgText(entry.msg.content)}</MarkdownContent>
        </div>
      </div>
    </div>
  );
});

/** v20: 消息流共享内核——虚拟化 + 贴底滚动跟随 + 跳底按钮 + 入场动画 + 搜索 + JumpDots。
 * 主会话与子代理面板共用；宿主差异（数据源/功能开关/空态文案）经 props 注入。 */
interface MessageFlowCoreProps {
  entries: TimelineEntry[];
  /** 是否运行中（决定流式占位与贴底策略） */
  running: boolean;
  /** 渲染单条 entry（外层闭包提供 TurnGroup/StandaloneEntry 与各模式差异） */
  renderEntry: (entry: TimelineEntry, index: number) => ReactNode;
  /** 运行中尾部（StreamingText），占虚拟列表最后一项 */
  streamingNode: ReactNode | null;
  /** 额外尾部（如压缩中卡片），占最后一项 */
  trailingNode?: ReactNode | null;
  /** 强制贴底信号（单调递增）。
   *
   *  本轮修复（用户反馈"运行中发送的消息没有固定位置，会被刷到下方"）：
   *  「立即发送」的注入消息**不再从时间线抽离**（旧实现把它渲染到流式段下方的独立槽位，
   *  于是它的位置随流式内容增长不断下移，turn 结束后又回归时间线 ⇒ 位置反复跳）。
   *  现在它按 id 序固定留在时间线内；但它落进的是**已存在的** turn entry，
   *  entries.length 不变 ⇒ 既有的"末尾新增以用户消息开头的新 turn → 滚底"判定不会触发。
   *  故由宿主显式下发该信号，保证"点了发送就滚到最新"的意图仍然成立。 */
  forceBottomKey?: number;
  /** 会话标识：变化时强制跳底（主界面传 currentSessionId，子代理传 threadId） */
  sessionKey: string | number;
  /** 贴底跟随的流式信号 prop 已移除（S8）：MessageFlowCore 内部直接订阅 store 缓冲，
   *  避免“缓冲每帧换引用 → 整棵消息树每帧重渲染”。 */
  /** 功能开关（默认按模式由外层传入） */
  jumpDots?: boolean;
  search?: boolean;
  /** 滚动目标（主界面任务卡点击穿透 / turn 导航） */
  scrollTarget?: { threadId?: number; turnId?: number } | null;
  clearScrollTarget?: () => void;
  className?: string;
  emptyText?: string;
}

/** plan-282-1421（第11项）：单个虚拟项的高亮包装。
 *  虚拟列表项随滚动挂载/卸载，用组件封装可让 hook 生命周期与项一致，
 *  避免在父级维护"下标 → ref"映射带来的清理负担。 */
const HighlightedItem = memo(function HighlightedItem({ keyword, active, children }: {
  keyword: string;
  active: boolean;
  children: ReactNode;
}) {
  const ref = useTextHighlight<HTMLDivElement>(keyword, active);
  return <div className="mf-list" ref={ref}>{children}</div>;
});

function MessageFlowCore({
  entries,
  running,
  renderEntry,
  streamingNode,
  trailingNode,
  forceBottomKey = 0,
  sessionKey,
  jumpDots = true,
  search = true,
  scrollTarget,
  clearScrollTarget,
  className,
  emptyText = "暂无消息",
}: MessageFlowCoreProps) {
  // plan-75-334 阶段0：记录组件渲染（仅采集期间统计，零开销）
  recordComponentRender("messageFlowCore");

  /* plan-20261018-sticky-fix：本实例的唯一标识。
     同页可能同时存在多个 MessageFlowCore（主消息流 + 右面板「侧边对话」+「子代理」，
     且右面板标签常驻挂载）。reconcile 任务表以 id 为键、后注册者覆盖前者，
     固定 id 会让主实例的「还原滚动锚点 / 提交虚拟器视口尺寸」被别处顶掉——
     锚点还原永不执行、虚拟器视口尺寸停在旧值。 */
  const instanceId = useId();

  const parentRef = useRef<HTMLDivElement>(null);
  /** plan-547: 虚拟内容容器（RO 监听测高变化保持贴底） */
  // plan-282-1444：显式含 null 联合类型，保证 ref 可变（需在回调里同时交给虚拟列表）
  const innerRef = useRef<HTMLDivElement | null>(null);
  const scrollIdleTimerRef = useRef(0);
  const [showScrollBottom, setShowScrollBottom] = useState(false);
  /** plan-20261020-pts：贴底跟随内核。每个实例一份（切会话时宿主按会话 key 重建本组件，
   *  新会话天然从「跟随」开始）；跟随状态只由用户意图改变，详见 stickyBottom.ts。 */
  const [sticky] = useState(() => new StickyBottom({ onJumpChange: setShowScrollBottom }));

  /** plan-282-1441（#2）：宽度变化时的"视口锚点"。
   *  拖拽面板分隔条会改变消息列宽度 → 文本重排 → 每条消息高度变化 → 虚拟列表重新测量
   *  → 总高度变化 → 同一个 scrollTop 对应的内容整体位移（"拖宽时消息位置漂移"）。
   *  plan-20261020-pts：只在**不跟随**时记录"首条可见项 + 其相对视口偏移"，重测收敛后
   *  让那条消息停回原来的视口位置；跟随中由 StickyBottom 在每次尺寸变化时当帧贴底。 */
  const widthAnchorRef = useRef<
    { index: number; offset: number; ratio: number; untrusted: boolean } | null
  >(null);
  const lastWidthRef = useRef(0);
  const [searchOpen, setSearchOpen] = useState(false);
  const searchInputRef = useRef<HTMLInputElement>(null);
  const [searchKeyword, setSearchKeyword] = useState("");
  const [activeMatchIndex, setActiveMatchIndex] = useState(0);
  /** 问题12: scrollspy——当前视口焦点 entry 下标，用于 JumpDots 自动聚焦对应小横条 */
  const [activeEntryIndex, setActiveEntryIndex] = useState(0);

  /** plan-20261024-jumpfix：跳转落点校正 + scrollspy 焦点锁定的收敛状态。
   *  - spyLockRef：锁定的目标 entry 下标（null=未锁定）；
   *  - jumpTokenRef：跳转代号，重入（连点刻度 / 搜索连跳）时旧校正循环立即作废；
   *  - jumpRafRef / jumpTtlRef：校正循环帧句柄与解锁兜底定时器；
   *  - jumpCleanupRef：本轮跳转的收尾函数（取消帧、定时器与用户接管监听）。 */
  const spyLockRef = useRef<number | null>(null);
  const jumpTokenRef = useRef(0);
  const jumpRafRef = useRef(0);
  const jumpTtlRef = useRef(0);
  const jumpCleanupRef = useRef<(() => void) | null>(null);

  const hasStreaming = Boolean(running && streamingNode);
  const hasTrailing = Boolean(trailingNode);
  const totalCount = entries.length + (hasStreaming ? 1 : 0) + (hasTrailing ? 1 : 0);

  /** plan-282-1434（B6）+ plan-282-1444：首帧定位状态。
   *  此前：虚拟列表初始 scrollOffset=0 → 先渲染顶部若干项，再由 useLayoutEffect 把
   *  scrollTop 推到底，多帧叠加后表现为"点击会话后从上往下滚一遍"。
   *  现在：① initialOffset 按末项估算位置给出初始偏移，首帧即落在底部区域；
   *        ② 定位**收敛**（总高连续稳定）前用 .is-positioning 隐藏内容。 */


  const [positioned, setPositioned] = useState(false);
  const EST_ITEM_H = 140;

  /** S9a（plan-329-1647）：按条目类型的**实测高度**分桶估算，替代固定 140。
   *
   *  为何分桶：条目真实高度差异极大——单行用户消息 ~60px，含长文 + 工具树的 turn 可达上千 px。
   *  统一按 140 估算会让「估算 → 实测」跳变很大，表现为长会话首屏收敛慢、滚动中总高反复变化。
   *  采样来源**零额外 DOM 读取**：直接取虚拟器当前可见项的 size（可见项在视口内必已挂载，
   *  挂载即被 measureElement 测过），取中位数抗异常项。 */
  const heightEstRef = useRef({ turn: EST_ITEM_H, standalone: EST_ITEM_H, stream: EST_ITEM_H, trailing: EST_ITEM_H });
  /** 条目 index → 估算桶（槽位顺序与渲染顺序一致：已落库 entries → 流式段 → 尾部卡片）。 */
  const estBucketOf = useCallback((index: number): "turn" | "standalone" | "stream" | "trailing" => {
    if (hasStreaming && index === entries.length) return "stream";
    if (hasTrailing && index === entries.length + (hasStreaming ? 1 : 0)) return "trailing";
    return entries[index]?.kind === "turn" ? "turn" : "standalone";
  }, [entries, hasStreaming, hasTrailing]);

  /** 虚拟器视口尺寸的「运动后提交」函数（S6 / RFL-6）。
   *  由 observeElementRect 在内部赋值：运动期它只记账（missedDuringMotion），
   *  需要有人叫一次才提交。原本靠 window-motion / pointerup / panel-drag-end 三个事件，
   *  现在统一由唯一收敛序列（reconcile）按 order 20 调用一次。 */
  const settleRectRef = useRef<(() => void) | null>(null);

  /** 运动期降低 overscan（拖拽卡顿治理 P2-2）：拖拽 / 窗口运动期间每条离屏项也会随宽度变化
   *  逐帧折行，它们不可见却同样占满帧预算；降到 2 仍能覆盖滚动余量，静止后立即恢复 6。
   *  注意：**不做**「离屏项 content-visibility 跳过」——那会让虚拟器测到估算高度、
   *  总高反复跳变，引发重叠/错位（详见 docs/panel-drag-jank-optimization.md P2-2）。 */
  const [overscan, setOverscan] = useState(6);
  useEffect(() => subscribe((busy) => setOverscan(busy ? 2 : 6)), []);

  /* plan-586-2374 §2.1：运动期只测视口内，视口外沿用缓存尺寸。
   *
   * 为什么必须用 ref：这个集合会随滚动 / 布局变化刷新，若走 useState，每次刷新都会
   * 重渲染整棵可见消息树（含 markdown 与工具树），这正是 `MessageFlow.tsx` 中
   * directDomUpdates 注释记载的卡顿主因；measureElement 也只需要读到最新值。
   * 刷新时机见下方 refreshVisibleIndexes 的调用点说明。 */
  const visibleIndexSetRef = useRef<Set<number>>(new Set());
  /** 本轮运动期是否真的发生过跳过：收敛序列据此决定要不要补测（没跳过就完全零动作）。 */
  const skippedDuringMotionRef = useRef(false);

  /** plan-586-2374 §2.1：自定义测量口径（覆盖 VirtualizerOptions.measureElement）。
   *  静止期与改造前**完全一致**（一律 defaultMeasureElement 真测）；只有「运动期 + 视口外」
   *  才返回缓存/估算值，从而既不读 DOM、也不推进 itemSizeCacheVersion → 不触发
   *  getMeasurements 的后缀重算。视口内的项永远真测，这是视觉不变量。 */
  const measureElementCb = useCallback(
    (el: Element, entry: ResizeObserverEntry | undefined, instance: Virtualizer<HTMLDivElement, Element>): number => {
      const index = instance.indexFromElement(el);
      if (isBusy() && shouldSkipMeasurement(index, instance, visibleIndexSetRef.current)) {
        skippedDuringMotionRef.current = true;
        recordDerivation("virtualMeasureSkipped");
        const key = instance.options.getItemKey(index);
        return instance.itemSizeCache.get(key) ?? instance.options.estimateSize(index);
      }
      recordDerivation("virtualMeasure");
      return defaultMeasureElement(el, entry, instance);
    },
    [],
  );

  const virtualizer = useVirtualizer({
    count: totalCount,
    getScrollElement: () => parentRef.current,
    estimateSize: (index) => heightEstRef.current[estBucketOf(index)],
    // 仅在 scrollOffset 为 null（首帧）时消费：按"末项估算起点"初始化，避免从顶部渲染
    initialOffset: () =>
      totalCount > 0 ? Math.max(0, (totalCount - 1) * heightEstRef.current[estBucketOf(totalCount - 1)]) : 0,
    overscan,
    /* plan-586-2374 §2.1：测量门控。注意渲染处的 ref 仍用实例方法
       `virtualizer.measureElement`（挂载/卸载登记 + 首次测量），它内部会调用本选项。 */
    measureElement: measureElementCb,
    /* plan-282-1444：位置改由虚拟列表**直接写 DOM**。
     *
     *  根因：每一项都是 `position:absolute`，位置来自虚拟列表的尺寸缓存。新挂载
     *  或长高的一项（典型：流式尾部下方新落库一条正文、工具行展开）要等
     *  ResizeObserver 回调才把真实高度写回缓存；而回调里走的是普通 React 重渲染
     *  （`resizeItem → notify(false) → rerender()`，进调度器 → **落在本次绘制之后**）。
     *  于是这一帧里后续项仍按旧值/`estimateSize`(140) 排版 → 与被撑高的内容**重叠**，
     *  下一帧测量回写才归位（"新行与原行短暂重叠后又正常"）。
     *
     *  开启后：位置在布局阶段（`applyDirectStyles`）同步写入 DOM，修正结果参与
     *  同一帧绘制，不再有"旧位置帧"。要求项元素不得再在 style 里自带主轴向位置，
     *  内层容器也不得再写 height（见下方渲染）。 */
    directDomUpdates: true,
    // 消息数较多时默认 transform 会让每个可视虚拟项进入合成层；切为 top 定位，
    // 避免窗口动画/滚动时维护大量图层。绝对定位项已满足 position 模式契约。
    directDomUpdatesMode: "position",
    /* plan-20261021-scroll-comp：补偿写 scrollTop 之前先把内容容器撑到新总高。
       directDomUpdates 下容器高度要等 onChange 才写入，而补偿发生在它之前：视口离底部不足补偿量时，
       浏览器会按旧高度钳制 scrollTop，补偿被截断，剩下的部分又露成位移；
       钳制到底部还会让贴底内核误判成「回到了底部」。 */
    scrollToFn: (offset, options, instance) => {
      const inner = innerRef.current;
      if (inner) inner.style.height = `${instance.getTotalSize()}px`;
      elementScroll(offset, options, instance);
    },
    // plan-20261020-pts：测量回调里条目尺寸 / 位置 / 总高刚写入 DOM，此时请求贴底可在同一帧完成
    // （上方条目变高时尾项尺寸不变，容器高度的尺寸通知又要晚一帧，只靠尺寸观察会露出一帧未贴底）
    onChange: () => sticky.requestPin(),
    // plan-31-152 S5-4（用户反馈"拖左右侧面板卡顿，空态首页不卡"）：
    //   拖拽期间容器宽度每帧在变，若每帧都 cb 提交 rect → 虚拟器 setState →
    //   **整棵可见消息树 React 重渲染**（含 markdown/工具树），这正是卡顿主因；
    //   空态首页没有消息，所以感觉不卡。
    //   现在按状态分流：
    //     · 运动期（拖分隔条/窗口缩放）→ **低频节流提交**（MOTION_RECT_THROTTLE_MS）：
    //       虚拟器仍能跟上宽度变化（可见范围计算），但不再逐帧触发整树重渲染；
    //     · 静止期 → rAF 合并提交（跟手，无额外开销）。
    //   运动结束后的最终 rect 由收敛序列（order 20）立即提交，保证精确。
    observeElementRect: (instance, cb) => {
      const el = instance.scrollElement;
      if (!el) return;
      const readFinalRect = () => ({ width: Math.round(el.clientWidth), height: Math.round(el.clientHeight) });
      cb(readFinalRect()); // 首帧立即提交
      let lastWidth = el.clientWidth;
      let lastHeight = el.clientHeight;
      let raf = 0;
      let throttleTimer = 0;
      const commit = () => {
        const width = el.clientWidth;
        const height = el.clientHeight;
        if (width === lastWidth && height === lastHeight) return;
        lastWidth = width;
        lastHeight = height;
        cb({ width: Math.round(width), height: Math.round(height) });
      };
      const scheduleNotify = () => {
        if (isBusy()) {
          // 运动期：节流为一拍一次（上一拍未到则合并进这一拍）
          if (throttleTimer) return;
          throttleTimer = window.setTimeout(() => {
            throttleTimer = 0;
            commit();
          }, MOTION_RECT_THROTTLE_MS);
          return;
        }
        if (raf) return; // 静止期：本帧已排队，合并为一次
        raf = requestAnimationFrame(() => {
          raf = 0;
          commit();
        });
      };
      const ro = new ResizeObserver(scheduleNotify);
      ro.observe(el);
      const settle = () => {
        // 收敛序列（order 20）调用：取消所有 pending，立即提交最终 rect
        if (raf) { cancelAnimationFrame(raf); raf = 0; }
        if (throttleTimer) { clearTimeout(throttleTimer); throttleTimer = 0; }
        lastWidth = el.clientWidth;
        lastHeight = el.clientHeight;
        cb({ width: Math.round(lastWidth), height: Math.round(lastHeight) });
      };
      settleRectRef.current = settle; // 供收敛序列（order 20）调用
      return () => {
        if (settleRectRef.current === settle) settleRectRef.current = null;
        if (raf) cancelAnimationFrame(raf);
        if (throttleTimer) clearTimeout(throttleTimer);
        ro.disconnect();
      };
    },
  });
  // 实例属性而非构造选项；赋值幂等（模块级函数、引用恒定），与 setOptions 一样在渲染期写入，
  // 保证首个 ref 回调里的同步测量就已按本口径判定。
  virtualizer.shouldAdjustScrollPositionOnItemSizeChange = compensateItemAboveViewport;

  /** 内层尺寸容器引用：既要给 RO 测高（innerRef），也要交给虚拟列表写高度。
   *  必须用稳定回调——内联箭头函数每次渲染都会触发 ref 脱挂/重挂，导致
   *  容器尺寸被反复重置。 */
  /** S9a：从虚拟器当前可见项采样高度，更新分桶估算（中位数，抗异常项）。
   *  调用点：条目数变化后、首屏定位收敛后——均为低频时机，不进逐帧路径。
   *  阈值 4px 内不改写，避免估算值抖动引发不必要的重排。 */
  const sampleHeights = useCallback(() => {
    const items = virtualizer.getVirtualItems();
    if (items.length === 0) return;
    const buckets: Record<"turn" | "standalone" | "stream" | "trailing", number[]> = {
      turn: [], standalone: [], stream: [], trailing: [],
    };
    for (const it of items) {
      const size = it.size;
      if (!Number.isFinite(size) || size <= 0) continue;
      buckets[estBucketOf(it.index)].push(size);
    }
    const est = heightEstRef.current;
    for (const k of Object.keys(buckets) as Array<keyof typeof est>) {
      const arr = buckets[k];
      if (arr.length < 2) continue; // 样本太少：保留原值，避免被单个异常项带偏
      arr.sort((a, b) => a - b);
      const mid = Math.round(arr[Math.floor(arr.length / 2)]);
      if (Math.abs(mid - est[k]) >= 4) est[k] = mid;
    }
  }, [virtualizer, estBucketOf]);

  const setInnerRef = useCallback((node: HTMLDivElement | null) => {
    innerRef.current = node;
    virtualizer.containerRef(node);
    // plan-20261020-pts：内容容器交给贴底内核观察（空态 → 有内容时节点才出现）
    sticky.setContent(node);
  }, [virtualizer, sticky]);

  /* plan-586-2374 §2.1：刷新「视口 ± 1 屏」内的 index 集合（测量门控的白名单）。
   *
   * 为什么限定在低频时机刷新、且不进 measureElement 内部反查：
   *   · getVirtualItems() 本身要取测量缓存 + 计算可见范围，逐次测量里再调一次等于把
   *     门控变成新的开销源，而且测量发生在虚拟器内部、重入语义不清；
   *   · 集合是「提前算好、多帧复用」的语义，它只需要覆盖运动期那 260ms 的判定。
   * 调用点（均由下面的代码标注）：滚动路径的 rAF 合并、条目数/定位收敛变化、
   * 以及运动开始事件（此时布局尚未改写，读到的几何最干净）。
   * 复用同一个 Set 对象：measureElement 持有它的引用，换对象会让门控读到空集合。
   *
   * 白名单必须按**不含 overscan** 的可见范围建立（用 `virtualizer.range`，不是
   * `getVirtualItems()`）：运动期 overscan 会从 6 降到 2，而降低是异步 state 更新、
   * 晚于 drag-start。若按 `getVirtualItems()`（此刻仍是 range ± 6）建表，随后实际渲染的
   * range ± 2 是它的子集、必然全部命中白名单 → 门控 100% 放行、等于没生效
   * （此坑由 plan-586-2374 的静态审查发现）。改用 `range` 后两侧都以「可见范围」为基准，
   * 只有真正离视口一屏以上的项才会被跳过。 */
  const refreshVisibleIndexes = useCallback(() => {
    const el = parentRef.current;
    const set = visibleIndexSetRef.current;
    set.clear();
    if (!el) return;
    const offset = el.scrollTop;
    const height = el.clientHeight;
    if (height <= 0) return;
    const margin = height; // 视口 ± 1 屏安全边距（§2.3）
    /* plan-586-2374：以「不含 overscan 的可见范围」为基准（`virtualizer.range`）。
       运动期 overscan 从 6 降到 2 是异步 state 更新、晚于 drag-start：
       若这里用 getVirtualItems()（此刻含 ±6），随后实际渲染的 ±2 是它的子集，
       必然全部命中白名单 → 门控 100% 放行、等于没生效。
       逐 index 取 measurementsCache 而不是 getVirtualItems()：前者不构造 VirtualItem 对象，
       且 range 的边界已由虚拟器算好，这里只做「按一屏余量外扩」。 */
    const range = virtualizer.range;
    const count = virtualizer.options.count;
    if (!range) {
      // 首帧（range 尚未算出）：退化为按 getVirtualItems 建表，宁可多测不可漏测
      for (const it of virtualizer.getVirtualItems()) {
        if (it.start < offset + height + margin && it.end > offset - margin) set.add(it.index);
      }
      return;
    }
    const last = Math.min(range.endIndex, count - 1);
    for (let i = Math.max(0, range.startIndex); i <= last; i++) {
      const m = virtualizer.measurementsCache[i];
      if (!m) continue;
      if (m.start < offset + height + margin && m.end > offset - margin) set.add(i);
    }
  }, [virtualizer]);

  /** 滚动路径：rAF 合并刷新（同一帧多次 scroll 只算一次）。
   *  运动期不执行——与 scheduleSpy 同口径（P1-4：运动期读 scrollTop/clientHeight 会升级成
   *  强制同步布局，与逐帧折行叠加）。集合在运动开始那一刻已刷过，且 measureElement 里还有
   *  不读 DOM 的几何兜底（shouldSkipMeasurement 只查虚拟器缓存），滞后不会误跳过视口内项。 */
  const visRafRef = useRef(0);
  // 卸载时必须取消尚未执行的帧：否则回调会去读已卸载的容器几何
  useEffect(() => () => { if (visRafRef.current) cancelAnimationFrame(visRafRef.current); }, []);
  const scheduleVisibleRefresh = useCallback(() => {
    if (isBusy()) return;
    if (visRafRef.current) return;
    visRafRef.current = requestAnimationFrame(() => {
      visRafRef.current = 0;
      refreshVisibleIndexes();
    });
  }, [refreshVisibleIndexes]);

  /* RFL-1（plan-329-1647 S4）：原本这里还有一个「冻结消息列内容宽度」的 effect
     （拖拽/窗口运动期间把 innerRef 的宽度钉成当前像素值，松手再解冻）。
     它确实降低了拖拽期的重排量，但代价是**排版不实时**：面板变宽后内容右侧留白、
     变窄则被裁切，直到松手才重新折行（用户反馈的问题）。现已删除：
       · 宽度实时跟随 → 文本逐帧折行（所见即所得）；
       · 拖拽期的重排成本改由「零布局读预算 + 帧闸门（RFL-2 / RFL-5）」控制；
       · 位置稳定交给浏览器原生滚动锚定 + 结束时的收敛序列（RFL-4 / RFL-6）。
     注：右面板内容根仍由 ResizeHandle.freezeRefs 冻结（见 App.tsx）。 */

  /** plan-20261020-pts：贴底内核绑定滚动容器。布局阶段绑定，保证首帧的尺寸观察与滚动事件都不漏。 */
  useLayoutEffect(() => {
    const el = parentRef.current;
    if (!el) return;
    sticky.attach(el);
    return () => sticky.detach();
  }, [sticky]);

  /** 问题12: scrollspy——取视口上 1/3 焦点线所在虚拟项，映射为 entry 下标传给 JumpDots */
  const updateActiveEntry = useCallback((el: HTMLDivElement) => {
    // plan-20261024-jumpfix：跳转收敛期间焦点锁定在目标项，不被焦点线重算覆盖
    if (spyLockRef.current != null) return;
    const items = virtualizer.getVirtualItems();
    if (items.length === 0) return;
    const focusY = el.scrollTop + el.clientHeight * 0.33;
    let idx = items[0].index;
    for (const it of items) {
      if (focusY <= it.start) { idx = it.index; break; }
      if (it.start <= focusY && focusY < it.start + it.size) { idx = it.index; break; }
      idx = it.index;
    }
    setActiveEntryIndex((prev) => (prev === idx ? prev : idx));
  }, [virtualizer]);

  /** S9b（plan-329-1647）：scrollspy 用 rAF 节流。scroll 事件可在一帧内多次触发，
   *  而 updateActiveEntry 每次都要取虚拟项集合与滚动位置做映射（JumpDots 焦点跟随）；
   *  节流到每帧一次，既保跟手又去掉同一帧内的重复计算。 */
  const spyRafRef = useRef(0);
  const scheduleSpy = useCallback(() => {
    // P1-4（拖拽卡顿治理）：运动期不读布局——updateActiveEntry 每次都要取虚拟项集合与
    // scrollTop/clientHeight；拖拽期这些读会升级为强制同步布局，与折行重排叠加。
    // 运动结束后由面板拖拽结束 / 窗口运动结束路径触发一次 scroll，届时自然补算。
    if (isBusy()) return;
    if (spyRafRef.current) return;
    spyRafRef.current = requestAnimationFrame(() => {
      spyRafRef.current = 0;
      const cur = parentRef.current;
      if (cur) updateActiveEntry(cur);
    });
  }, [updateActiveEntry]);


  /** 滚动中显示滚动条 + scrollspy。跟随判定不在这里（由贴底内核按用户意图维护）。 */
  const onScroll = useCallback(() => {
    const el = parentRef.current;
    if (!el) return;
    el.classList.add("is-scrolling");
    window.clearTimeout(scrollIdleTimerRef.current);
    scrollIdleTimerRef.current = window.setTimeout(() => {
      parentRef.current?.classList.remove("is-scrolling");
    }, 700);
    scheduleSpy();
    scheduleVisibleRefresh();
  }, [scheduleSpy, scheduleVisibleRefresh]);

  // 滚动条自动隐藏的延时器不能在组件卸载后继续回写 DOM。
  useEffect(() => () => {
    window.clearTimeout(scrollIdleTimerRef.current);
    parentRef.current?.classList.remove("is-scrolling", "is-scrollbar-hover");
    // plan-20261024-jumpfix：卸载时收尾跳转校正（取消帧、定时器与用户接管监听）
    jumpCleanupRef.current?.();
  }, []);

  /** v45: 滚动条显现的指针热区 = 消息流右侧的滚动条条带（宽 6px + 少量容差）。
   *  旧实现用 CSS :hover 把整个消息面板当作热区，导致"只要鼠标在面板内就显示滚动条"；
   *  这里改为按指针横坐标判定，只有真正移到滚动条上才显示。 */
  useEffect(() => {
    const el = parentRef.current;
    if (!el) return;
    const SCROLLBAR_BAND = 12; // 6px 滚动条 + 6px 容差，便于命中
    // P0-3（拖拽卡顿治理）：getBoundingClientRect 是强制同步布局读，此前每次 pointermove
    // 都执行一次——与拖拽期逐帧写入的宽度叠加，等于每帧多付一次全量重排。
    // 现在：① 运动期（拖拽 / 窗口缩放）直接跳过——此时指针被拖拽占用，热区无意义；
    // ② 静止期用 rAF 合并，一帧至多读一次。
    let raf = 0;
    let lastX = 0;
    let lastY = 0;
    const apply = () => {
      raf = 0;
      const rect = el.getBoundingClientRect();
      if (lastY < rect.top || lastY > rect.bottom) { el.classList.remove("is-scrollbar-hover"); return; }
      const nearRight = lastX >= rect.right - SCROLLBAR_BAND && lastX <= rect.right + 2;
      el.classList.toggle("is-scrollbar-hover", nearRight);
    };
    const onMove = (e: PointerEvent) => {
      if (isBusy()) return;
      lastX = e.clientX;
      lastY = e.clientY;
      if (raf) return;
      raf = requestAnimationFrame(apply);
    };
    const onLeave = () => {
      if (raf) { cancelAnimationFrame(raf); raf = 0; }
      el.classList.remove("is-scrollbar-hover");
    };
    el.addEventListener("pointermove", onMove);
    el.addEventListener("pointerleave", onLeave);
    return () => {
      if (raf) cancelAnimationFrame(raf);
      el.removeEventListener("pointermove", onMove);
      el.removeEventListener("pointerleave", onLeave);
      el.classList.remove("is-scrollbar-hover");
    };
  }, []);

  /* plan-20261020-pts：原「内容高度 RO + 运动期记账、运动结束补滚 + 按距底距离同步跟随态」整段移除。
     跟随中的贴底由 StickyBottom 在视口 / 内容 / 尾项的尺寸观察里当帧完成（运动期同样贴底：
     ResizeObserver 回调发生在布局之后，此时读 scrollHeight 不会额外触发重排）；
     按距底距离同步跟随态正是「贴底被误判成离开底部」的来源，不再保留。 */

  /** plan-282-1441（#2）：宽度变化 → 锚点补偿（仅不跟随时）。
   *
   *  分工：跟随中由贴底内核在尺寸变化时当帧贴底；本 effect 只负责「**不跟随**时容器宽度变化，
   *  保持用户正在看的那条消息停在原位」（拖拽分隔条 / 折叠展开面板 / 窗口缩放）。
   *
   *  必须在宽度变化**当帧**记录锚点（此时还没重排），否则记录到的偏移已被污染。 */
  useEffect(() => {
    const el = parentRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;

    const capture = () => {
      // plan-20261020-pts：跟随中不记锚点——贴底内核会在每次尺寸变化时当帧贴底，
      // 这里再按锚点还原只会和它抢位置。
      if (sticky.isFollowing()) {
        widthAnchorRef.current = null;
        return;
      }
      const nodes = Array.from(el.querySelectorAll<HTMLElement>("[data-index]"));
      const viewportTop = el.getBoundingClientRect().top + el.clientTop;
      const firstNode = nodes.find((node) => node.getBoundingClientRect().bottom > viewportTop);
      const firstIndex = firstNode ? Number(firstNode.dataset.index) : -1;
      const first = Number.isFinite(firstIndex) && firstIndex >= 0
        ? { index: firstIndex, start: el.scrollTop + firstNode!.getBoundingClientRect().top - viewportTop }
        : null;
      // plan-387-1825 A7：宽度为 0（折叠态）或拿不到虚拟项时几何处于塌缩状态，
      // 只记比例、标记 untrusted，还原时按比例保持，不做逐项定位。
      const max = el.scrollHeight - el.clientHeight;
      if (el.clientWidth === 0 || !first) {
        widthAnchorRef.current = {
          index: -1, offset: 0,
          untrusted: true,
          ratio: max > 0 ? el.scrollTop / max : 0,
        };
        return;
      }
      widthAnchorRef.current = {
        index: first.index,
        // 首条可见项相对视口顶部的偏移：还原时用它把同一条放回原位
        offset: (() => {
          const node = el.querySelector(`[data-index="${first.index}"]`);
          return node instanceof HTMLElement
            ? node.getBoundingClientRect().top - el.getBoundingClientRect().top - el.clientTop
            : first.start - el.scrollTop;
        })(),
        untrusted: false,
        ratio: max > 0 ? el.scrollTop / max : 0,
      };
    };

    /* plan-20261020-pts：还原只负责位置，不再改写跟随状态（原 finishRestore 按还原后的几何
       重算跟随，测量尚未收敛时会把贴底误判成离开底部）。还原途中若用户又开始跟随，立即让位。 */
    const restore = () => {
      const anchor = widthAnchorRef.current;
      const el2 = parentRef.current;
      if (!anchor || !el2 || sticky.isFollowing()) return;
      const max = el2.scrollHeight - el2.clientHeight;
      if (anchor.index >= 0) {
        // ① 粗还原：按虚拟器几何把锚点项放回它原来的视口偏移。
        const offsetTop = virtualizer.getOffsetForIndex(anchor.index)?.[0];
        if (typeof offsetTop === "number") {
          el2.scrollTop = Math.max(0, offsetTop - anchor.offset);
        } else if (max > 0) {
          el2.scrollTop = anchor.ratio * max;
        }
        // ② 精确校正（本轮新增，针对性修「位置偏差非常大」）：
        //    getOffsetForIndex 对**尚未测量**的项用的是 estimateSize（按类型分桶取中位数），
        //    长会话中锚点项之前有大量离屏项时，估算累计误差可达数百像素 —— 粗还原因此偏得很远，
        //    且偏移量随会话长度增长（这正是用户说的"偏差非常大"，而不是几像素的抖动）。
        //    锚点项此刻必然已挂载（上一步刚把它定位进视口），直接量它的真实位置做像素级校正，
        //    等价于用实测值覆盖估算值。虚拟项自带 data-index（见渲染处 data-index={item.index}）。
        const correctAnchor = (remaining: number) => {
          if (sticky.isFollowing()) return;
          const node = el2.querySelector(`[data-index="${anchor.index}"]`);
          if (!(node instanceof HTMLElement) && remaining > 0) {
            virtualizer.scrollToIndex(anchor.index, { align: "start", behavior: "auto" });
            requestAnimationFrame(() => correctAnchor(remaining - 1));
            return;
          }
          if (node instanceof HTMLElement) {
            const actual = node.getBoundingClientRect().top - el2.getBoundingClientRect().top - el2.clientTop;
            const delta = actual - anchor.offset;
            if (Math.abs(delta) > 1) el2.scrollTop += delta;
          }
          if (remaining > 0) requestAnimationFrame(() => correctAnchor(remaining - 1));
        };
        requestAnimationFrame(() => correctAnchor(5));
      } else if (max > 0) {
        el2.scrollTop = anchor.ratio * max;
      }
    };

    lastWidthRef.current = el.clientWidth;
    // plan-31-151 S4：锚点 capture 时机修正——拖拽期间 RO 回调里 capture 时布局已被改写，
    //   捕获到的是污染锚点。改为在 ResizeHandle 的 mousedown（拖拽开始前）派发的
    //   chatcoder:panel-drag-start 事件里捕获（此时布局尚未变）；窗口 resize 路径
    //   仍在 RO 回调第一帧捕获（此时是窗口几何驱动，布局同样尚未变）。
    let pendingRestore = 0;
    let missedDuringMotion = false;
    const inMotion = () => isBusy(); // PerfBus（S3）
    // plan-387-1825 A13：还原时机 = 「容器宽度已落定」，并以 400ms 硬兜底。
    //
    // 演进记录（前两版都不对，这里写明为什么最终这样写）：
    //   · 最初「固定重试 3 次」≈96ms —— 短于折叠/展开的 `--dur-pane`（260ms）CSS 过渡，
    //     restore 读到**半程布局**并随即清空锚点，过渡结束后再无补偿 → 位置错位。
    //   · 上一版「宽度与总高连续 3 帧都不变」—— 虚拟器重排期间总高会持续微调，
    //     判据过严导致长期不满足，被 800ms 兜底强制还原时位置已经漂了。
    //   现在只用一个判据：**宽度连续两帧一致且非 0**。
    //     拖拽 → 宽度瞬时到位，4 帧内即还原；
    //     折叠/展开 → 宽度在 260ms 过渡中持续变化，自然等到过渡走完才还原。
    //   测量缓存的新旧不再影响准确性 —— restore 末尾会用 DOM 实测做像素级校正。
    const RESTORE_MAX_WAIT_MS = 400;
    let lastProbeW = -1;
    let restoreStartedAt = 0;
    const scheduleRestore = () => {
      if (!restoreStartedAt) restoreStartedAt = performance.now();
      if (pendingRestore) cancelAnimationFrame(pendingRestore);
      pendingRestore = requestAnimationFrame(() => {
        pendingRestore = requestAnimationFrame(() => {
          pendingRestore = 0;
          const el2 = parentRef.current;
          if (!el2) {
            widthAnchorRef.current = null;
            missedDuringMotion = false;
            restoreStartedAt = 0;
            lastProbeW = -1;
            return;
          }
          const w = el2.clientWidth;
          const widthSettled = w > 0 && Math.abs(w - lastProbeW) < 1;
          lastProbeW = w;
          const timedOut = performance.now() - restoreStartedAt > RESTORE_MAX_WAIT_MS;
          if (!widthSettled && !timedOut) {
            scheduleRestore(); // 继续等，不消耗锚点
            return;
          }
          restoreStartedAt = 0;
          lastProbeW = -1;
          restore();
          widthAnchorRef.current = null;
          missedDuringMotion = false;
        });
      });
    };
    /** 运动结束后收尾：补一次锚点还原。 */
    const settleAfterMotion = () => {
      if (!missedDuringMotion) return;
      scheduleRestore();
    };
    /* plan-586-2374 §2.2：运动结束补测——把运动期被跳过的屏外项一次性写回真实高度。
     *
     * 为什么不用 virtualizer.measure()：直接全量清缓存会与 directDomUpdates 下的 RO 重测
     * 竞争、导致项重叠（见 reconcile.ts 中 RECONCILE_ORDER.virtualizerMeasure 的沿革注释）。
     * 这里逐项 resizeItem 是幂等写——视口内项的缓存本来已是最新，delta 为 0，不产生任何动作。
     * 先读齐再写：避免 getBoundingClientRect 与 resizeItem 的 scrollTop 补偿交替，把一次
     * 布局读变成 N 次强制同步布局。 */
    const remeasureSkippedItems = () => {
      if (!skippedDuringMotionRef.current) return;
      skippedDuringMotionRef.current = false;
      const pending: Array<{ index: number; height: number }> = [];
      virtualizer.elementsCache.forEach((el) => {
        if (!el.isConnected) return;
        const index = virtualizer.indexFromElement(el);
        if (index < 0) return;
        const height = Math.round(el.getBoundingClientRect().height);
        if (!Number.isFinite(height) || height <= 0) return;
        pending.push({ index, height });
      });
      if (pending.length === 0) return;
      for (const p of pending) virtualizer.resizeItem(p.index, p.height);
      // 高度已变 → 可见范围与贴底同步跟上，避免收敛后露出半帧偏差
      refreshVisibleIndexes();
      sticky.pin();
    };
    // plan-31-151 S4：锚点收尾路径去重——删除 pointerup 捕获监听与 panel-drag-end 事件，
    //   只保留 reconcile order 30 一条路径；窗口 resize 路径仍走 chatcoder:window-motion。
    const offRect = registerReconcileTask(`mf-virtualizer-rect:${instanceId}`, RECONCILE_ORDER.virtualizerRect,
      "提交虚拟器视口尺寸", () => settleRectRef.current?.());
    const offAnchor = registerReconcileTask(`mf-scroll-anchor:${instanceId}`, RECONCILE_ORDER.scrollAnchor,
      "还原滚动锚点", () => settleAfterMotion());
    /* plan-586-2374 §2.2：运动期跳过的项在收敛序列里补测（order 40 在 rect / 锚点之后，
       此时宽度已落定，量到的是终态高度）。没发生过跳过时整个任务是一次布尔判断。 */
    const offRemeasure = registerReconcileTask(`mf-virtualizer-remeasure:${instanceId}`,
      RECONCILE_ORDER.virtualizerMeasure, "补测运动期跳过的项", () => remeasureSkippedItems());
    const onMotionEnd = (e: Event) => {
      const active = (e as CustomEvent<{ active?: boolean }>).detail?.active;
      if (active === false) {
        // 窗口缩放路径不经过 runReconcile（App.tsx 只广播事件），此处补上同一份补测。
        remeasureSkippedItems();
        settleAfterMotion();
        return;
      }
      // plan-586-2374：窗口缩放（IPC window:motion active=true）不派发 panel-drag-start /
      // pane-change-start，白名单需要在这里单独重建。主进程在改窗口几何**之前**同步发来该事件，
      // 此时读到的 scrollTop / clientHeight 还是运动前的干净几何。
      //
      // 注意**不要**在这里清 `skippedDuringMotionRef`：标记归属「单次运动」，而本事件是
      // 窗口几何运动源的开始信号。若用户正拖分隔条（panel-drag 已置 true）期间窗口又收到
      // resize/move，无条件清零会让拖拽收尾的补测变成 no-op，屏外项停在旧高度。
      // 各运动源在**自己的开始处**归零（onDragStart / onPaneChangeStart / 本分支的前一次收尾）。
      refreshVisibleIndexes();
    };
    window.addEventListener("chatcoder:window-motion", onMotionEnd);
    // plan-31-151 S4：拖拽开始前捕获锚点（ResizeHandle mousedown 派发，此时布局尚未变）。
    const onDragStart = () => {
      if (!inMotion()) return; // 非拖拽路径不捕获（窗口 resize 走 RO 第一帧）
      lastWidthRef.current = el.clientWidth;
      capture();
      missedDuringMotion = true;
      // plan-586-2374：运动开始 = 布局尚未改写，此刻刷新的「视口 ± 1 屏」最干净；
      // 运动期不再刷新（避免读布局），整个 260ms 门控都用这份集合。
      refreshVisibleIndexes();
      skippedDuringMotionRef.current = false;
    };
    window.addEventListener("chatcoder:panel-drag-start", onDragStart);
    // plan-387-1825 B1：折叠/展开按钮的几何变更。applyPaneUpdate 在 acquire("panel-anim")
    // 之后、提交布局之前派发本事件 —— 与分隔条 mousedown 同一时机语义（布局尚未改写），
    // 捕获到的锚点是干净的。此处**不能**沿用 onDragStart 的 inMotion 守卫的早退逻辑：
    // 折叠时 isBusy() 恰为真（panel-anim 已登记），但事件本身正是我们要的触发点。
    const onPaneChangeStart = () => {
      lastWidthRef.current = el.clientWidth;
      capture();
      missedDuringMotion = true;
      refreshVisibleIndexes();
      skippedDuringMotionRef.current = false;
    };
    window.addEventListener("chatcoder:pane-change-start", onPaneChangeStart);
    const ro = new ResizeObserver(() => {
      const el2 = parentRef.current;
      if (!el2) return;
      // 运动期：锚点已在 drag-start 事件里捕获（拖拽路径），或在 RO 第一帧捕获（窗口 resize）。
      //   这里只记账，不再重复 capture（避免在已改写的布局上捕获污染锚点）。
      if (inMotion()) {
        missedDuringMotion = true;
        return;
      }
      const w = el2.clientWidth;
      if (Math.abs(w - lastWidthRef.current) < 1) return; // 高度抖动不参与
      lastWidthRef.current = w;
      // 运动期钉住了内容宽度，松手解冻后这次 RO 仍应使用运动前的锚点，
      //   不能在内容折行后的新布局上重新 capture（否则就是把漂移后的偏移当作目标）。
      if (missedDuringMotion) {
        scheduleRestore();
        return;
      }
      // 非运动路径（窗口 resize 的第一帧 RO）：此时布局尚未变，可安全捕获。
      capture();
      scheduleRestore();
    });
    ro.observe(el);
    return () => {
      offRect(); offAnchor(); offRemeasure();
      if (pendingRestore) cancelAnimationFrame(pendingRestore);
      window.removeEventListener("chatcoder:window-motion", onMotionEnd);
      window.removeEventListener("chatcoder:panel-drag-start", onDragStart);
      window.removeEventListener("chatcoder:pane-change-start", onPaneChangeStart);
      ro.disconnect();
    };
  }, [virtualizer, instanceId, sticky, refreshVisibleIndexes]);

  /** 已做过"会话首次填充贴底"的会话标识（切换会话时重新进入首次填充分支） */
  const initSessionKeyRef = useRef<string | number | null>(null);
  /** plan-282-1444：首次定位的收敛轮询句柄（切会话/卸载时取消） */
  const settleRafRef = useRef(0);
  useLayoutEffect(() => {
    // plan-282-1434（B6）：首次填充是"点击会话"路径 —— 必须**直接呈现底部**，
    // 不能出现"先顶部、再滚下去"的过程：定位收敛前用 .is-positioning 隐藏（见下方揭示逻辑）。
    //
    // plan-20261020-pts：首屏定位只用 scrollTop = scrollHeight 收敛（经贴底内核），
    // 不再逐帧调用 scrollToIndex(末项, end)——它只对齐到末项底边、不含滚动区留白，
    // 还会在虚拟列表内部定时重试，与贴底来回拉扯；首次进入后变更卡片等异步内容撑高时
    // 由内核的尺寸观察继续贴底，而不是只在这 400ms 窗口内有效。
    if (totalCount === 0) return;
    if (initSessionKeyRef.current !== sessionKey) {
      initSessionKeyRef.current = sessionKey;
      setPositioned(false); // 进入新会话：先隐藏，待定位完成
      sticky.stick();

      /** plan-282-1444：揭示时机按"测量收敛"：总高连续 2 帧不变才显示；
       *  10 帧 / 400ms 强制揭示兜底，不允许内容被永久隐藏。 */
      cancelAnimationFrame(settleRafRef.current);
      const startedAt = performance.now();
      let lastTotal = -1;
      let stableFrames = 0;
      let frames = 0;
      const settle = () => {
        const total = virtualizer.getTotalSize();
        stableFrames = total === lastTotal ? stableFrames + 1 : 0;
        lastTotal = total;
        frames += 1;
        // 用户在这段窗口内主动上滑：立即停止定位并揭示，不与他抢滚动
        if (!sticky.isFollowing()) {
          settleRafRef.current = 0;
          setPositioned(true);
          return;
        }
        sticky.pin(); // 收敛过程中持续贴底：测量把高度撑开时不能停在中间
        const settled = stableFrames >= 2 && frames >= 2;
        if (settled || frames >= 10 || performance.now() - startedAt > 400) {
          settleRafRef.current = 0;
          setPositioned(true);
          return;
        }
        settleRafRef.current = requestAnimationFrame(settle);
      };
      settleRafRef.current = requestAnimationFrame(settle);
      return;
    }
    // 条目数变化：跟随中在本次提交、绘制之前贴底（尺寸观察是下一拍的兜底）
    sticky.pin();
  }, [sessionKey, totalCount, virtualizer, sticky]);

  /** plan-20261020-pts：尾项交给贴底内核观察——流式输出与异步卡片都在最后一条里长高，
   *  直接观察它可在同一帧完成贴底（只观察内容容器要晚一帧，新行会先露出半截再跳）。 */
  useLayoutEffect(() => {
    sticky.setTail(innerRef.current?.lastElementChild ?? null);
  });

  // plan-282-1444：切会话/卸载时取消首次定位轮询，避免对已换掉的内容继续补滚
  useEffect(() => () => { cancelAnimationFrame(settleRafRef.current); settleRafRef.current = 0; }, []);

  // 兜底：极端情况下（如首次填充未触发）不允许内容永久隐藏
  useEffect(() => {
    if (positioned) return;
    const t = window.setTimeout(() => setPositioned(true), 400);
    return () => window.clearTimeout(t);
  }, [positioned]);

  // v0.3.1: 对话启动（running 由 false -> true）时强制滚到底部并恢复跟随
  const prevRunningRef = useRef(running);
  useEffect(() => {
    if (running && !prevRunningRef.current) sticky.stick();
    prevRunningRef.current = running;
  }, [running, sticky]);

  // 用户发送消息（时间线末尾新增"以用户消息开头"的新 turn）→ 无条件滚底并恢复跟随
  const prevEntryLenRef = useRef(entries.length);
  useEffect(() => {
    const grew = entries.length > prevEntryLenRef.current;
    prevEntryLenRef.current = entries.length;
    if (!grew) return;
    const last = entries[entries.length - 1];
    const lastStartsUser = last != null && last.kind === "turn"
      && last.items.length > 0 && last.items[0].kind === "user";
    if (lastStartsUser) sticky.stick();
  }, [entries, sticky]);

  /* plan-20261020-pts：原「订阅流式缓冲长度 → 补滚」已移除。流式文本长在尾项里，
     由贴底内核对尾项的尺寸观察当帧贴底；在订阅回调里读写滚动位置反而发生在
     React 渲染新文本之前，读到的是旧高度。 */

  // v0.3.1: 内容整表替换（refreshMessages 等 REST 刷新/回滚/压缩）后同样贴底——
  // 依赖 entries 引用而非 length：REST 刷新常使 entries 重建但长度不变。不跟随时不打扰。
  useEffect(() => {
    sticky.pin();
  }, [entries, sticky]);

  // 本轮修复：强制贴底信号（"立即发送"后用户消息固定在时间线内，entries.length 不变，
  // 故由宿主下发信号保证"发送即滚到最新"）。
  const prevForceBottomRef = useRef(forceBottomKey);
  useEffect(() => {
    if (forceBottomKey === prevForceBottomRef.current) return;
    prevForceBottomRef.current = forceBottomKey;
    sticky.stick();
  }, [forceBottomKey, sticky]);

  /** 问题12: 内容/滚动变化后刷新 scrollspy 焦点，保证 JumpDots 自动跟随 */
  useEffect(() => {
    const el = parentRef.current;
    if (el) updateActiveEntry(el);
    sampleHeights();
    // plan-586-2374：条目集合变了，门控白名单随之失效，就地重建一次（低频）。
    refreshVisibleIndexes();
  }, [entries.length, updateActiveEntry, sampleHeights, refreshVisibleIndexes]);

  useEffect(() => {
    if (!positioned) return;
    sampleHeights();
    // plan-586-2374：首屏定位收敛后可见范围已稳定，重建门控白名单。
    refreshVisibleIndexes();
  }, [positioned, sampleHeights, refreshVisibleIndexes]);

  const matchedIndices = useMemo(() => {
    const kw = searchKeyword.trim().toLowerCase();
    if (!kw) return [];
    const result: number[] = [];
    entries.forEach((e, idx) => {
      if (e.kind === "turn") {
        const hit = e.items.some((it) => {
          if (it.kind === "tools") return it.nodes.some((n) => nodeToolText(n).toLowerCase().includes(kw));
          return itemText(it).toLowerCase().includes(kw);
        });
        if (hit) result.push(idx);
      } else {
        if (msgText(e.msg.content).toLowerCase().includes(kw)) result.push(idx);
      }
    });
    return result;
  }, [entries, searchKeyword]);

  /** 搜索面板开启且有关键字时才对渲染层做命中标记（否则零开销） */
  const isSearching = searchOpen && searchKeyword.trim().length > 0;

  /** plan-156-619 S2：搜索入口按钮是否显示（默认关闭，可在「设置 → 常规」打开）。
   *  只隐藏**入口按钮**：Ctrl+F 仍然可用——关闭一个可视化入口不应连带夺走搜索能力。 */
  const searchEntryVisible = useUiStore((s) => s.showMessageSearch);

  /** plan-156-619 S2：Ctrl+F / Cmd+F 唤起搜索条。
   *  此前按钮 title 写着「Ctrl+F」但全项目没有绑定该快捷键（点按钮才能开），
   *  入口一旦按开关隐藏就彻底没有搜索入口，因此这里把快捷键补实。
   *  焦点在输入框里（发消息后焦点一直在输入框）也要生效：Electron 没有挂原生查找，
   *  Ctrl+F 在输入框内没有编辑含义，不能因为焦点在输入框就失效。 */
  useEffect(() => {
    if (!search) return;
    const onKey = (e: KeyboardEvent) => {
      if (!(e.ctrlKey || e.metaKey) || e.altKey) return;
      if (e.key !== "f" && e.key !== "F") return;
      e.preventDefault();
      setSearchOpen(true);
      // 搜索条已展开时输入框已挂载：直接交出焦点并全选；未展开时由输入框的 autoFocus 完成
      searchInputRef.current?.focus();
      searchInputRef.current?.select();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [search]);

  /** plan-20261020-pts：主动跳到某条消息（搜索命中 / JumpDots / 任务卡定位）是用户意图，
   *  先解除跟随——否则下一次内容增长会把视图拉回底部，跳转白做。
   *
   *  plan-20261024-jumpfix：落点做像素级校正。scrollToIndex 的目标偏移来自尺寸缓存，
   *  目标之前未测量项按 estimateSize 累计，长会话里估算误差可达数百像素，会把落点向下
   *  推过目标 turn（现象：点倒数第二个刻度，视图落到倒数第一条用户消息处）。
   *  因此平滑滚动停稳后直接量目标项真实 DOM 位置收敛（与宽度还原的 correctAnchor 同思路）；
   *  收敛期间把 scrollspy 焦点锁定到目标项，用户一滚动即交还。 */
  const jumpToIndex = useCallback((index: number, align: "start" | "center") => {
    if (index < 0) return;
    sticky.release();

    // 上一轮跳转收尾（重入：连点两个刻度 / 搜索连跳）——动画与校正一并在 cleanup 里取消
    jumpCleanupRef.current?.();
    const cleanup = () => {
      spyLockRef.current = null;
      jumpTokenRef.current += 1; // 仍在排队的动画 / 校正帧按 token 失效
      if (jumpRafRef.current) {
        cancelAnimationFrame(jumpRafRef.current);
        jumpRafRef.current = 0;
      }
      window.clearTimeout(jumpTtlRef.current);
      jumpTtlRef.current = 0;
      window.removeEventListener("wheel", cleanup, true);
      window.removeEventListener("pointerdown", cleanup, true);
      window.removeEventListener("touchstart", cleanup, true);
      window.removeEventListener("keydown", cleanup, true);
      if (jumpCleanupRef.current === cleanup) jumpCleanupRef.current = null;
      // 锁定期间 scrollspy 不重算：解锁后刷新一次，避免高亮停在旧值
      scheduleSpy();
    };
    jumpCleanupRef.current = cleanup;
    // 用户接管判定：捕获阶段监听，任何滚轮/按下/触摸/按键都视为接管，立即交还控制
    window.addEventListener("wheel", cleanup, true);
    window.addEventListener("pointerdown", cleanup, true);
    window.addEventListener("touchstart", cleanup, true);
    window.addEventListener("keydown", cleanup, true);
    jumpTtlRef.current = window.setTimeout(cleanup, JUMP_LOCK_TTL_MS);
    spyLockRef.current = index;
    setActiveEntryIndex(index);

    const token = jumpTokenRef.current;
    /** 落点守护截止时间：收敛后到此时间前持续盯住目标位置（防贴底/测量补偿推走） */
    const holdUntil = performance.now() + JUMP_HOLD_MS;
    let lastTop = -1;
    let stableFrames = 0;
    let fixRounds = 0;
    let mountRetries = 0;
    let elapsed = 0;
    /** 落点校正：等滚动停稳后按目标项真实 DOM 位置收敛（plan-20261024-jumpfix 原逻辑）。
     *  plan-549-2281（第13项）：它只在缓动动画**结束之后**才启动——两者都要写 scrollTop，
     *  同时写会互相覆盖，落点就又回到「估算位置」了。 */
    const correct = () => {
      if (token !== jumpTokenRef.current) return; // 新一轮跳转已接管
      if (spyLockRef.current === null || sticky.isFollowing()) { cleanup(); return; } // 用户接管 / 已恢复跟随
      const el = parentRef.current;
      if (!el) return;
      const node = el.querySelector<HTMLElement>(`[data-index="${index}"]`);
      if (!(node instanceof HTMLElement)) {
        // 目标项未挂载（还在虚拟窗外）：按缓存几何拉进视口，下帧再量（同 correctAnchor 的重试）
        if (mountRetries >= 4) { cleanup(); return; }
        mountRetries++;
        virtualizer.scrollToIndex(index, { align, behavior: "auto" });
        lastTop = -1;
        stableFrames = 0;
        elapsed = 0;
        jumpRafRef.current = requestAnimationFrame(correct);
        return;
      }
      elapsed++;
      if (el.scrollTop === lastTop) stableFrames++;
      else { stableFrames = 0; lastTop = el.scrollTop; }
      // 滚动还没停稳（测量补偿正在写 / 动画尾部）时先别校正：等连续两帧不变；
      // 约 750ms 仍未稳则直接校正兜底（写进去被覆盖会在下一轮再收敛）
      if (stableFrames < 2 && elapsed <= 45) {
        jumpRafRef.current = requestAnimationFrame(correct);
        return;
      }
      const cur = node.getBoundingClientRect().top - el.getBoundingClientRect().top - el.clientTop;
      const want = align === "start" ? 0 : Math.max(0, (el.clientHeight - node.offsetHeight) / 2);
      const delta = cur - want;
      if (Math.abs(delta) <= 2) {
        // 已对齐：守护窗口内继续盯（不写，只观察），到期交还控制
        if (performance.now() < holdUntil) {
          lastTop = el.scrollTop;
          jumpRafRef.current = requestAnimationFrame(correct);
          return;
        }
        cleanup();
        return;
      }
      // 守护期内的反复推走也要拉回，但给个上限避免与外部机制无休止互搏
      if (fixRounds >= 12) { cleanup(); return; }
      fixRounds++;
      const before = el.scrollTop;
      el.scrollTop = before + delta;
      // 写不进去 = 被 maxScroll / 顶部钳制：目标已尽可能对齐（钳制语义下完整可见），接受现状
      if (el.scrollTop === before) { cleanup(); return; }
      lastTop = -1;
      stableFrames = 0;
      jumpRafRef.current = requestAnimationFrame(correct);
    };

    /* plan-549-2281（第13项）：先缓动、再校正。
     * 目标偏移取自虚拟器尺寸缓存（getOffsetForIndex），只用来决定「往哪滚、滚多远」——
     * 它本来就不精确，精确落点交给动画结束后的 correct 收敛。
     * 近距离（一次微调）与超远距离（途中跨越大量未测量项，长动画只会更晃）直接瞬时到位，
     * 保持改动前的干脆手感。 */
    const el = parentRef.current;
    const target = virtualizer.getOffsetForIndex(index, align);
    const from = el ? el.scrollTop : 0;
    const to = target ? target[0] : null;
    const dist = to == null ? 0 : Math.abs(to - from);
    if (!el || to == null || dist < JUMP_ANIM_MIN_PX || dist > JUMP_ANIM_MAX_DIST_PX) {
      virtualizer.scrollToIndex(index, { align, behavior: "auto" });
      jumpRafRef.current = requestAnimationFrame(correct);
      return;
    }

    // 动画期间逐帧直接写 scrollTop，先按 scrollToFn 同口径把内容撑到总高，
    // 否则靠近底部时会被旧高度钳制，动画中途「粘住」再跳一下。
    const inner = innerRef.current;
    if (inner) inner.style.height = `${virtualizer.getTotalSize()}px`;
    const duration = Math.min(JUMP_ANIM_MAX_MS, JUMP_ANIM_BASE_MS + dist * 0.12);
    const startedAt = performance.now();
    const animate = (now: number) => {
      if (token !== jumpTokenRef.current) return; // 新一轮跳转 / cleanup 已接管
      if (spyLockRef.current === null || sticky.isFollowing()) { cleanup(); return; }
      const box = parentRef.current;
      if (!box) { cleanup(); return; }
      const p = Math.min(1, (now - startedAt) / duration);
      box.scrollTop = from + (to - from) * jumpEase(p);
      if (p < 1) {
        jumpRafRef.current = requestAnimationFrame(animate);
        return;
      }
      jumpRafRef.current = requestAnimationFrame(correct);
    };
    jumpRafRef.current = requestAnimationFrame(animate);
  }, [sticky, virtualizer, scheduleSpy]);

  useEffect(() => {
    setActiveMatchIndex(0);
    if (matchedIndices.length > 0) jumpToIndex(matchedIndices[0], "center");
  }, [matchedIndices, jumpToIndex]);

  const jumpMatch = (dir: 1 | -1) => {
    if (matchedIndices.length === 0) return;
    const next = (activeMatchIndex + dir + matchedIndices.length) % matchedIndices.length;
    setActiveMatchIndex(next);
    jumpToIndex(matchedIndices[next], "center");
  };

  useEffect(() => {
    if (!scrollTarget) return;
    /* plan-549-2281（第7项）：定位链路此前是断的——
     *  ① 任务摘要面板写的是 { threadId }（点「定位到执行消息」传子代理 id），
     *     而这里只处理 turnId，threadId 分支从来没实现；
     *  ② 全库也没有任何地方把 scrollTarget 传进 MessageFlow（插件只注册 props={source:"main"}）。
     * 现在：threadId → turn 的解析在主消息流层完成（那里才有 subagentMeta），
     * 内核算通用，只认 turnId；但仍要**解析不到也清理**——否则目标一直挂在 store 里，
     * 下次进会话还会莫名跳一次。 */
    if (scrollTarget.turnId == null) {
      clearScrollTarget?.();
      return;
    }
    const idx = entries.findIndex((e) => e.kind === "turn" && (e.turnId === scrollTarget.turnId || (e.turnIds?.includes(scrollTarget.turnId!) ?? false)));
    if (idx >= 0) jumpToIndex(idx, "start");
    clearScrollTarget?.();
  }, [scrollTarget, entries, jumpToIndex, clearScrollTarget]);

  return (
    <div className={`message-flow-outer ${className || ""}`}>
      {/* plan-156-619 S2：入口按钮受「设置 → 常规 → 消息搜索按钮」控制；
          搜索条一旦打开则始终显示（否则 Ctrl+F 唤起后看不到输入框）。 */}
      {search && (searchOpen || searchEntryVisible) && (
        <div className="flow-search-toggle">
          {!searchOpen ? (
            <button
              type="button"
              className="flow-search-btn"
              onClick={() => setSearchOpen(true)}
              title="搜索消息 (Ctrl+F)"
              aria-label="搜索消息"
            >
              <IconSearch size={14} />
            </button>
          ) : (
            <div className="flow-search-bar">
              <IconSearch size={13} />
              <input
                ref={searchInputRef}
                autoFocus
                value={searchKeyword}
                onChange={(e) => setSearchKeyword(e.target.value)}
                placeholder="搜索消息内容…"
              />
              {matchedIndices.length > 0 && (
                <span className="flow-search-count">
                  {activeMatchIndex + 1}/{matchedIndices.length}
                </span>
              )}
              <button type="button" onClick={() => jumpMatch(-1)} disabled={matchedIndices.length === 0} title="上一个">
                <IconChevronUp size={12} />
              </button>
              <button type="button" onClick={() => jumpMatch(1)} disabled={matchedIndices.length === 0} title="下一个">
                <IconChevronDown size={12} />
              </button>
              <button
                type="button"
                onClick={() => {
                  setSearchOpen(false);
                  setSearchKeyword("");
                }}
                title="关闭搜索"
              >
                <IconX size={12} />
              </button>
            </div>
          )}
        </div>
      )}

      {jumpDots && <JumpDots entries={entries} activeIndex={activeEntryIndex} onJump={(entry) => jumpToIndex(entries.indexOf(entry), "start")} />}

      {/* plan-282-1434（B6）：首次填充贴底完成前隐藏内容，避免看到"从顶部滚下来"的过程 */}
      <div
        ref={parentRef}
        className={"message-flow" + (positioned || totalCount === 0 ? "" : " is-positioning")}
        onScroll={onScroll}
      >
        {totalCount === 0 ? (
          <Empty size="lg" title={emptyText} />
        ) : (
          // plan-282-1444：高度改由 `virtualizer.containerRef` 直接写（directDomUpdates），
          // 故 style 里**不得**再声明 height——否则两处写同一属性会互相抖动。
          <div
            ref={setInnerRef}
            className="message-flow-virtual-inner"
            style={{ position: "relative", width: "100%" }}
          >
            {virtualizer.getVirtualItems().map((item) => {
              // 槽位顺序：已落库 entries -> 流式段 -> 尾部卡片
              // （注入的用户消息已回归时间线，不再占用独立槽位——见 MessageFlowCoreProps.forceBottomKey）
              const streamStart = entries.length;
              const trailingStart = streamStart + (hasStreaming ? 1 : 0);
              const isStreamSlot = hasStreaming && item.index === streamStart;
              const isTrailingSlot = hasTrailing && item.index === trailingStart;
              let node: ReactNode;
              if (isStreamSlot) {
                node = streamingNode;
              } else if (isTrailingSlot) {
                node = trailingNode;
              } else {
                const entry = entries[item.index];
                node = entry ? renderEntry(entry, item.index) : null;
              }
              return (
                <div
                  key={item.key}
                  data-index={item.index}
                  ref={virtualizer.measureElement}
                  className="message-flow-virtual-item"
                  // plan-282-1444：主轴向位置（transform/top）由虚拟列表直接写 DOM，
                  // 此处只保留 top/left 锚点与宽度——若再声明 transform，
                  // 会与虚拟列表写入的值打架，重叠回归。
                  style={{
                    position: "absolute",
                    top: 0,
                    left: 0,
                    width: "100%",
                  }}
                >
                  {/* plan-282-1421（第11项）：搜索命中高亮（包裹层做文本节点标记，
                      不侵入 Markdown 结构；当前命中项用更强的 is-active 配色）。 */}
                  <HighlightedItem
                    keyword={searchKeyword}
                    active={isSearching && item.index === matchedIndices[activeMatchIndex]}
                  >
                    {node}
                  </HighlightedItem>
                </div>
              );
            })}
          </div>
        )}
        {/* plan-156-619 S7：输入框上方的悬浮胶囊已下线（改由右上角综合工具卡承载），
            这里原先的 `.message-flow-capsule-space` 占位块一并移除——
            没有胶囊就不需要为它预留高度过渡的安全区。 */}
      </div>

      {showScrollBottom && (
        <button
          type="button"
          className="flow-scroll-bottom-btn"
          onClick={() => sticky.stick(true)}
          title="回到底部"
          aria-label="回到底部"
        >
          <IconArrowDown size={14} />
        </button>
      )}
    </div>
  );
}

/** 主会话数据源（source="main"）：全局 store 驱动，完整功能。 */
function MainMessageFlow({
  actions,
  scrollTarget,
  clearScrollTarget,
  className,
  emptyText,
  suggestionCard,
}: {
  actions?: "full" | "copy-only" | "none";
  scrollTarget?: { threadId?: number; turnId?: number } | null;
  clearScrollTarget?: () => void;
  className?: string;
  emptyText?: string;
  /** plan-156-621：false 时不渲染主会话的后续任务建议卡（侧边对话场景） */
  suggestionCard?: boolean;
}) {
  const getScoped = useScopedGetter();
  const messages = useScoped("messages");
  const turns = useScoped("turns");
  const isRunning = useScoped("isRunning");
  const runningTurnId = useScoped("runningTurnId");
  // v39: 后台子代理运行数——主会话等待子代理时显示「等待子代理结束…」
  const pendingSubagents = useScoped("pendingSubagents");
  const currentSessionId = useScoped("currentSessionId");
  /* plan-20261020-suggestion-gate：会话首次加载期间消息还没渲染出来。
   * 建议卡属于「已收口的一轮」，先于消息出现会让人以为任务只剩这一步，因此等消息落地后再展示。
   * 侧边对话（ConvoScopeProvider）不消费主仓库的加载态——它本身也关闭了建议卡。 */
  const sessionLoading = useChatStore((s) => s.sessionLoading);
  const messagesPending = useContext(ConvoScopeContext) == null && sessionLoading;
  // 主代理未在跑、但后台工作流在执行（含工作流结束后等待唤醒主代理的空档）：消息流下方显示「等待响应…」（工作流才是此刻的执行者）
  const workflowBusy = useWorkflowStore((s) => selectSessionWorkflowBusy(s, currentSessionId));
  // 本会话已启动的工作流里最晚的结束时刻（完成 / 取消 / 暂停）：整轮「已工作」用时的终点下限，见 TurnGroup
  const backgroundEndedAt = useWorkflowStore((s) => (currentSessionId == null ? 0 : latestWorkflowEndMs(s.bySession[currentSessionId])));
  const subagentMeta = useScoped("subagentMeta");
  // S8（FlowEngine）：**不再**订阅 streamingBuffers / thinkingBuffers。
  // 缓冲每帧换新引用，此前会让整棵消息流每帧重渲染（运行期“更卡”的头号来源）。
  // 现在订阅下沉到 <StreamingTail>（只重渲染那一个组件），主树与 delta 解耦。
  // plan-95: 计划卡归属 turn——卡片内嵌到该 turn 行尾随时间线滚动，
  // 不再固定在消息流最底部（与后续新消息脱节）
  const planTurnId = useScoped("pendingPlan")?.turnId ?? null;
  // v30: 压缩中状态（compact.started 载荷）——v42: 不再在消息流尾部渲染「正在压缩上下文」
  // 统计卡片（用户反馈与状态行重复、样式突兀），改为把流式状态行文案从「处理中…」切换为「压缩中…」。
  const isCompacting = useScoped("isCompacting");
  // plan-282-1441（#8）：调试现场——AI 调试试过程中在消息流尾部展示“停在哪一行”。
  // 这是“用户能看到断点进行到哪一行代码”在消息流侧的落点（另一处在调试面板）。
  const debugState = useScoped("debugState");
  const activeDebug = useMemo(() => {
    for (const t of ["web", "java"] as const) {
      const st = debugState?.[t];
      if (st?.connected) return st;
    }
    return null;
  }, [debugState]);

  const subagentsByTurn = useMemo(() => {
    // plan-330-1648 M7: 透传 error——卡片/面板据此展示失败原因（不再只有一个红叉）
    // plan-20261013-wf-order: 透传 turnId / programId / spawnMessageId——工作流节点卡据此定位到其创建位置
    const map = new Map<
      number,
      Array<{
        agentId: number;
        name: string;
        status: string;
        error?: string | null;
        turnId: number;
        programId: number | null;
        spawnMessageId: number | null;
      }>
    >();
    for (const [aid, m] of Object.entries(subagentMeta)) {
      if (m.turnId == null) continue;
      const list = map.get(m.turnId) ?? [];
      list.push({
        agentId: Number(aid),
        name: m.name,
        status: m.status,
        error: m.error ?? null,
        turnId: m.turnId,
        programId: m.programId ?? m.workflow?.program_id ?? null,
        spawnMessageId: m.spawnMessageId ?? null,
      });
      map.set(m.turnId, list);
    }
    return map;
  }, [subagentMeta]);

  /** 一轮（含并入的续跑轮）内的子代理。续跑轮派发的子代理归属续跑轮的 turn，合并轮须并集各轮，
   *  否则这些子代理卡不会出现在任何位置（plan-20261013-wf-order）。单轮保持原数组引用，不影响 memo。
   *
   *  plan-586-2374：合并轮的并集结果按「轮 id 组合」缓存。原先每次调用都 `flatMap` 出新数组，
   *  而它作为 `subagents` prop 传给 TurnGroup → `unplacedSubagents` / `placeWorkflowNodeCards`
   *  / 三次切段的依赖全被推翻、每渲染重算，正好抵消本轮把 placeWorkflowNodeCards 变为
   *  O(cards × items) 缓存的收益（合并轮在续跑会话里很常见）。 */
  const mergedRoundCache = useRef(new Map<string, NonNullable<ReturnType<typeof subagentsByTurn.get>>>());
  const subagentsOfRound = useCallback(
    (entry: Extract<TimelineEntry, { kind: "turn" }>) => {
      const ids = entry.turnIds ?? (entry.turnId != null ? [entry.turnId] : []);
      if (ids.length === 0) return undefined;
      if (ids.length === 1) return subagentsByTurn.get(ids[0]);
      const cacheKey = ids.join(",");
      const cache = mergedRoundCache.current;
      const hit = cache.get(cacheKey);
      if (hit) return hit.length > 0 ? hit : undefined;
      const merged = ids.flatMap((id) => subagentsByTurn.get(id) ?? []);
      cache.set(cacheKey, merged);
      return merged.length > 0 ? merged : undefined;
    },
    [subagentsByTurn],
  );
  // 子代理状态变化 → 合并轮缓存失效（否则卡片状态会停在旧快照）
  useEffect(() => { mergedRoundCache.current.clear(); }, [subagentsByTurn]);

  // plan-41-227: 时间线直接用落库消息本体（按 id 序），位置只由 id 决定。
  //
  // 历史实现（v41/v42）在注入消息（"立即发送"）落库时快照"当时正在流式的 agent"，
  // 等该段落库后把它前移到注入消息上方，充当"注入前 / 注入后"的时间分割点。
  // 实测该前移会让位置来回跳：段落库瞬间从流式槽位（注入消息下方）跳到注入消息上方，
  // turn 结束后标记清空又落回 id 序（用户反馈"位置不按时间线，任务完成后才回到该在的位置"）。
  // 现在注入消息落库即按 id 序固定——后端广播已带 turn_id，直接归入所属 turn，不再二次搬移。

  // v30: 被压缩的消息保留在时间线上（不隐藏）；压缩块卡由 SUMMARY 消息渲染。
  // S8b：改用增量构建器——单条消息落库只重建它所在的那个 turn，其余 turn 的 entry
  // 对象逐项复用（引用不变）⇒ 下游 memo 的 TurnGroup 继续命中，不再全量重渲染。
  const buildEntries = useMemo(() => createTimelineBuilder(), []);
  // 续跑轮（工作流 / 子代理唤醒后的回复）并入前一轮：一轮只有一个计时条、一排操作按钮（plan-282-1530 阶段 D）
  const mergeRounds = useMemo(() => createRoundMerger(), []);
  // plan-20261016-ux（问题 8）：空会话 / 连续切换时的「模型已切换」分隔线不展示（见 pruneModelDividers）
  const entries = useMemo(() => mergeRounds(buildEntries(pruneModelDividers(messages))), [mergeRounds, buildEntries, messages]);

  /** 强制贴底信号：用户消息条数。
   *
   *  为什么需要：注入消息落在**已存在的** turn entry 内部，entries.length 与末尾 entry
   *  形态都不变 ⇒ MessageFlowCore 里"末尾新增以用户消息开头的新 turn → 滚底"的判定不会
   *  触发，点了「立即发送」或发新消息后视图不会跟到底。
   *  这里用"用户消息条数"作信号：每新增一条用户消息（含注入与乐观占位）即 +1，
   *  核心层据此无条件贴底——语义精确且不会因流式内容变化而误触发。 */
  const userMsgSeq = useMemo(
    () => messages.reduce((n, m) => (m.sender_type === "user" ? n + 1 : n), 0),
    [messages],
  );

  const plansByTurn = useScoped("plansByTurn");

  // plan-75-334 阶段1：在父层一次性计算全局派生值，避免每个 TurnGroup 重复 selector
  // 1. 最近用户消息 ID（从尾部扫描，每个 TurnGroup 都需要判断是否为最新）
  const latestUserId = useMemo(() => {
    for (let k = messages.length - 1; k >= 0; k--) {
      if (messages[k].sender_type === "user") return messages[k].id;
    }
    return -1;
  }, [messages]);

  // 2. 流错误相关状态（每个 TurnGroup 都需要判断是否需要隐藏错误消息）
  const flowErrorTurnId = useScoped("flowError")?.turnId ?? null;
  const hiddenTurnErrors = useScoped("hiddenTurnErrors");

  // plan-20261014-actions-wait：后台（工作流 / 子代理）仍在跑时，消息流尾部那一轮不算结束——
  // 主代理即使已收工（turn 行状态为 completed），后台跑完后服务端还会唤醒续跑并并入该轮。
  // 口径与输入框 busy、底部「等待响应… / 等待子代理结束…」一致；只作用于尾部一轮，
  // 之后若又有了新一轮，更早的轮次即视为已结束。
  // 尾部一轮跳过 turn_id 回填前的唤醒孤儿条目（它马上并入前一轮），避免唤醒消息到达的瞬间上一轮不再是尾部。
  const backgroundBusy = workflowBusy || pendingSubagents > 0;
  const tailIndex = tailRoundIndex(entries);

  const renderEntry = useCallback(
    (entry: TimelineEntry, index: number) => {
      if (entry.kind !== "turn") return <StandaloneEntry entry={entry} />;
      // v12: 已回滚 turn 显示专用横幅（回滚后消息被软删，以此占位区分「回滚了」与「没执行」）
      // plan-334-1661 S3: 改用 O(1) 索引（原先每次渲染每个可见条目都做一次 O(turns) 的 find）
      // plan-75-334 阶段1：一次查询同时供「回滚横幅」与「turn 行状态 props」使用
      const turnRow = getTurnById(turns, entry.turnId);
      const rolledBack = turnRow?.status === "rolled_back";
      // 并入续跑轮时整轮的运行态与行状态取最后一个 turn：整轮结束才算结束
      const tailTurnId = entry.turnIds?.[entry.turnIds.length - 1] ?? entry.turnId;
      const turnRowStatus = getTurnById(turns, tailTurnId)?.status;
      const roundRunning = entry.turnIds
        ? runningTurnId != null && entry.turnIds.includes(runningTurnId)
        : runningTurnId === entry.turnId;
      // 计划卡内嵌到其归属 turn 内部规划说明之后、执行操作之前（彻底根治时序倒挂沉底 Bug）
      const hasTurnPlan = entry.turnId != null && (plansByTurn[entry.turnId] != null || entry.turnId === planTurnId);
      return (
        <TurnGroup
          entry={entry}
          isRunning={roundRunning}
          rolledBack={rolledBack}
          subagents={subagentsOfRound(entry)}
          actions={actions}
          hasPlan={hasTurnPlan}
          globalRunning={isRunning}
          latestUserId={latestUserId}
          flowErrorTurnId={flowErrorTurnId}
          hiddenTurnErrors={hiddenTurnErrors}
          turnStatus={turnRowStatus}
          awaitingBackground={backgroundBusy && index === tailIndex}
          backgroundEndedAt={index === tailIndex ? backgroundEndedAt : 0}
        />
      );
    },
    [turns, runningTurnId, subagentsOfRound, planTurnId, plansByTurn, actions, isRunning, latestUserId, flowErrorTurnId, hiddenTurnErrors, backgroundBusy, tailIndex, backgroundEndedAt]
  );

  // S8：流式文本/信号不再在此计算（已下沉到 <StreamingTail> 的精确 selector）。
  const turnStatus = useScoped("turnStatus");
  const workflowOnly = !isRunning && workflowBusy;
  // plan-308-1542 需求1：任务执行类错误卡（只进消息流，不弹右上角 Toast）
  const flowError = useScoped("flowError");
  const clearFlowError = useScoped("clearFlowError");
  const dismissTurnError = useScoped("dismissTurnError");
  /* plan-549-2281（第7项）：主消息流**自己**订阅滚动目标。
   * 此前靠外层把 scrollTarget 一路传下来，但唯一渲染点（plugins/builtin.ts 注册的
   * props={source:"main"}）从来没传过——任务摘要面板点「定位到执行消息」写进 store 后
   * 没有任何消费者，表现为「点了没反应」，目标还一直挂着不清。
   * 允许外部 props 覆盖（侧边对话等复用场景可以自行控制），缺省读 store。 */
  const storeScrollTarget = useChatStore((s) => s.scrollTarget);
  const storeClearScrollTarget = useChatStore((s) => s.clearScrollTarget);
  const effectiveScrollTarget = scrollTarget !== undefined ? scrollTarget : storeScrollTarget;
  const effectiveClearScrollTarget = clearScrollTarget ?? storeClearScrollTarget;
  /* plan-549-2281（第7项）：子代理 → 所属 turn 的解析放在这一层——
   * subagentMeta 只有主消息流作用域里才有，内核是主/子代理共用的通用组件。
   * 解析不到 turnId 时传 null 给内核，由内核负责清理（目标不能一直挂在 store 里）。 */
  const resolvedScrollTarget = useMemo(() => {
    if (!effectiveScrollTarget) return null;
    if (effectiveScrollTarget.turnId != null) return effectiveScrollTarget;
    const tid = effectiveScrollTarget.threadId;
    if (tid == null) return null;
    const agentTurnId = subagentMeta[tid]?.turnId;
    return agentTurnId != null ? { turnId: agentTurnId } : null;
  }, [effectiveScrollTarget, subagentMeta]);

  return (
    <MessageFlowCore
      key={currentSessionId}
      entries={entries}
      // 主代理未跑但工作流在跑时同样开启流式槽位（否则尾部「等待响应…」不渲染）
      running={isRunning || workflowBusy}
      renderEntry={renderEntry}
      streamingNode={
        // v42: 压缩期间状态行改显「压缩中…」（优先于「处理中…／等待响应…」；
        // 若同时存在 turn 级瞬态提示（如重试），瞬态提示仍优先）。
        <StreamingTail
          active={Boolean((isRunning && runningTurnId) || workflowOnly)}
          statusLabel={turnStatus ?? (isCompacting ? "压缩中…" : undefined)}
          persistedText={workflowOnly ? "" : lastPersistedText(entries)}
        />
      }
      trailingNode={
        // plan-156-619 S10：后续任务建议卡优先级最高——它是本轮收尾后的「下一步」，
        // 与错误卡同为收尾信息，但建议是正向出口，放在更靠前的位置更容易被看到。
        // 运行中 / 等待子代理时不展示：建议只属于「已收口」的一轮。
        // plan-156-621：侧边对话等复用场景可通过 suggestionCard=false 关闭。
        // plan-20261020-suggestion-gate：消息尚未落地（会话首次加载中）时不展示建议卡。
        !isRunning && pendingSubagents === 0 && !activeDebug && !flowError && !messagesPending && suggestionCard !== false ? <SuggestionCard />
          // v39: 后台子代理运行期间主会话保持“运行中”——主 turn 未跑但有子代理在跑时，
          // 用与流式状态行同款样式显示「等待子代理结束…」（完成后服务端自动唤醒新一轮）。
          : !isRunning && pendingSubagents > 0 ? <SubagentWaitingLine count={pendingSubagents} />
          : activeDebug ? <DebugCard status={activeDebug} />
          // plan-308-1542 需求1：任务执行类错误统一在消息流末尾报（不弹右上角）
          : flowError ? (
            <FlowErrorCard
              text={flowError.text}
              onRetry={() => {
                // 重试 = 重发最近一条用户消息（若可定位），否则仅清除提示
                const lastUser = [...messages].reverse().find((m) => m.sender_type === "user");
                if (lastUser) void getScoped().sendTurn(msgText(lastUser.content));
              }}
              // plan-41-228：关闭 = 清除末尾卡 + 隐藏该 turn 内同源 ERROR 消息（否则同一条错误换个样式又冒出来）
              onClose={() => { dismissTurnError(flowError.turnId); clearFlowError(); }}
            />
          )
          : null
      }
      forceBottomKey={userMsgSeq}
      sessionKey={currentSessionId ?? 0}
      jumpDots
      search
      scrollTarget={resolvedScrollTarget}
      clearScrollTarget={effectiveClearScrollTarget}
      className={className}
      emptyText={emptyText ?? "选择或创建会话，开始你的任务"}
    />
  );
}

/** 子代理数据源（source="subagent"）：store 消息桶 + REST 历史合并去重，流式尾部用线程缓冲。 */
function SubagentMessageFlow({
  threadId,
  actions,
  visible = true,
  className,
}: {
  threadId?: number;
  actions?: "full" | "copy-only" | "none";
  /** plan-75-334 阶段3：宿主可见性（右面板隐藏标签为 false）。 */
  visible?: boolean;
  className?: string;
}) {
  const currentSessionId = useScoped("currentSessionId");
  const storeMessages = useChatStore((s) => (threadId != null ? s.subagentMessages[threadId] || [] : []));
  // S8：子代理流式文本同样下沉到 <StreamingTail>（见其注释），此处不再订阅缓冲。
  const subagentMeta = useChatStore((s) => (threadId != null ? s.subagentMeta[threadId] : undefined));
  // plan-75-334 阶段1：turn 行状态改由本层统一查询（与改造前每个 TurnGroup 各自查询语义一致）
  const turns = useScoped("turns");

  // v36 修复：与 SubagentPanel / SubagentCard 口径统一——后端存在 in_progress 状态，
  // 只判 running 会把运行中的面板当成终态（计时条消失、汇报卡片提前生效、运行态标记误判）。
  const isRunning = subagentMeta?.status === "running" || subagentMeta?.status === "in_progress";

  // plan-330-1648 M6: REST 历史回填——事件桶只覆盖“本次连接期间”收到的消息，
  // 重开面板/断线/切会话后会有缺口（用户反馈：面板内容不完整）。这里按 threadId 拉一次历史，
  // 与事件桶按 id 去重后并入（升序），不覆盖已有流式状态。
  useEffect(() => {
    if (threadId == null || currentSessionId == null) return;
    const requestedSid = currentSessionId;
    let cancelled = false;
    void (async () => {
      try {
        const fetched = await api.listSessionMessages(requestedSid, threadId);
        if (cancelled || fetched.length === 0) return;
        useChatStore.setState((s) => {
          // plan-560-2311 E5：请求期间可能已切走会话——迟到的历史不得写进新会话的
          // 同号线程桶（agent id 跨会话会复用），否则切回新会话说看到上一个会话的执行过程。
          if (s.currentSessionId !== requestedSid) return {};
          const bucket = s.subagentMessages[threadId] ?? [];
          const known = new Set(bucket.map((m) => m.id));
          const merged = [...bucket, ...fetched.filter((m) => !known.has(m.id))];
          if (merged.length === bucket.length) return {};
          merged.sort((a, b) => a.id - b.id);
          return { subagentMessages: { ...s.subagentMessages, [threadId]: merged } };
        });
      } catch { /* 非阻塞：拉取失败就只用事件桶 */ }
    })();
    return () => { cancelled = true; };
  }, [threadId, currentSessionId]);

  // 将子代理消息按 timeline 构建（子代理消息通常是平铺的 tool/text/thinking）
  // S8b：同样走增量构建器（与主会话同一套引用复用语义）。
  const buildEntries = useMemo(() => createTimelineBuilder(), []);
  const entries = useMemo(() => buildEntries(storeMessages), [buildEntries, storeMessages]);

  // plan-156-619 S5d：取消「末条 text = 结构化汇报卡」的判定。
  // 子会话就是普通会话，它的输出与主会话同排版（不再六节橙卡），因此无需定位该消息。

  const renderEntry = useCallback(
    (entry: TimelineEntry) => {
      if (entry.kind !== "turn") return <StandaloneEntry entry={entry} />;
      // plan-75-334 阶段1：父层统一计算的全局派生值经 props 传入。
      //  · turnStatus 保留改造前语义（子代理消息的 turn_id 也可能命中主会话 turns 行）；
      //  · latestUserId / flowErrorTurnId / hiddenTurnErrors 仅主消息流消费
      //    （suppressTurnError 有 flow === "main" 前提，错误抑制在子代理面板不适用）。
      const turnRowStatus = entry.turnId != null ? getTurnById(turns, entry.turnId)?.status : undefined;
      return (
        <TurnGroup
          entry={entry}
          isRunning={isRunning}
          actions={actions || "copy-only"}
          flow="subagent"
          agentId={threadId ?? undefined}
          globalRunning={isRunning}
          latestUserId={-1}
          flowErrorTurnId={null}
          hiddenTurnErrors={EMPTY_TURN_ERRORS}
          turnStatus={turnRowStatus}
        />
      );
    },
    [isRunning, actions, threadId, turns]
  );


  return (
    <MessageFlowCore
      key={threadId ?? "subagent-default"}
      entries={entries}
      running={isRunning}
      renderEntry={renderEntry}
      streamingNode={
        // plan-75-334 阶段3：面板隐藏时不推进流式视觉更新（active=false ⇒ TailBody 返回 null，
        // 流式 Markdown 不重算）；恢复可见时 active 恢复，以当前缓冲一次性对齐。
        <StreamingTail
          source={threadId ?? -1}
          active={isRunning && visible}
          statusLabel={isRunning ? "子代理执行中…" : undefined}
          persistedText={lastPersistedText(entries)}
        />
      }
      sessionKey={`${currentSessionId ?? 0}:${threadId ?? 0}`}
      jumpDots={false}
      search={false}
      className={className}
      emptyText="子代理尚未产生输出"
    />
  );
}

/** 统一对外组件：根据 props.source 分流。 */
export function MessageFlow(props: MessageStreamProps) {
  if (props.source === "subagent") {
    return (
      <SubagentMessageFlow
        threadId={props.threadId}
        actions={props.features?.actions ?? "copy-only"}
        visible={props.visible !== false}
        className={props.className}
      />
    );
  }
  return (
    <MainMessageFlow
      actions={props.features?.actions ?? "full"}
      suggestionCard={props.features?.suggestionCard ?? true}
      /* plan-549-2281（第7项）：这里**不要**无条件把 props.scrollTarget 传下去。
       *  props 上没传时它是 undefined，而不是 null——传给 MainMessageFlow 会盖掉
       *  「props 未指定就回落到 store」的语义（见那边的 effectiveScrollTarget）。
       *  显式写了才透传，否则留在 undefined 让主消息流自己订阅 store。 */
      scrollTarget={props.scrollTarget}
      clearScrollTarget={props.clearScrollTarget}
      className={props.className}
      emptyText={props.emptyText}
    />
  );
}
