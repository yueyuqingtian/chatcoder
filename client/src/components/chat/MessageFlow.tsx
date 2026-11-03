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
import { StreamingText } from "./StreamingText";
import { IconSearch, IconChevronUp, IconChevronDown, IconX, IconArrowDown } from "../icons";
import { ErrorCard } from "./ErrorCard";
// plan-156-619 S10：后续任务建议卡（消息流底部收尾卡）
import { SuggestionCard } from "./SuggestionCard";
import { useTextHighlight } from "../../hooks/useTextHighlight";
import { MarkdownContent } from "../MarkdownContent";
import { MsgType } from "@chatcoder/shared";
import { useChatStore, mergeHistoryIntoChat } from "../../store/chat";
import { useVisibleChat } from "../../store/visibleChat";
import { refreshMessageWindow, olderMessages, aroundMessages, aroundTurnMessages, useMessageHistory } from "../../store/messageHistory";
import { useUiStore } from "../../store/ui";
import { useScoped, useScopedGetter, ConvoScopeContext } from "./convoScope";
import { latestWorkflowEndMs, selectSessionWorkflowBusy, useWorkflowStore } from "../../store/workflow";
import { api } from "../../api/client";
import { Button, Empty, Tooltip } from "../ui";
import { StickyBottom } from "./stickyBottom";
import { FlowVisibilityContext } from "./useRunningTicker";
import { advanceUserSendSignal, type UserSendSignal } from "../../utils/userSendSignal";

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

/** plan-20261115-firstframe-geometry：首屏几何对齐的兜底上限。
 *  正常情况下 1~3 帧即可对齐；超过这个窗口说明末项一直没能实测（异常内容/宽度动画中），
 *  宁可先揭示再由贴底内核纠正，也不允许内容被永久隐藏。 */
const FIRST_FRAME_SETTLE_MAX_FRAMES = 12;
const FIRST_FRAME_SETTLE_MAX_MS = 500;

/** plan-20261115-firstframe-geometry：取内容容器里「真实末项底」——末项已挂载则返回它的坐标，否则 null。
 *
 *  为什么需要它：虚拟容器的 height 由虚拟器按 `getTotalSize()` 写，而 getTotalSize 对**尚未挂载**
 *  的项用的是 estimateSize。贴底时若末项未进渲染窗口（首帧尤其如此），尾部就按估算值占位：
 *  估算偏高 ⇒ 最后一条消息下面多出一段空白（且它是可滚动区的一部分，表现就是「已经到底了、
 *  下面还空一截」）；估算偏低则末项被裁。末项一旦挂载，真实高度就已知，就能直接算出正确的总高。 */
function bottomGeometryOf(inner: HTMLElement, totalCount: number): number | null {
  if (totalCount <= 0) return null;
  const tail = inner.querySelector<HTMLElement>(`[data-index="${totalCount - 1}"]`);
  if (!(tail instanceof HTMLElement)) return null;
  // 项是 inner 的绝对定位子项（directDomUpdates 的 position 模式），offsetParent 即 inner
  const bottom = tail.offsetTop + tail.offsetHeight;
  return Number.isFinite(bottom) && bottom > 0 ? bottom : null;
}

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
 *  口径（plan-633-2604 修订）：**首测与重测同口径**——只有「整条已在视口上方」（`item.end <= offset`）
 *  才补偿。跨过视口顶边的条目一律不补偿，交给浏览器原生锚定（锚点在同一条目内，不受 top 重写影响）。
 *
 *  为什么要改：原实现的首测分支用 `item.start < offset`（只要项顶在视口之上就补偿），
 *  而向上滚动时每个新挂载项都满足这个条件，于是上滑一次就触发「逐项补写 scrollTop」的洪流：
 *  一帧内多次写入、库内对 scrollOffset 做乐观累加（可能透支真实值），再叠加原生锚定的同一段补偿，
 *  表现为「停止滚动后还上下抖 200ms」。首测与重测同口径后，跨视口项完全交给原生锚定，不再双写。
 */
function compensateItemAboveViewport(
  item: VirtualItem,
  _delta: number,
  instance: Virtualizer<HTMLDivElement, Element>,
): boolean {
  const offset = instance.scrollOffset ?? 0;
  // 估高项（尚无缓存）的 end 由估算值推出，仍用它判断「整条在视口上方」——
  // 这比原来的 start < offset 更保守：宁可少补一次，也不与原生锚定重复补偿。
  return item.end <= offset;
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
const EMPTY_SUBAGENT_MESSAGES: ReturnType<typeof useChatStore.getState>["messages"] = [];

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

/** plan-20261112-loading-home-sidebar：后台等待复用流式状态行，避免主轮次交接时三点动画消失。 */
const SubagentWaitingLine = memo(function SubagentWaitingLine({ count }: { count: number }) {
  return (
    <StreamingText
      active
      thinking=""
      text=""
      waitingLabel={count > 1 ? `等待子代理结束…（${count} 个运行中）` : "等待子代理结束…"}
    />
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
  historySessionId?: number | null;
  historyThreadId?: number;
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
  historySessionId,
  historyThreadId,
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
  const history = useMessageHistory(historySessionId ?? null, historyThreadId);
  const prependAnchor = useRef<{ key: string | number | bigint; offset: number; messageId?: string; messageOffset?: number } | null>(null);
  const loadingOlder = useRef(false);
  /** plan-630-2590：分页自动触发的冷却时间戳。
   *
   * 为什么需要：加载更早消息会让 scrollHeight 变大，若还原后 scrollTop 仍落在触发阈值内，
   * 同一个 scroll 事件链就会立刻再次触发加载，形成「加载→变高→再加载」的自持回路，
   * 表现为用户没有滚动、消息流却自己上下抖。每次真正发起请求时打点，冷却期内不再触发。 */
  const lastOlderAtRef = useRef(0);
  /** plan-547: 虚拟内容容器（RO 监听测高变化保持贴底） */
  // plan-282-1444：显式含 null 联合类型，保证 ref 可变（需在回调里同时交给虚拟列表）
  const innerRef = useRef<HTMLDivElement | null>(null);
  /** plan-20261115-firstframe-geometry：容器高度校正用的末项下标镜像。
   *  虚拟器选项闭包与 scrollToFn 里读不到最新的 totalCount，用它在这些闭包里取当前末项下标。
   *  由下方 effect 同步（不能在渲染期直接赋值：totalCount 在文件更靠后的位置才声明）。 */
  const innerRefTailIndex = useRef(0);
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
  /** plan-20261118-width-epoch：消息列宽度每变化一次就 +1。
   *  收敛序列据此判断「屏外项的缓存高度是否已过期」——宽度变了，旧宽度下的实测高度就不再是
   *  这一版布局的真相，必须让它们回到 estimateSize 重新真测（详见 remeasureSkippedItems 内注释）。 */
  const widthEpochRef = useRef(0);
  /** 已应用过期失效的宽度纪元（避免同一纪元重复全量重测） */
  const appliedWidthEpochRef = useRef(0);
  const [searchOpen, setSearchOpen] = useState(false);
  const searchInputRef = useRef<HTMLInputElement>(null);
  const [searchKeyword, setSearchKeyword] = useState("");
  const [activeMatchIndex, setActiveMatchIndex] = useState(0);
  const [remoteMatches, setRemoteMatches] = useState<Array<{ id: number; turn_id: number | null }>>([]);
  const [remoteMore, setRemoteMore] = useState(false);
  const [remoteLoading, setRemoteLoading] = useState(false);
  const searchController = useRef<AbortController | null>(null);
  const [pendingMatch, setPendingMatch] = useState<{ id: number; turn_id: number | null; loaded?: boolean } | null>(null);
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
  // plan-20261115-firstframe-geometry：把「当前末项下标」同步给只能在闭包里读 ref 的地方
  // （虚拟器选项 / scrollToFn）。渲染期赋值即可，它只影响下一次布局写入。
  innerRefTailIndex.current = totalCount > 0 ? totalCount - 1 : 0;

  /** plan-282-1434（B6）+ plan-282-1444：首帧定位状态。
   *  此前：虚拟列表初始 scrollOffset=0 → 先渲染顶部若干项，再由 useLayoutEffect 把
   *  scrollTop 推到底，多帧叠加后表现为"点击会话后从上往下滚一遍"。
   *  现在：① initialOffset 按末项估算位置给出初始偏移，首帧即落在底部区域；
   *        ② 定位**收敛**（总高连续稳定）前用 .is-positioning 隐藏内容。 */


  const [positioned, setPositioned] = useState(false);
  const EST_ITEM_H = 140;
  /* plan-20261115-firstframe-geometry：首帧「估算 → 实测」为什么必须在揭示前完成。
   *
   *  ── 修的根因 ──
   *  切会话 / 重新进入子代理面板时 MessageFlowCore 被重建（`key={currentSessionId}`），
   *  虚拟器的 itemSizeCache 清空，只剩 estimateSize；旧实现又用估算值初始化 `initialOffset`，
   *  而 DOM 的 scrollTop 仍是 0。两者只在**第一个 scroll 事件**后才对齐，而这个事件只有用户
   *  滚动才会产生。在此之前：补偿谓词拿估算偏移判「项在视口上方」→ scrollToFn 把 scrollTop
   *  写到数万像素再被真实 scrollHeight 钳制；内容容器被撑到估算总高；而 is-positioning 的揭示
   *  判据只看「总高稳定」，正好在这个中间态之后揭示 ⇒ 露出上下大片空白，滚一下才对齐。
   *
   *  ── 现在 ──
   *  首帧不再由估算值决定任何可见几何：DOM scrollTop 直接写真实 scrollHeight，虚拟器偏移在同一帧
   *  显式对齐；揭示改为「几何真的对齐」后才执行（判据见下方 useLayoutEffect）。
   *
   *  注意：**不得**改回「用估算总高初始化偏移」——那是这一系列问题的起点。 */

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
  /** plan-633-2604：记录本轮运动期**被跳过测量的 index**（原来是 boolean）。
   *  只记 boolean 时，补测只能覆盖「补测那一刻仍挂载」的项；运动期挂载过又卸载的项
   *  永远停留在旧高度，于是消息流中间露出一片占位空白（滚动让它重挂载真测后恢复）。
   *  记成集合后，补测结束仍未被补到的项会被显式失效，下次挂载首帧强制真测。 */
  const skippedDuringMotionRef = useRef<Set<number>>(new Set());

  /** plan-586-2374 §2.1：自定义测量口径（覆盖 VirtualizerOptions.measureElement）。
   *  静止期与改造前**完全一致**（一律 defaultMeasureElement 真测）；只有「运动期 + 视口外」
   *  才返回缓存/估算值，从而既不读 DOM、也不推进 itemSizeCacheVersion → 不触发
   *  getMeasurements 的后缀重算。视口内的项永远真测，这是视觉不变量。 */
  const measureElementCb = useCallback(
    (el: Element, entry: ResizeObserverEntry | undefined, instance: Virtualizer<HTMLDivElement, Element>): number => {
      const index = instance.indexFromElement(el);
      if (isBusy() && shouldSkipMeasurement(index, instance, visibleIndexSetRef.current)) {
        skippedDuringMotionRef.current.add(index);
        recordDerivation("virtualMeasureSkipped");
        const key = instance.options.getItemKey(index);
        return instance.itemSizeCache.get(key) ?? instance.options.estimateSize(index);
      }
      /* plan-633-2604：0 高度保护。
       * 默认测量直接取 borderBoxSize.blockSize，没有 0 值防护。右面板用 display:none 保活非激活标签，
       * 子树尺寸归零时逐项 RO 会以 blockSize=0 回调 → 尺寸被写成 0 → 这些项的 start 全部相等，
       * 表现为整片空白（总高塌陷）或多项叠在同一位置。
       * 这里对 0 高度一律回退到「缓存值，其次估算值」：真实高度为 0 的条目本来也不会被用户看到，
       * 而错把 0 写进缓存会让后续所有项的位置一起错。 */
      const measured = defaultMeasureElement(el, entry, instance);
      if (!Number.isFinite(measured) || measured <= 0) {
        recordDerivation("virtualMeasureZero");
        const key = instance.options.getItemKey(index);
        return instance.itemSizeCache.get(key) ?? instance.options.estimateSize(index);
      }
      recordDerivation("virtualMeasure");
      return measured;
    },
    [],
  );

  const virtualizer = useVirtualizer({
    getItemKey: (index) => {
      const entry = entries[index];
      if (!entry) {
        /* plan-630-2590：流式槽 / 尾部槽的 key 必须绑定「当前这批内容」，不能是常量。
         *
         * 之前 `"stream"` / `"trailing"` 是固定字符串，于是虚拟器的 itemSizeCache 会跨轮复用：
         * 上一轮流式内容可能高达几百像素，下一轮刚开始只有一行，库却先把旧高度按上去，
         * 该项与下一段内容之间就露出大片空白，直到滚动触发重新挂载与真测才恢复。
         * 用「已落库的最后一条 entry」作后缀：流式过程中它不变（槽位身份稳定），
         * 一旦有新消息落库或换轮，键随之变化 → 天然不复用旧缓存。 */
        const last = entries.length > 0 ? entries[entries.length - 1] : null;
        const tailId = last == null
          ? "empty"
          : last.kind !== "turn"
            ? last.msg.id
            : (last.turnId ?? (last.items[0] && "msg" in last.items[0] ? last.items[0].msg.id : entries.length));
        return index === entries.length && hasStreaming ? `stream:${tailId}` : `trailing:${tailId}`;
      }
      if (entry.kind !== "turn") return `message:${entry.msg.id}`;
      const first = entry.items[0];
      return `turn:${entry.turnId ?? (first && "msg" in first ? first.msg.id : index)}`;
    },
    count: totalCount,
    getScrollElement: () => parentRef.current,
    estimateSize: (index) => heightEstRef.current[estBucketOf(index)],
    /* plan-20261115-firstframe-geometry：**不**再用估算值初始化内部偏移。
     *
     * 此前这里是 `(totalCount - 1) * estimateSize(末项)`：它只写虚拟器内部变量，不改 DOM，
     * 于是首帧渲染范围按「估算的底部」算、实际 scrollTop 是 0，两者在第一个 scroll 事件前不等。
     * 由此派生的空白链条见 geometryAlignedRef 的注释。
     *
     * 现在从 0 起，但 0 不是「显示在顶部」——首帧的贴底由 layoutEffect 直接写 DOM
     * （见 startFirstFrameGeometry），虚拟器随后的 scroll 回调把内部偏移同步成真实值。
     * 必须保留这个选项（传 0）而不是删掉：删掉会让库回落到它自己的默认 0，语义相同但少一层显式说明。 */
    initialOffset: 0,
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
      /* plan-20261115-firstframe-geometry：容器高度优先用「真实末项底」，不再无条件写估算总高。
       *
       * 直接写 `getTotalSize()` 的问题：末项尚未挂载时它是估算值，写进容器就成了一段多余的
       * 滚动空间——表现就是「已经到底了，下面还空一截」。而预撑高本身仍必须保留，否则靠近底部
       * 时补偿会被旧高度钳制（见上方注释）。能算出真实末项底就用真实的，否则回落到估算值。 */
      if (inner) {
        const realBottom = bottomGeometryOf(inner, innerRefTailIndex.current + 1);
        inner.style.height = `${realBottom ?? instance.getTotalSize()}px`;
      }
      elementScroll(offset, options, instance);
    },
    // plan-20261020-pts：测量回调里条目尺寸 / 位置 / 总高刚写入 DOM，此时请求贴底可在同一帧完成
    // （上方条目变高时尾项尺寸不变，容器高度的尺寸通知又要晚一帧，只靠尺寸观察会露出一帧未贴底）
    onChange: () => {
      sticky.requestPin();
      scheduleBottomGeometryFix();
    },
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


  const loadOlder = useCallback(async () => {
    const el = parentRef.current;
    if (!el || historySessionId == null || loadingOlder.current || !history.hasBefore) return;
    loadingOlder.current = true;
    lastOlderAtRef.current = performance.now();
    /* plan-630-2590：只有「用户已经离开底部」时才解除跟随。
     *
     * 原实现无条件 release()，会把跟随状态永久置 false（除 running 起跳 / 手动回底外无人恢复），
     * 于是 onScroll 里的 `!sticky.isFollowing()` 守卫从此恒真，任何 scroll 事件都能再次自动加载。
     * 跟随中说明用户在看最新内容，加载历史不该改变他的视图意图。 */
    if (!sticky.isFollowing()) sticky.release();
    try {
      const messages = await olderMessages(historySessionId, historyThreadId);
      // 数据到达时才捕获锚点，等待期间用户继续滚动或新输出落地不应使旧锚点抢位。
      if (!sticky.isFollowing()) {
        const anchor = virtualizer.getVirtualItems().find((item) => item.end > el.scrollTop);
        if (anchor) {
          const top = el.getBoundingClientRect().top;
          const node = Array.from(el.querySelectorAll<HTMLElement>("[data-message-id]")).find((element) => element.getBoundingClientRect().bottom > top);
          prependAnchor.current = { key: anchor.key, offset: el.scrollTop - anchor.start,
            messageId: node?.dataset.messageId, messageOffset: node ? node.getBoundingClientRect().top - top : undefined };
        }
      }
      mergeHistoryIntoChat(historySessionId, messages, historyThreadId);
    }
    catch { prependAnchor.current = null; }
    finally { loadingOlder.current = false; }
  }, [historySessionId, historyThreadId, history.hasBefore, sticky, virtualizer]);

  useLayoutEffect(() => {
    const anchor = prependAnchor.current;
    if (!anchor) return;
    /* plan-20261115-firstframe-geometry：直写 scrollTop 之前先把容器撑到新总高。
     *
     * `scrollToOffset` 走 scrollToFn（自带撑高），但下面这两处纠偏是**直接写 el.scrollTop**，
     * 不经过它——旧高度会先把写入钳制住，剩下那段位移就露成「加载更早消息后顶部/底部缺口 」
     * （plan-630-2590 只把差值阈值收到 0.5px，没有处理钳制本身）。prepend 之后末项位置不变，
     * 所以这里优先用真实末项底，取不到才用估算总高。 */
    const inner = innerRef.current;
    if (inner) {
      const realBottom = bottomGeometryOf(inner, totalCount);
      inner.style.height = `${realBottom ?? virtualizer.getTotalSize()}px`;
    }
    const item = virtualizer.measurementsCache.find((value) => value.key === anchor.key);
    if (item) virtualizer.scrollToOffset(item.start + anchor.offset);
    const el = parentRef.current;
    if (el && anchor.messageId && anchor.messageOffset != null) {
      const node = el.querySelector<HTMLElement>(`[data-message-id="${anchor.messageId}"]`);
      /* plan-630-2590：第二次校正按真实 DOM 补差值，但加亚像素阈值。
       *
       * 第一次 scrollToOffset 用的是虚拟器估算几何，第二次用真实 DOM 位置纠偏——
       * 这一步本身是必要的；问题在于它无条件写 scrollTop，而每次写入都会触发 onScroll，
       * 与 `shouldAdjustScrollPositionOnItemSizeChange` 的补偿抢同一段位移，一帧内先跳后拉。
       * 差值在半像素以内时说明第一次已经对齐，跳过写入即可断开这条无谓的写入链。 */
      if (node) {
        const delta = node.getBoundingClientRect().top - el.getBoundingClientRect().top - anchor.messageOffset;
        if (Math.abs(delta) > 0.5) el.scrollTop += delta;
      }
    }
    prependAnchor.current = null;
  }, [entries, virtualizer, totalCount]);

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
    /* plan-630-2590：接近顶部才自动加载更早的消息。
     *
     * 原实现用绝对阈值 `scrollTop < 400`：子代理面板视口本身不高（容器窄、头部还会周期性变高），
     * 400px 几乎覆盖整个视口，于是任何 scroll 事件都会触发加载；配合 prepend 后 scrollTop 仍可能落在
     * 阈值内，就形成自持回路（没滚轮却上下抖）。这里三处收紧：
     *  1. 阈值取视口高度的 30% 且不超过 200px——小视口不再恒真；
     *  2. 400ms 冷却——一次加载的几何收敛期内不再触发下一次；
     *  3. 只在用户不在底部时加载（跟随态说明他在看最新内容）。 */
    if (positioned && !sticky.isFollowing()) {
      const nearTopPx = Math.min(200, el.clientHeight * 0.3);
      if (el.scrollTop < nearTopPx && performance.now() - lastOlderAtRef.current > 400) void loadOlder();
    }
  }, [scheduleSpy, scheduleVisibleRefresh, positioned, sticky, loadOlder]);

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
      /* plan-20261115-firstframe-geometry：直写 scrollTop 前先把容器撑到正确总高。
       * 这里 ①② 都是直接写 el2.scrollTop（不走 scrollToFn，没有预撑高）：宽度变化后总高
       * 会先按估算值排一遍，旧高度会把写入钳制住，剩下的位移就留成内容上方的空隙，
       * 而 ② 的像素级校正又是「先读位置再增量写」，被钳制时增量写不进去、下一帧 continue，
       * 表现成「拖完侧栏后消息停在中间、上下都留白」。 */
      const innerEl = innerRef.current;
      if (innerEl) {
        const realBottom = bottomGeometryOf(innerEl, totalCount);
        innerEl.style.height = `${realBottom ?? virtualizer.getTotalSize()}px`;
      }
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
      const skipped = skippedDuringMotionRef.current;
      /* plan-20261118-width-epoch：宽度纪元的失效**不在这里做**——本任务是 order 40，
       *  而锚点还原是 order 30，先执行。若把失效挪到这里，锚点还原读到的仍是「新旧宽度混算」
       *  的总高，还原目标本身就错，改动等于没生效。失效已抽成 invalidateOnWidthChange()，
       *  注册在 RECONCILE_ORDER.widthInvalidate（25，rect 之后、锚点还原之前）。 */
      if (skipped.size === 0) return;
      const pending: Array<{ index: number; height: number }> = [];
      // plan-633-2604：补到的 index 从集合里摘除；集合里剩下的就是「运动期挂载过、
      // 补测时已卸载」的项——它们必须显式失效，否则会一直按旧尺寸占位（大片空白）。
      const unresolved = new Set(skipped);
      virtualizer.elementsCache.forEach((el) => {
        if (!el.isConnected) return;
        const index = virtualizer.indexFromElement(el);
        if (index < 0) return;
        const height = Math.round(el.getBoundingClientRect().height);
        // 未布局 / 已隐藏的项读不到高度，跳过它但不影响其余项（forEach 里的 return 即 continue）。
        if (!Number.isFinite(height) || height <= 0) return;
        unresolved.delete(index);
        /* plan-630-2590：与缓存一致的项不再 push。
         *
         * resizeItem 会推进 itemSizeCacheVersion 并触发 scrollTop 补偿；对一个本来就没变的项
         * 反复写同值，等于在收敛序列里制造无谓的重排，与贴底内核抢同一帧。缓存已经是正确值时
         * 直接跳过，把写入集中到真正需要纠正的项上。 */
        const cached = virtualizer.itemSizeCache.get(virtualizer.options.getItemKey(index));
        if (cached != null && Math.abs(cached - height) < 1) return;
        pending.push({ index, height });
      });
      skipped.clear();
      for (const p of pending) virtualizer.resizeItem(p.index, p.height);
      /* plan-20261118-width-epoch：未解析项**不再写回估算值**，改为从缓存删除。
       *
       *  plan-633-2604 当年写「估算值」是为了打断旧缓存，但写进去之后
       *  `shouldSkipMeasurement` 的「从未测过必须真测」守卫（判据是 itemSizeCache.has）
       *  就不再放行——该项下次挂载本会真测，却因为这行写入被判成「已测过」而沿用估算值，
       *  于是这一项的高度误差要等下一次滚动/重挂载才被纠正，正是「中间一片空白、滚几下才好」。
       *
       *  删除则同时满足两个目标：断开了旧缓存（Map 里没有该 key → 回到 estimateSize），
       *  又保住了「未测过必须真测」的语义（下次挂载立即真测）。
       *  这里不能用 virtualizer.measure()：那会清掉视口内刚测好的正确值，
       *  而本任务（order 40）执行时锚点还原已经完成，再全清会让位置二次跳。 */
      for (const index of unresolved) {
        virtualizer.itemSizeCache.delete(virtualizer.options.getItemKey(index));
      }
      if (pending.length === 0 && unresolved.size === 0) return;
      // 高度已变 → 可见范围与贴底同步跟上，避免收敛后露出半帧偏差
      refreshVisibleIndexes();
      sticky.pin();
    };
    /* plan-20261118-width-epoch：宽度纪元失效（修「展开右面板 → 拖宽度 → 折叠右面板」后的空白）。
     *
     *  ── 根因 ──
     *  过渡期间消息列宽度持续变化，已挂载项的文本逐帧重新折行、真实高度跟着变；但**屏外项
     *  不会被重测**：`measureElement` 在运动期对「视口 ± 1 屏之外」直接返回缓存值
     *  （shouldSkipMeasurement），而运动结束的补测只覆盖「补测时仍挂载」的项——拖动结束时
     *  它们早已卸载。于是缓存高度停在**旧宽度**的实测值，视口内却是**新宽度**的实测值：
     *  总高 = 新旧宽度混算，项位置由 start 累计，失真总高把后续内容整体推偏，露出的就是
     *  消息之间/末条下方那片空白。滚动让项重新挂载并被真测，于是「滚几下就恢复」。
     *
     *  ── 为什么必须排在锚点还原之前（order 25 < 30）──
     *  锚点还原要用「总高与各项 start」算出目标 scrollTop。若先还原、后失效，还原用的就是
     *  那个混算总高，目标位置本身就是错的，等于白改。所以顺序必须是：
     *  提交视口 rect（20）→ **失效并重测（25）** → 还原锚点 / 贴底（30）。
     *
     *  ── 为什么用 measure() 而不是逐项 resizeItem ──
     *  `measure()` 是官方语义的「所有项回到 estimateSize」：清 itemSizeCache、pendingMin 归零、
     *  推进 itemSizeCacheVersion、notify(false)。逐项 resizeItem 会按补偿谓词一百多次写
     *  scrollTop，与贴底/锚点三方互抢；而直接改 itemSizeCache + 手写版本号会踩 private 字段
     *  （版本号不推进则 getMeasurements 继续返回旧 flat 数组）。
     *  失效后立刻把**仍挂载**项的实测高度写回：它们已完成折行，量到的就是新布局真值，
     *  视口内不会先渲染一帧估算排版再跳；屏外项保持无缓存，下次挂载时由 measureElement 真测。 */
    const invalidateOnWidthChange = () => {
      if (widthEpochRef.current === appliedWidthEpochRef.current) return;
      appliedWidthEpochRef.current = widthEpochRef.current;
      skippedDuringMotionRef.current.clear();
      virtualizer.measure();
      const pending: Array<{ index: number; height: number }> = [];
      virtualizer.elementsCache.forEach((el) => {
        if (!el.isConnected) return;
        const index = virtualizer.indexFromElement(el);
        if (index < 0 || index >= virtualizer.options.count) return;
        const height = Math.round(el.getBoundingClientRect().height);
        if (!Number.isFinite(height) || height <= 0) return;
        pending.push({ index, height });
      });
      // 先读齐再写：resizeItem 会按 delta 补偿 scrollTop，边读边写会退化成 N 次强制同步布局
      for (const p of pending) virtualizer.resizeItem(p.index, p.height);
      refreshVisibleIndexes();
      /* 重测后库的总高按新尺寸重算，但 directDomUpdates 的容器高度要等它的 onChange 才写；
       * 这里直接把容器校正一次，避免「总高已变、容器还是旧的」这段时间里贴底算到错的底部
       * （末条下方留白）。校正取真实末项底，末项未挂载时退回虚拟器总高。 */
      const innerEl = innerRef.current;
      if (innerEl) {
        const realBottom = bottomGeometryOf(innerEl, totalCount);
        innerEl.style.height = `${realBottom ?? virtualizer.getTotalSize()}px`;
      }
    };
    // plan-31-151 S4：锚点收尾路径去重——删除 pointerup 捕获监听与 panel-drag-end 事件，
    //   只保留 reconcile order 30 一条路径；窗口 resize 路径仍走 chatcoder:window-motion。
    const offWidth = registerReconcileTask(`mf-width-invalidate:${instanceId}`,
      RECONCILE_ORDER.widthInvalidate, "宽度变化后让屏外项高度失效", () => invalidateOnWidthChange());
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
        // plan-20261118-width-epoch：同样要在这里做宽度失效——否则「窗口缩放」这条路径
        // 的屏外项仍停在旧宽度高度（reconcile 任务根本没被调用）。
        invalidateOnWidthChange();
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
      // plan-633-2604：集合按「单次运动」归零（记 index，供收尾逐项补测/失效）。
      skippedDuringMotionRef.current.clear();
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
      // plan-633-2604：同 onDragStart，按运动源归零本次运动要记录的跳过项。
      skippedDuringMotionRef.current.clear();
    };
    window.addEventListener("chatcoder:pane-change-start", onPaneChangeStart);
    const ro = new ResizeObserver(() => {
      const el2 = parentRef.current;
      if (!el2) return;
      // plan-20261118-width-epoch：宽度确实变了就记一个纪元，收敛期据此让屏外项高度失效。
      // 放在 isBusy 早退之前——运动期的宽度变化同样会让缓存高度过期，这正是要修的路径。
      const wNow = el2.clientWidth;
      if (wNow > 0 && Math.abs(wNow - lastWidthRef.current) >= 1) {
        widthEpochRef.current += 1;
      }
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
      offRect(); offAnchor(); offRemeasure(); offWidth();
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

  /** plan-20261115-firstframe-geometry：把内容容器的总高校正到「真实末项底」。返回末项是否已实测。
   *
   *  ── 修的是什么（底部一段空白）──
   *  虚拟容器的 height 由虚拟器按 `getTotalSize()` 写，而 getTotalSize 对**尚未挂载**的项用的是
   *  estimateSize。贴底时若末项还没进渲染窗口（首帧尤其如此），尾部就按估算值占位：
   *  估算偏高 ⇒ 最后一条消息下面多出一段空白，且这段空白是「可滚动区」的一部分，
   *  表现为「已经到底了，但下面还有一截空的」；估算偏低则末项被裁。
   *
   *  一旦末项挂载，它的真实高度就已知，于是可以：
   *   ① 把容器高度写成末项的真实 bottom（贴着底就没有多余滚动空间）；
   *   ② 顺手再把 scrollTop 顶到真实底部。
   *  后续库若把高度写回估算值（下一次 onChange），下一帧会再校正一次，直到库拿到的总高与真实一致
   *  （末项实测后 getTotalSize 本身就等于真实 bottom，校正自动停止，不会长期互写）。 */
  const syncBottomGeometry = useCallback((): boolean => {
    const el = parentRef.current;
    const inner = innerRef.current;
    if (!el || !inner || totalCount === 0) return false;
    const realBottom = bottomGeometryOf(inner, totalCount);
    if (realBottom == null) return false;
    if (Math.abs(inner.offsetHeight - realBottom) > 1) {
      inner.style.height = `${realBottom}px`;
    }
    return true;
  }, [totalCount]);

  /** plan-20261115-firstframe-geometry：把「校正容器总高」排队到微任务，一帧内合并为一次。
   *
   *  为什么需要调度而不是在 onChange 里直接调用：库每次测量后都会写容器高度并触发 onChange，
   *  而校正本身又要改这个高度——直接同步写会让「写 → 观察 → 再写」在同一帧里反复触发。
   *  合并到微任务后，一次测量回调最多校正一次；且只有在校正确实改变了高度时才补一次贴底，
   *  否则会让贴底与滚动事件互相唤醒（贴底写 scrollTop → 触发 onChange → 再贴底）。
   *  末项实测后 getTotalSize 与真实末项底一致，校正成为 no-op，这条链路自然停下。 */
  const bottomFixQueuedRef = useRef(false);
  const scheduleBottomGeometryFix = useCallback(() => {
    if (bottomFixQueuedRef.current) return;
    bottomFixQueuedRef.current = true;
    queueMicrotask(() => {
      bottomFixQueuedRef.current = false;
      const inner = innerRef.current;
      const before = inner?.offsetHeight ?? 0;
      if (!syncBottomGeometry()) return;
      if (inner && Math.abs(inner.offsetHeight - before) > 1) sticky.requestPin();
    });
  }, [syncBottomGeometry, sticky]);

  useLayoutEffect(() => {
    /* plan-20261115-firstframe-geometry：首屏定位改为「按真实 DOM 收敛」，不再按估算总高收敛。
     *
     * 三版沿革（都写在这里，避免以后再退回前两版）：
     *   · plan-282-1434：首帧先渲染顶部再由 layoutEffect 把 scrollTop 推到底 —— 多帧叠加成
     *     「点开会话后从上往下滚一遍」；
     *   · plan-282-1444：用估算偏移做首帧起点 + 「总高连续 2 帧不变」作揭示判据 —— 估算偏移只写
     *     虚拟器内部变量、不改 DOM，两者在第一个 scroll 事件前不相等；而判据只看总高，正好在这个
     *     中间态之后揭示，于是露出上下大片空白（本轮要修的主症状）。
     *   · 现在：揭示条件改为**几何真的对齐**——末项已挂载并实测、容器高等于真实末项底、
     *     距底 ≤1px、且虚拟器内部偏移已等于 DOM scrollTop。任何一条不满足都继续藏在
     *     .is-positioning 下，并用 scrollToIndex(末项) 把末项拉进渲染窗口。 */
    if (totalCount === 0) return;
    if (initSessionKeyRef.current !== sessionKey) {
      initSessionKeyRef.current = sessionKey;
      setPositioned(false); // 进入新会话：先隐藏，待几何对齐
      sticky.stick();

      cancelAnimationFrame(settleRafRef.current);
      const startedAt = performance.now();
      let stableFrames = 0;
      let frames = 0;
      const settle = () => {
        settleRafRef.current = 0;
        const el = parentRef.current;
        const inner = innerRef.current;
        // 用户在这段窗口内主动上滑：立即停止定位并揭示，不与他抢滚动
        if (!el || !inner || !sticky.isFollowing()) {
          setPositioned(true);
          return;
        }
        frames += 1;
        // ① 末项未进渲染窗口时，先按虚拟器几何把它拉进来——否则总高一直是估算值，
        //    贴底会停在「估算出来的底部」，那里既没有内容又比真实底部高（上下同时露白）。
        const tailMeasured = syncBottomGeometry();
        if (!tailMeasured) {
          virtualizer.scrollToIndex(totalCount - 1, { align: "end", behavior: "auto" });
        } else {
          // ② 末项已实测：直接写真实底部（浏览器按真实 scrollHeight 钳制），
          //    这次写入产生的 scroll 事件会把虚拟器内部偏移同步成真实值。
          el.scrollTop = el.scrollHeight;
        }

        // ③ 对齐判据：末项已实测 + 已在底部 + 内部偏移与 DOM 一致（±1px 抗小数舍入）
        const atBottom = el.scrollHeight - el.clientHeight - el.scrollTop <= 1;
        /* 同帧显式对齐虚拟器内部偏移。
         *
         * 为什么不能只等 scroll 事件：写 scrollTop 产生的 scroll 事件是**异步分发**的
         * （滚动由合成/事件循环派发，不在写入当帧的 rAF 回调里），而本循环一帧只跑一次。
         * 只依赖事件的话，`offsetSynced` 在写入后的那一帧恒为 false，只能等到超时兜底才揭示——
         * 又把「等一个说不准的时刻」引了回来。
         *
         * 直接写实例字段是安全的：库的 scroll 回调本身也是赋值同一字段（virtual-core:855），
         * 两者写的是同一个真实值；后续事件到达时赋同样的值，不会产生方向误判。 */
        virtualizer.scrollOffset = el.scrollTop;
        const offsetSynced = Math.abs((virtualizer.scrollOffset ?? 0) - el.scrollTop) <= 1;
        if (tailMeasured && atBottom && offsetSynced) stableFrames += 1;
        else stableFrames = 0;

        const aligned = stableFrames >= 1;
        const timedOut = frames >= FIRST_FRAME_SETTLE_MAX_FRAMES
          || performance.now() - startedAt > FIRST_FRAME_SETTLE_MAX_MS;
        if (aligned || timedOut) {
          // 超时也揭示：不允许内容被永久隐藏；此后由 StickyBottom 的尺寸观察继续纠正
          setPositioned(true);
          return;
        }
        settleRafRef.current = requestAnimationFrame(settle);
      };
      settleRafRef.current = requestAnimationFrame(settle);
      return;
    }
    /* 条目数变化 / 同一会话内追加内容：
     * 先把尾部高度校正到真实末项底（消除底部空白），再贴底。
     * 注意顺序——贴底读的是 scrollHeight，必须在校正之后。 */
    syncBottomGeometry();
    sticky.pin();
  }, [sessionKey, totalCount, virtualizer, sticky, syncBottomGeometry]);

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

  // plan-20261101-performance：发消息只由 forceBottomKey 识别，向前补历史不能触发贴底。

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
  const configuredSearchEntry = useUiStore((s) => s.showMessageSearch);
  const searchEntryVisible = configuredSearchEntry || historyThreadId != null;

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
      const focused = document.activeElement;
      const focusedFlow = focused?.closest(".message-flow-outer");
      if (focusedFlow && !focusedFlow.contains(parentRef.current)) return;
      const subPanel = focused?.closest(".subagent-panel");
      if (historyThreadId != null ? !focusedFlow && !subPanel : subPanel != null) return;
      e.preventDefault();
      setSearchOpen(true);
      // 搜索条已展开时输入框已挂载：直接交出焦点并全选；未展开时由输入框的 autoFocus 完成
      searchInputRef.current?.focus();
      searchInputRef.current?.select();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [search, historyThreadId]);

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
      /* plan-20261115-firstframe-geometry：直写 scrollTop 前先把容器撑到正确总高。
       * 目标项在长会话里常靠后，`getOffsetForIndex` 给出的落点可能超过当前 scrollHeight——
       * 而被旧高度钳制的那一段就变成「跳过去之后目标上方/下方多出空白」。 */
      const innerEl = innerRef.current;
      if (innerEl) {
        const realBottom = bottomGeometryOf(innerEl, totalCount);
        innerEl.style.height = `${realBottom ?? virtualizer.getTotalSize()}px`;
      }
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
    // plan-20261115-firstframe-geometry：与 scrollToFn 同口径——能算出真实末项底就用真实值，
    // 否则回落到估算总高；只写估算总高会在这里凭空多出一段可滚动区（帧末回弹）。
    const inner = innerRef.current;
    if (inner) {
      const realBottom = bottomGeometryOf(inner, totalCount);
      inner.style.height = `${realBottom ?? virtualizer.getTotalSize()}px`;
    }
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
  }, [sticky, virtualizer, scheduleSpy, totalCount]);

  useEffect(() => {
    if (historySessionId != null) return;
    setActiveMatchIndex(0);
    if (matchedIndices.length > 0) jumpToIndex(matchedIndices[0], "center");
  }, [matchedIndices, jumpToIndex, historySessionId]);

  useEffect(() => {
    if (historySessionId == null || !searchOpen || !searchKeyword.trim()) {
      setRemoteMatches([]); setRemoteMore(false); return;
    }
    const controller = new AbortController();
    searchController.current = controller;
    setRemoteLoading(true);
    const timer = window.setTimeout(() => {
      void api.searchMessages(historySessionId, searchKeyword.trim(), historyThreadId, controller.signal).then((result) => {
        if (controller.signal.aborted) return;
        setRemoteMatches(result.items); setRemoteMore(result.has_more); setActiveMatchIndex(0);
        setPendingMatch(result.items[0] ?? null);
      }).catch(() => { if (!controller.signal.aborted) setRemoteMatches([]); })
        .finally(() => { if (!controller.signal.aborted) setRemoteLoading(false); });
    }, 200);
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [historySessionId, historyThreadId, searchKeyword, searchOpen]);

  useEffect(() => {
    if (!pendingMatch || historySessionId == null) return;
    const index = entries.findIndex((entry) => entry.kind === "turn"
      ? (pendingMatch.turn_id != null && (entry.turnId === pendingMatch.turn_id || entry.turnIds?.includes(pendingMatch.turn_id)))
        || entry.items.some((item) => "msg" in item && item.msg.id === pendingMatch.id)
      : entry.msg.id === pendingMatch.id);
    if (index >= 0 && pendingMatch.loaded) { jumpToIndex(index, "center"); setPendingMatch(null); return; }
    if (pendingMatch.loaded) { setPendingMatch(null); return; }
    let cancelled = false;
    void aroundMessages(historySessionId, pendingMatch.id, historyThreadId).then((messages) => {
      if (!cancelled) {
        mergeHistoryIntoChat(historySessionId, messages, historyThreadId);
        setPendingMatch({ ...pendingMatch, loaded: true });
      }
    }).catch(() => { if (!cancelled) setPendingMatch(null); });
    return () => { cancelled = true; };
  }, [pendingMatch, entries, historySessionId, historyThreadId, jumpToIndex]);

  const jumpMatch = (dir: 1 | -1) => {
    if (historySessionId != null) {
      if (!remoteMatches.length) return;
      const next = (activeMatchIndex + dir + remoteMatches.length) % remoteMatches.length;
      setActiveMatchIndex(next); setPendingMatch(remoteMatches[next]); return;
    }
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
    if (idx < 0 && historySessionId != null && scrollTarget.turnId != null) {
      let cancelled = false;
      void aroundTurnMessages(historySessionId, scrollTarget.turnId, historyThreadId).then((messages) => {
        if (!cancelled) mergeHistoryIntoChat(historySessionId, messages, historyThreadId);
      }).catch(() => { if (!cancelled) clearScrollTarget?.(); });
      return () => { cancelled = true; };
    }
    if (idx >= 0) jumpToIndex(idx, "start");
    clearScrollTarget?.();
  }, [scrollTarget, entries, jumpToIndex, clearScrollTarget, historySessionId, historyThreadId]);

  const activeHighlightIndex = historySessionId == null ? matchedIndices[activeMatchIndex] : entries.findIndex((entry) => {
    const match = remoteMatches[activeMatchIndex];
    if (!match) return false;
    return entry.kind === "turn" ? (match.turn_id != null && (entry.turnId === match.turn_id || entry.turnIds?.includes(match.turn_id)))
      || entry.items.some((item) => "msg" in item && item.msg.id === match.id) : entry.msg.id === match.id;
  });
  return (
    <div className={`message-flow-outer ${className || ""}`}>
      {historySessionId != null && history.hasBefore && <div className="flow-history-control">
        <Button size="xs" variant="subtle" loading={history.loading} onClick={() => void loadOlder()}>
          {history.error ? "重试加载历史" : "加载更早消息"}
        </Button>
      </div>}
      {/* plan-156-619 S2：入口按钮受「设置 → 常规 → 消息搜索按钮」控制；
          搜索条一旦打开则始终显示（否则 Ctrl+F 唤起后看不到输入框）。 */}
      {search && (searchOpen || searchEntryVisible) && (
        <div className="flow-search-toggle">
          {!searchOpen ? (
            <Tooltip content="搜索消息 (Ctrl+F)">
              <button
                type="button"
                className="flow-search-btn"
                onClick={() => setSearchOpen(true)}
                aria-label="搜索消息"
              >
                <IconSearch size={14} />
              </button>
            </Tooltip>
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
              {(historySessionId != null ? remoteMatches.length > 0 : matchedIndices.length > 0) && (
                <span className="flow-search-count">
                  {remoteLoading ? "查找中…" : `${activeMatchIndex + 1}/${historySessionId != null ? remoteMatches.length : matchedIndices.length}${remoteMore ? "+" : ""}`}
                </span>
              )}
              <Tooltip content="上一个">
                <button type="button" onClick={() => jumpMatch(-1)} disabled={historySessionId != null ? !remoteMatches.length : !matchedIndices.length} aria-label="上一个">
                  <IconChevronUp size={12} />
                </button>
              </Tooltip>
              <Tooltip content="下一个">
                <button type="button" onClick={() => jumpMatch(1)} disabled={historySessionId != null ? !remoteMatches.length : !matchedIndices.length} aria-label="下一个">
                  <IconChevronDown size={12} />
                </button>
              </Tooltip>
              <Tooltip content="关闭搜索">
                <button
                  type="button"
                  onClick={() => {
                    setSearchOpen(false);
                    setSearchKeyword("");
                  }}
                  aria-label="关闭搜索"
                >
                  <IconX size={12} />
                </button>
              </Tooltip>
              {remoteMore && historySessionId != null && <Button size="xs" loading={remoteLoading} onClick={() => {
                const sid = historySessionId, keyword = searchKeyword.trim();
                const signal = searchController.current?.signal;
                setRemoteLoading(true);
                void api.searchMessages(sid, keyword, historyThreadId, signal, remoteMatches.at(-1)?.id).then((result) => {
                  if (signal?.aborted) return;
                  setRemoteMatches((values) => [...values, ...result.items]); setRemoteMore(result.has_more);
                }).catch(() => {}).finally(() => { if (!signal?.aborted) setRemoteLoading(false); });
              }}>更多结果</Button>}
            </div>
          )}
        </div>
      )}

      {jumpDots && <JumpDots entries={entries} activeIndex={activeEntryIndex} onJump={(entry) => jumpToIndex(entries.indexOf(entry), "start")} />}

      {/* plan-282-1434（B6）：首次填充贴底完成前隐藏内容，避免看到"从顶部滚下来"的过程 */}
      <div
        ref={parentRef}
        tabIndex={0}
        aria-label={historyThreadId != null ? "子代理消息流" : "消息流"}
        className={"message-flow" + (positioned || totalCount === 0 ? "" : " is-positioning")}
        onScroll={onScroll}
      >
        {totalCount === 0 ? (
          /* plan-621-2572：会话消息为空时补充温和的操作引导副标题 */
          <Empty
            size="lg"
            title={emptyText}
            desc={search ? "在下方输入任务目标，或输入 @ 引用项目文件、/ 调用技能" : undefined}
          />
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
                  // plan-282-1444：主轴向位置（transform/top）由虚拟列表直接写 DOM。
                  // plan-633-2604：**top 的初值必须给 item.start**，不能再是 0。
                  //  directDomUpdates 的 applyDirectStyles 只遍历「当前 getVirtualItems()」内的元素，
                  //  某个元素若已挂载但本次不在该集合里（range 收缩 / 测量抖动 / 重入），
                  //  这一帧就不会被写位置 ⇒ 留在 top:0，与首项重叠成一团文字，
                  //  直到下一次重渲染把它卸载才恢复（这正是「停下滚动后文字重叠、再滚两下就好」）。
                  //  用 item.start 作锚点后，任何漏写的元素至少停在自己的正确位置。
                  style={{
                    position: "absolute",
                    top: item.start,
                    left: 0,
                    width: "100%",
                  }}
                >
                  {/* plan-282-1421（第11项）：搜索命中高亮（包裹层做文本节点标记，
                      不侵入 Markdown 结构；当前命中项用更强的 is-active 配色）。 */}
                  <HighlightedItem
                    keyword={searchKeyword}
                    active={isSearching && item.index === activeHighlightIndex}
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
  const sideScope = useContext(ConvoScopeContext);
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
  const sendSignal = useRef<UserSendSignal>({ sessionId: null, confirmed: 0, optimistic: 0, sequence: 0 });
  const userMsgSeq = useMemo(() => {
    sendSignal.current = advanceUserSendSignal(sendSignal.current, currentSessionId, messages);
    return sendSignal.current.sequence;
  }, [messages, currentSessionId]);

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
      historySessionId={sideScope == null ? currentSessionId : undefined}
      entries={entries}
      // 主代理未跑但工作流在跑时同样开启流式槽位（否则尾部「等待响应…」不渲染）
      running={isRunning || workflowBusy}
      renderEntry={renderEntry}
      streamingNode={
        // v42: 压缩期间状态行改显「压缩中…」（优先于「处理中…／等待响应…」；
        // 若同时存在 turn 级瞬态提示（如重试），瞬态提示仍优先）。
        <StreamingTail
          active={isRunning || workflowOnly}
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
  const storeMessages = useVisibleChat((s) => (threadId != null ? s.subagentMessages[threadId] : undefined) ?? EMPTY_SUBAGENT_MESSAGES, visible);
  // S8：子代理流式文本同样下沉到 <StreamingTail>（见其注释），此处不再订阅缓冲。
  const subagentMeta = useVisibleChat((s) => (threadId != null ? s.subagentMeta[threadId] : undefined), visible);
  // plan-75-334 阶段1：turn 行状态改由本层统一查询（与改造前每个 TurnGroup 各自查询语义一致）
  const turns = useScoped("turns");

  // v36 修复：与 SubagentPanel / SubagentCard 口径统一——后端存在 in_progress 状态，
  // 只判 running 会把运行中的面板当成终态（计时条消失、汇报卡片提前生效、运行态标记误判）。
  const isRunning = subagentMeta?.status === "running" || subagentMeta?.status === "in_progress";

  // plan-330-1648 M6: REST 历史回填——事件桶只覆盖“本次连接期间”收到的消息，
  // 重开面板/断线/切会话后会有缺口（用户反馈：面板内容不完整）。这里按 threadId 拉一次历史，
  // 与事件桶按 id 去重后并入（升序），不覆盖已有流式状态。
  useEffect(() => {
    if (!visible || threadId == null || currentSessionId == null) return;
    const requestedSid = currentSessionId;
    let cancelled = false;
    void (async () => {
      try {
        const result = await refreshMessageWindow(requestedSid, useChatStore.getState().subagentMessages[threadId] ?? [], threadId);
        if (cancelled) return;
        mergeHistoryIntoChat(requestedSid, result.items, threadId, result.checkedIds);
      } catch { /* 非阻塞：拉取失败就只用事件桶 */ }
    })();
    return () => { cancelled = true; };
  }, [threadId, currentSessionId, visible]);

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
    <FlowVisibilityContext.Provider value={visible}><MessageFlowCore
      key={threadId ?? "subagent-default"}
      historySessionId={currentSessionId}
      historyThreadId={threadId}
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
      search={visible}
      className={className}
      emptyText="子代理尚未产生输出"
    /></FlowVisibilityContext.Provider>
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
