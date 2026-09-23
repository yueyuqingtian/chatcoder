/** MessageFlow（v20 插件化重写）：消息流公共插件。
 * - source="main"（默认）：主会话全局 store 数据，全功能（虚拟化/搜索/JumpDots/计划卡/流式）。
 * - source="subagent"：子代理线程数据（threadId 读 store 桶 + REST 历史合并去重），
 *   窄面板排版，关闭 JumpDots/搜索/计划卡，操作仅复制。
 * 共享内核 MessageFlowCore：虚拟化 + 贴底滚动 + 跳底按钮 + 入场动画 + 搜索 + JumpDots，
 * 与参考项目「同一渲染引擎 + 数据注入」对齐——中间面板与右面板共用同一注册插件。
 */
import { useEffect, useLayoutEffect, useMemo, useRef, useState, useCallback, memo, type ReactNode } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import type { TimelineEntry, ToolNode, TurnItem } from "./timeline";
import { createTimelineBuilder, msgText, lastPersistedText } from "./timeline";
import { isBusy, subscribe } from "../../perf/bus";
import { registerReconcileTask, RECONCILE_ORDER } from "../../perf/reconcile";
import { TurnGroup } from "./TurnGroup";
import { JumpDots } from "./JumpDots";
import { DebugCard } from "./DebugCard";
import { StreamingTail } from "./StreamingTail";
import { IconSearch, IconChevronUp, IconChevronDown, IconX, IconArrowDown, IconAlertCircle } from "../icons";
import { useTextHighlight } from "../../hooks/useTextHighlight";
import { MarkdownContent } from "../MarkdownContent";
import { MsgType } from "@chatcoder/shared";
import { useChatStore } from "../../store/chat";
import { api, type MessageOut } from "../../api/client";

/** plan-31-152 S5-4：运动期（拖分隔条/窗口缩放）视口 rect 的提交节流间隔（毫秒）。
 *  逐帧提交会让虚拟器每帧 setState → 整棵可见消息树 React 重渲染（含 markdown/工具树），
 *  这是"拖动左右侧面板卡顿"的主因（空态首页无消息所以不卡）。取 ~120ms（约 8 帧一次）：
 *  既让虚拟器的可见范围跟上宽度变化，又把整树重渲染频率压到拖拽可接受的水平；
 *  运动结束由收敛序列（order 20）立即提交最终 rect 保证精确。 */
const MOTION_RECT_THROTTLE_MS = 120;

/** plan-308-1542 需求1：任务执行类错误的**唯一**棂位——消息流末尾错误卡。
 *  此前这类错误会同时写入 store.error（右上角 Toast）与消息流，造成重复报错；
 *  现在 sendTurn 失败 / turn.failed / 重试 / 回滚 / 审核失败等只走本卡。 */
const FlowErrorCard = memo(function FlowErrorCard({
  text, onRetry, onClose,
}: { text: string; onRetry?: () => void; onClose: () => void }) {
  return (
    <div className="turn-item turn-item-error flow-error-card">
      <IconAlertCircle size={15} className="err-icon" />
      <div className="err-body">
        <div className="err-title">任务执行出错</div>
        <div className="err-msg">{text}</div>
        <div className="flow-error-actions">
          {onRetry && (
            <button className="btn btn-ghost btn-xs" onClick={onRetry} type="button">重试</button>
          )}
          <button className="btn btn-ghost btn-xs" onClick={onClose} type="button">关闭</button>
        </div>
      </div>
    </div>
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
  if (it.kind === "tools") return "";
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
    /** 消息操作行能力：full=完整（赞踩/重试/回滚）；copy-only=仅复制；none=无操作行 */
    actions?: "full" | "copy-only" | "none";
  };
  /** 滚动目标：当外部点击任务卡时传 { turnId } 或 { threadId }，滚动到对应节点 */
  scrollTarget?: { threadId?: number; turnId?: number } | null;
  clearScrollTarget?: () => void;
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
  const parentRef = useRef<HTMLDivElement>(null);
  /** plan-547: 虚拟内容容器（RO 监听测高变化保持贴底） */
  // plan-282-1444：显式含 null 联合类型，保证 ref 可变（需在回调里同时交给虚拟列表）
  const innerRef = useRef<HTMLDivElement | null>(null);
  /** plan-282-1492：悬浮胶囊的底部占位块（高度 0 ↔ 44px 由 CSS 过渡驱动）。 */
  const capsuleSpaceRef = useRef<HTMLDivElement | null>(null);
  const scrollIdleTimerRef = useRef(0);
  const [autoScroll, setAutoScroll] = useState(true);
  /** autoScroll 的 ref 镜像：ResizeObserver 回调读取，避免每次回调 setState */
  const autoScrollRef = useRef(true);
  /** 用户接管标记（本轮优化）：用户在消息流内向上滑动（滚轮/触控板）后置位。
   *  接管期间——无论内容多快增长、条目如何增加——都不再把视图拉回底部；
   *  只有用户自己滚回贴底（距底 < 8px）才解除接管并恢复自动跟随。
   *  修复"上滑一点点 → 被 60px 贴底阈值立刻判回跟随 → 内容增长又拉回底部"的鬼畜循环。 */
  const userScrollOverrideRef = useRef(false);
  /** 程序补滚标记：补滚期间 onScroll 不翻转跟随状态 */
  const programmaticScrollRef = useRef(false);

  /** plan-282-1441（#2）：宽度变化时的"视口锚点"。
   *  拖拽面板分隔条会改变消息列宽度 → 文本重排 → 每条消息高度变化 → 虚拟列表重新测量
   *  → 总高度变化 → 同一个 scrollTop 对应的内容整体位移（"拖宽时消息位置漂移"）。
   *  这里在宽度变化时记录"首条可见项 + 其相对视口偏移"，待重测收敛后按锚点还原：
   *   - 原本贴底 → 仍然贴底（保持"我在最底部"的语义）；
   *   - 否则 → 让那条消息停在原来的视口位置（看哪条就停在哪条）。 */
  const widthAnchorRef = useRef<{ index: number; offset: number; atBottom: boolean } | null>(null);
  const lastWidthRef = useRef(0);
  const [showScrollBottom, setShowScrollBottom] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
  const [searchKeyword, setSearchKeyword] = useState("");
  const [activeMatchIndex, setActiveMatchIndex] = useState(0);
  /** 问题12: scrollspy——当前视口焦点 entry 下标，用于 JumpDots 自动聚焦对应小横条 */
  const [activeEntryIndex, setActiveEntryIndex] = useState(0);

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

  const virtualizer = useVirtualizer({
    count: totalCount,
    getScrollElement: () => parentRef.current,
    estimateSize: (index) => heightEstRef.current[estBucketOf(index)],
    // 仅在 scrollOffset 为 null（首帧）时消费：按"末项估算起点"初始化，避免从顶部渲染
    initialOffset: () =>
      totalCount > 0 ? Math.max(0, (totalCount - 1) * heightEstRef.current[estBucketOf(totalCount - 1)]) : 0,
    overscan,
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
  }, [virtualizer]);

  /* RFL-1（plan-329-1647 S4）：原本这里还有一个「冻结消息列内容宽度」的 effect
     （拖拽/窗口运动期间把 innerRef 的宽度钉成当前像素值，松手再解冻）。
     它确实降低了拖拽期的重排量，但代价是**排版不实时**：面板变宽后内容右侧留白、
     变窄则被裁切，直到松手才重新折行（用户反馈的问题）。现已删除：
       · 宽度实时跟随 → 文本逐帧折行（所见即所得）；
       · 拖拽期的重排成本改由「零布局读预算 + 帧闸门（RFL-2 / RFL-5）」控制；
       · 位置稳定交给浏览器原生滚动锚定 + 结束时的收敛序列（RFL-4 / RFL-6）。
     注：右面板内容根仍由 ResizeHandle.freezeRefs 冻结（见 App.tsx）。 */

  /** 贴底滚动。
   *  force=false（默认）：仅在"跟随态且用户未接管"时补滚，且不改变跟随状态——
   *    内容增长/条目增加触发的补滚不得复活被用户取消的跟随；
   *  force=true：用户主动要求贴底（切换会话 / 发起任务 / 发送消息 / 点"回到底部"），
   *    解除用户接管并恢复自动跟随。 */
  const scrollToBottom = useCallback((smooth = false, force = false) => {
    const el = parentRef.current;
    if (!el) return;
    if (!force && (!autoScrollRef.current || userScrollOverrideRef.current)) return;
    if (smooth) {
      el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
    } else {
      programmaticScrollRef.current = true;
      el.scrollTop = el.scrollHeight;
      // plan-547/v0.3.1 (对齐 v0.2.0)：双帧补滚——虚拟列表动态测量在渲染后才把总高度撑大，
      // 首帧撑开排版，次帧补齐 TanStack Virtual 测量差额坚决贴底（彻底删除 anchor 误杀拦截）
      requestAnimationFrame(() => {
        requestAnimationFrame(() => {
          const el2 = parentRef.current;
          // plan-238-1188: 补滚窗口内用户若已上滚（wheel 捕获即时置 autoScrollRef=false），
          // 放弃这次补滚——否则快速流式刷新时用户的上滚会被逐帧拉回底部（"划不动"）
          if (el2 && autoScrollRef.current && !userScrollOverrideRef.current) el2.scrollTop = el2.scrollHeight;
          programmaticScrollRef.current = false;
        });
      });
    }
    if (force) {
      userScrollOverrideRef.current = false;
      setAutoScroll(true);
      autoScrollRef.current = true;
      setShowScrollBottom(false);
    }
  }, []);

  /** 问题12: scrollspy——取视口上 1/3 焦点线所在虚拟项，映射为 entry 下标传给 JumpDots */
  const updateActiveEntry = useCallback((el: HTMLDivElement) => {
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


  const onScroll = useCallback(() => {
    const el = parentRef.current;
    if (el) {
      el.classList.add("is-scrolling");
      window.clearTimeout(scrollIdleTimerRef.current);
      scrollIdleTimerRef.current = window.setTimeout(() => {
        parentRef.current?.classList.remove("is-scrolling");
      }, 700);
    }
    // plan-547: 程序补滚产生的 scroll 事件不参与跟随判定
    if (programmaticScrollRef.current) return;
    if (!el) return;
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
    // 恢复 v0.2.0 贴底阈值 60px（抵抗高 DPI 缩放浮点舍入与末尾元素 margin/padding 波动）
    const isNearBottom = distance < 60;
    if (userScrollOverrideRef.current) {
      // 用户接管期间：只有真正滚回贴底（< 8px）才恢复跟随；否则"还差几十像素"的轻微
      // 上滑会被 isNearBottom(60px) 立刻判回跟随 → 内容增长再拉回底部（鬼畜）
      if (distance < 8) {
        userScrollOverrideRef.current = false;
        setAutoScroll(true);
        autoScrollRef.current = true;
      }
    } else {
      setAutoScroll(isNearBottom);
      autoScrollRef.current = isNearBottom;
    }
    setShowScrollBottom(distance > 120);
    scheduleSpy();
  }, [scheduleSpy]);

  /** plan-238-1188 + 本轮优化: 用户上滚意图必须"当帧生效"。
   *  思考内容快速刷新时，ResizeObserver / 双帧补滚都可能在 scroll 事件派发前
   *  把 scrollTop 拉回底部，表现为"消息流往上划不动"。
   *  在 wheel 捕获阶段同步关闭跟随（passive，不拦截默认滚动）并置"用户接管"标记：
   *  贴底状态下检测到向上滑动立即取消贴底；此后内容增长一律不打扰，
   *  直到用户滚回贴底由 onScroll 清除标记、自动刷新贴底跟随。 */
  useEffect(() => {
    const el = parentRef.current;
    if (!el) return;
    const onWheelCapture = (e: WheelEvent) => {
      if (e.deltaY < 0 && !userScrollOverrideRef.current) {
        userScrollOverrideRef.current = true;
        autoScrollRef.current = false;
        setAutoScroll(false);
      }
    };
    el.addEventListener("wheel", onWheelCapture, { passive: true, capture: true });
    return () => el.removeEventListener("wheel", onWheelCapture, { capture: true });
  }, []);

  // 滚动条自动隐藏的延时器不能在组件卸载后继续回写 DOM。
  useEffect(() => () => {
    window.clearTimeout(scrollIdleTimerRef.current);
    parentRef.current?.classList.remove("is-scrolling", "is-scrollbar-hover");
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

  /** plan-547: 内容总高度变化（虚拟测量/图片加载/展开）时若处于跟随态则保持贴底。
   *
   *  本轮优化（用户反馈"拖拉尺寸时内容重渲染的延迟很高"）：
   *  窗口/面板几何变化的每一帧，浏览器都要重排一次；而 `el.scrollHeight` 是**布局属性读取**，
   *  读取它会强制把此前的脏布局全部算完（forced synchronous layout）。把它放在逐帧的
   *  RO 回调里，等于每帧额外付一次全量重排，与"重排后每条消息高度变化 → 虚拟列表测量
   *  → 再次重排"叠加成两倍开销。
   *  现在运动期间只**记账**，几何落定后统一补一次贴底——中间帧本来就看不到底部变化。 */
  const hasContent = totalCount > 0;
  useEffect(() => {
    if (!hasContent) return;
    const inner = innerRef.current;
    const el = parentRef.current;
    if (!inner || !el || typeof ResizeObserver === "undefined") return;
    let missedStick = false;
    const inMotion = () => isBusy(); // PerfBus（S3）
    const stick = () => {
      if (!autoScrollRef.current || userScrollOverrideRef.current) return;
      el.scrollTop = el.scrollHeight;
    };
    const stickAfterMotion = () => {
      if (!missedStick) return;
      missedStick = false;
      stick();
    };
    const onMotionEnd = (e: Event) => {
      if ((e as CustomEvent<{ active?: boolean }>).detail?.active === false) stickAfterMotion();
    };
    const onPointerUp = () => stickAfterMotion();
    window.addEventListener("chatcoder:window-motion", onMotionEnd);
    window.addEventListener("pointerup", onPointerUp, true);
    const ro = new ResizeObserver(() => {
      // 问题4 回退：内容高度变化时若处于跟随态直接贴底；用户上滑已由 onScroll
      // 关闭 autoScroll（补滚窗口内则靠 scrollToBottom 的「用户已滚动则放弃」兜底），
      // 无需在 RO 内重复判定（内容增长后 scrollHeight 先变大，dist 判定会误关 autoScroll）。
      // 本轮补充：用户接管期间（已上滑）RO 也不得贴底，否则内容增长会把视图拉回底部。
      if (inMotion()) { missedStick = true; return; } // 运动中不读布局属性（见上方说明）
      stick();
    });
    ro.observe(inner);
    return () => {
      window.removeEventListener("chatcoder:window-motion", onMotionEnd);
      window.removeEventListener("pointerup", onPointerUp, true);
      ro.disconnect();
    };
  }, [hasContent]);

  /** plan-282-1492：胶囊占位块 0 ↔ 44px 的**高度过渡期间逐帧贴底**。
   *
   *  胶囊是悬浮元素（absolute，不占布局），出现前不预留、出现时靠占位块的高度过渡
   *  把消息流顶上去；高度过渡每帧都会触发 ResizeObserver 回调，正好用来逐帧把
   *  scrollTop 拉到 scrollHeight —— 于是"顶上去"的全过程都停在最底部、保持自动滚动。
   *  用户已上滑接管时一律不动（沿用全局接管协议）。 */
  useEffect(() => {
    const space = capsuleSpaceRef.current;
    const el = parentRef.current;
    if (!space || !el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => {
      // 本轮优化：几何运动中不读布局属性（scrollHeight 会强制同步布局），
      // 与高度 RO 同一口径——运动结束后由事件收尾统一补一次。
      if (isBusy()) return;
      if (!autoScrollRef.current || userScrollOverrideRef.current) return;
      el.scrollTop = el.scrollHeight;
    });
    ro.observe(space);
    return () => ro.disconnect();
  }, []);

  /** plan-282-1441（#2）：宽度变化 → 锚点补偿。
   *
   *  与上面"高度 RO"分工明确：
   *   - 高度 RO 负责"内容增长时贴底跟随"（跟随态语义）；
   *   - 本 effect 负责"**容器宽度变化**时保持用户当前看到的位置"（拖拽分隔条语义）。
   *
   *  必须在宽度变化**当帧**记录锚点（此时还没重排），否则记录到的偏移已被污染。 */
  useEffect(() => {
    const el = parentRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;

    const capture = () => {
      const items = virtualizer.getVirtualItems();
      const first = items[0];
      if (!first) return;
      widthAnchorRef.current = {
        index: first.index,
        // 首条可见项相对视口顶部的偏移：还原时用它把同一条放回原位
        offset: first.start - el.scrollTop,
        atBottom: el.scrollHeight - el.scrollTop - el.clientHeight < 60,
      };
    };

    const restore = () => {
      const anchor = widthAnchorRef.current;
      const el2 = parentRef.current;
      if (!anchor || !el2) return;
      programmaticScrollRef.current = true;
      if (anchor.atBottom) {
        el2.scrollTop = el2.scrollHeight;
      } else {
        // 按虚拟列表当前尺寸缓存找回同一条消息的起点，还原其视口偏移
        const offsetTop = virtualizer.getOffsetForIndex(anchor.index)?.[0];
        if (typeof offsetTop === "number") {
          el2.scrollTop = Math.max(0, offsetTop - anchor.offset);
        }
      }
      // 下一帧解除"程序滚动"标记，避免吞掉用户真实滚动
      requestAnimationFrame(() => {
        programmaticScrollRef.current = false;
      });
    };

    lastWidthRef.current = el.clientWidth;
    // plan-31-151 S4：锚点 capture 时机修正——拖拽期间 RO 回调里 capture 时布局已被改写，
    //   捕获到的是污染锚点。改为在 ResizeHandle 的 mousedown（拖拽开始前）派发的
    //   chatcoder:panel-drag-start 事件里捕获（此时布局尚未变）；窗口 resize 路径
    //   仍在 RO 回调第一帧捕获（此时是窗口几何驱动，布局同样尚未变）。
    let pendingRestore = 0;
    let missedDuringMotion = false;
    const inMotion = () => isBusy(); // PerfBus（S3）
    const scheduleRestore = () => {
      // 重排/重新测量在渲染后完成：双帧后再还原（与 scrollToBottom 的补滚节奏一致）。
      // 额外做 rAF 合并：连续宽度变化只保留最后一次还原，避免回调排队堆积。
      if (pendingRestore) cancelAnimationFrame(pendingRestore);
      pendingRestore = requestAnimationFrame(() => {
        pendingRestore = requestAnimationFrame(() => {
          pendingRestore = 0;
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
    // plan-31-151 S4：锚点收尾路径去重——删除 pointerup 捕获监听与 panel-drag-end 事件，
    //   只保留 reconcile order 30 一条路径；窗口 resize 路径仍走 chatcoder:window-motion。
    const offRect = registerReconcileTask("mf-virtualizer-rect", RECONCILE_ORDER.virtualizerRect,
      "提交虚拟器视口尺寸", () => settleRectRef.current?.());
    const offAnchor = registerReconcileTask("mf-scroll-anchor", RECONCILE_ORDER.scrollAnchor,
      "还原滚动锚点", () => settleAfterMotion());
    const onMotionEnd = (e: Event) => {
      const active = (e as CustomEvent<{ active?: boolean }>).detail?.active;
      if (active === false) settleAfterMotion();
    };
    window.addEventListener("chatcoder:window-motion", onMotionEnd);
    // plan-31-151 S4：拖拽开始前捕获锚点（ResizeHandle mousedown 派发，此时布局尚未变）。
    const onDragStart = () => {
      if (!inMotion()) return; // 非拖拽路径不捕获（窗口 resize 走 RO 第一帧）
      lastWidthRef.current = el.clientWidth;
      capture();
      missedDuringMotion = true;
    };
    window.addEventListener("chatcoder:panel-drag-start", onDragStart);
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
      offRect(); offAnchor();
      if (pendingRestore) cancelAnimationFrame(pendingRestore);
      window.removeEventListener("chatcoder:window-motion", onMotionEnd);
      window.removeEventListener("chatcoder:panel-drag-start", onDragStart);
      ro.disconnect();
    };
  }, [virtualizer]);

  /** 已做过"会话首次填充贴底"的会话标识（切换会话时重新进入首次填充分支） */
  const initSessionKeyRef = useRef<string | number | null>(null);
  /** plan-282-1444：首次定位的收敛轮询句柄（切会话/卸载时取消） */
  const settleRafRef = useRef(0);
  useLayoutEffect(() => {
    // v0.3.1: 长会话切换时优先将虚拟列表定位到最后一条（end），再执行贴底双帧补滚，
    // 彻底解决由于消息过多、初始估算高度误差导致切换会话后停在中间的 Bug。
    // 本轮修复: 原实现以 totalCount 为触发条件并无条件贴底 + 恢复跟随——任务执行期间
    // 条目持续增加（工具节点/注入/压缩卡）会把上滑中的用户反复拉回底部（"鬼畜"）。
    // 现在：仅"会话首次填充"强制贴底，后续条目增加只在跟随态（用户未接管）下补滚。
    //
    // plan-282-1434（B6）：首次填充是"点击会话"路径 —— 必须**直接呈现底部**，
    // 不能出现"先顶部、再滚下去"的过程。因此：
    //  - 定位使用非平滑滚动（scrollToIndex 默认 auto + scrollToBottom(smooth=false)）；
    //  - 定位收敛前用 .is-positioning 隐藏（见下方揭示逻辑）。
    if (totalCount === 0) return;
    if (initSessionKeyRef.current !== sessionKey) {
      initSessionKeyRef.current = sessionKey;
      setPositioned(false); // 进入新会话：先隐藏，待定位完成
      virtualizer.scrollToIndex(totalCount - 1, { align: "end" });
      scrollToBottom(false, true);

      /** plan-282-1444：揭示时机由"固定两帧"改为"测量收敛"。
       *  原来只等 2 帧就取消隐藏，但长会话里可见项的真实高度要经过多轮
       *  ResizeObserver → resizeItem 才逐步收敛；在仍按 estimateSize(140) 排版的那一帧
       *  揭示，就会看到"一部分消息重叠"，且随测量快慢时有时无（用户描述的"偶发、
       *  重进又找不到"）。现在：每帧持续补滚贴底，且只有总高连续 2 帧不变（收敛）
       *  才显示；10 帧 / 400ms 强制揭示兜底，不允许内容被永久隐藏。 */
      cancelAnimationFrame(settleRafRef.current);
      const startedAt = performance.now();
      let lastTotal = -1;
      let stableFrames = 0;
      let frames = 0;
      const settle = () => {
        const el = parentRef.current;
        const total = virtualizer.getTotalSize();
        stableFrames = total === lastTotal ? stableFrames + 1 : 0;
        lastTotal = total;
        frames += 1;
        // 用户在这段窗口内主动上滑（wheel 捕获即时置接管标记）：立即停止定位并揭示，
        // 不与他抢滚动（沿用全局"用户接管"协议）。
        if (userScrollOverrideRef.current) {
          settleRafRef.current = 0;
          setPositioned(true);
          return;
        }
        // 收敛过程中持续贴底：测量把高度撑开时不能停在中间
        if (el && autoScrollRef.current) el.scrollTop = el.scrollHeight;
        virtualizer.scrollToIndex(totalCount - 1, { align: "end" });
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
    if (!autoScrollRef.current || userScrollOverrideRef.current) return;
    virtualizer.scrollToIndex(totalCount - 1, { align: "end" });
    scrollToBottom(false);
  }, [sessionKey, scrollToBottom, totalCount, virtualizer]);

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
    if (running && !prevRunningRef.current) {
      scrollToBottom(false, true);
    }
    prevRunningRef.current = running;
  }, [running, scrollToBottom]);

  // 用户发送消息（时间线末尾新增"以用户消息开头"的新 turn）→ 无条件滚底，
  // 不受 autoScroll 影响（此前上滑过则发送后不滚底）
  const prevEntryLenRef = useRef(entries.length);
  useEffect(() => {
    const grew = entries.length > prevEntryLenRef.current;
    prevEntryLenRef.current = entries.length;
    if (!grew) return;
    const last = entries[entries.length - 1];
    const lastStartsUser = last != null && last.kind === "turn"
      && last.items.length > 0 && last.items[0].kind === "user";
    if (lastStartsUser) scrollToBottom(false, true);
  }, [entries, scrollToBottom]);

  // S8：流式增长 → 跟随态下贴底。改为**直接订阅 store**（不再经 props）：
  // 缓冲每帧换新引用，若作为 prop 传入会让整棵消息流每帧重渲染。
  // 这里只做一次轻量长度比较（数字），命中变化才 scrollToBottom，全程不触发 React 重渲染。
  useEffect(() => {
    // 问题4 回退：流式内容变化时在跟随态（autoScroll）下直接贴底。
    // autoScroll 由 onScroll（用户滚动）与补滚的「用户已滚动则放弃」判定维护——
    // 此处不另设距底判定，否则内容增长后 scrollHeight 先变大、scrollTop 未贴底的
    // 时序会误判 dist>60 而误关 autoScroll，导致"已在底部却不自动滚动"。
    const total = (map: Record<number, string> | undefined) => {
      let n = 0;
      if (map) for (const k in map) n += map[k].length;
      return n;
    };
    const measure = (s: ReturnType<typeof useChatStore.getState>) =>
      total(s.streamingBuffers) + total(s.thinkingBuffers)
      + total(s.subagentStreams) + total(s.subagentThinking);
    let last = measure(useChatStore.getState());
    return useChatStore.subscribe((s) => {
      const len = measure(s);
      if (len === last) return;
      last = len;
      if (autoScrollRef.current) scrollToBottom(false);
    });
  }, [scrollToBottom]);

  // v0.3.1: 内容整表替换（refreshMessages 等 REST 刷新/回滚/压缩）后同样贴底——
  // 依赖 entries 引用而非 length：REST 刷新常使 entries 重建但长度不变，
  // 旧实现（entries.length）不触发，刷新后 scrollTop 因虚拟测量/高度变化离底，
  // 表现为"刷新前在底部、刷新后有时在底部有时不在"。上滑用户（autoScroll=false）不受打扰。
  useEffect(() => {
    if (autoScroll) scrollToBottom(false);
  }, [entries, autoScroll, scrollToBottom]);

  // 本轮修复：强制贴底信号（"立即发送"后用户消息固定在时间线内，entries.length 不变，
  // 故由宿主下发信号保证"发送即滚到最新"）。
  const prevForceBottomRef = useRef(forceBottomKey);
  useEffect(() => {
    if (forceBottomKey === prevForceBottomRef.current) return;
    prevForceBottomRef.current = forceBottomKey;
    scrollToBottom(false, true);
  }, [forceBottomKey, scrollToBottom]);

  /** 问题12: 内容/滚动变化后刷新 scrollspy 焦点，保证 JumpDots 自动跟随 */
  useEffect(() => {
    const el = parentRef.current;
    if (el) updateActiveEntry(el);
    sampleHeights();
  }, [entries.length, updateActiveEntry, sampleHeights]);

  useEffect(() => {
    if (positioned) sampleHeights();
  }, [positioned, sampleHeights]);

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

  useEffect(() => {
    setActiveMatchIndex(0);
    if (matchedIndices.length > 0) {
      virtualizer.scrollToIndex(matchedIndices[0], { align: "center", behavior: "smooth" });
    }
  }, [matchedIndices, virtualizer]);

  const jumpMatch = (dir: 1 | -1) => {
    if (matchedIndices.length === 0) return;
    const next = (activeMatchIndex + dir + matchedIndices.length) % matchedIndices.length;
    setActiveMatchIndex(next);
    virtualizer.scrollToIndex(matchedIndices[next], { align: "center", behavior: "smooth" });
  };

  useEffect(() => {
    if (!scrollTarget) return;
    if (scrollTarget.turnId != null) {
      const idx = entries.findIndex((e) => e.kind === "turn" && e.turnId === scrollTarget.turnId);
      if (idx >= 0) {
        virtualizer.scrollToIndex(idx, { align: "start", behavior: "smooth" });
        clearScrollTarget?.();
      }
    }
  }, [scrollTarget, entries, virtualizer, clearScrollTarget]);

  return (
    <div className={`message-flow-outer ${className || ""}`}>
      {search && (
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

      {jumpDots && <JumpDots entries={entries} activeIndex={activeEntryIndex} onJump={(entry) => virtualizer.scrollToIndex(entries.indexOf(entry), { align: "start", behavior: "smooth" })} />}

      {/* plan-282-1434（B6）：首次填充贴底完成前隐藏内容，避免看到"从顶部滚下来"的过程 */}
      <div
        ref={parentRef}
        className={"message-flow" + (positioned || totalCount === 0 ? "" : " is-positioning")}
        onScroll={onScroll}
      >
        {totalCount === 0 ? (
          <div className="flow-empty">{emptyText}</div>
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
        {/* plan-282-1492：悬浮胶囊的底部安全区（高度过渡见 .message-flow-capsule-space）。
            未出现时高度 0（不预留空白），出现时过渡到 44px——胶囊"顶上去"消息流且不盖住末行。 */}
        <div ref={capsuleSpaceRef} className="message-flow-capsule-space" aria-hidden="true" />
      </div>

      {showScrollBottom && (
        <button
          type="button"
          className="flow-scroll-bottom-btn"
          onClick={() => scrollToBottom(true, true)}
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
}: {
  actions?: "full" | "copy-only" | "none";
  scrollTarget?: { threadId?: number; turnId?: number } | null;
  clearScrollTarget?: () => void;
  className?: string;
}) {
  const messages = useChatStore((s) => s.messages);
  const turns = useChatStore((s) => s.turns);
  const isRunning = useChatStore((s) => s.isRunning);
  const runningTurnId = useChatStore((s) => s.runningTurnId);
  // v39: 后台子代理运行数——主会话等待子代理时显示「等待子代理结束…」
  const pendingSubagents = useChatStore((s) => s.pendingSubagents);
  const currentSessionId = useChatStore((s) => s.currentSessionId);
  const subagentMeta = useChatStore((s) => s.subagentMeta);
  // S8（FlowEngine）：**不再**订阅 streamingBuffers / thinkingBuffers。
  // 缓冲每帧换新引用，此前会让整棵消息流每帧重渲染（运行期“更卡”的头号来源）。
  // 现在订阅下沉到 <StreamingTail>（只重渲染那一个组件），主树与 delta 解耦。
  // plan-95: 计划卡归属 turn——卡片内嵌到该 turn 行尾随时间线滚动，
  // 不再固定在消息流最底部（与后续新消息脱节）
  const planTurnId = useChatStore((s) => s.pendingPlan?.turnId ?? null);
  // v30: 压缩中状态（compact.started 载荷）——v42: 不再在消息流尾部渲染「正在压缩上下文」
  // 统计卡片（用户反馈与状态行重复、样式突兀），改为把流式状态行文案从「处理中…」切换为「压缩中…」。
  const isCompacting = useChatStore((s) => s.isCompacting);
  // plan-282-1441（#8）：调试现场——AI 调试试过程中在消息流尾部展示“停在哪一行”。
  // 这是“用户能看到断点进行到哪一行代码”在消息流侧的落点（另一处在调试面板）。
  const debugState = useChatStore((s) => s.debugState);
  const activeDebug = useMemo(() => {
    for (const t of ["web", "java"] as const) {
      const st = debugState?.[t];
      if (st?.connected) return st;
    }
    return null;
  }, [debugState]);

  const subagentsByTurn = useMemo(() => {
    // plan-330-1648 M7: 透传 error——卡片/面板据此展示失败原因（不再只有一个红叉）
    const map = new Map<number, Array<{ agentId: number; name: string; status: string; error?: string | null }>>();
    for (const [aid, m] of Object.entries(subagentMeta)) {
      if (m.turnId == null) continue;
      const list = map.get(m.turnId) ?? [];
      list.push({ agentId: Number(aid), name: m.name, status: m.status, error: m.error ?? null });
      map.set(m.turnId, list);
    }
    return map;
  }, [subagentMeta]);

  // v42 → 本轮修复：注入消息（"立即发送"）**固定留在时间线内**渲染。
  //
  // 旧实现（v42/v41）把它从 entries 剥离、渲染到流式段**下方的独立槽位**，
  // 导致用户反馈"发送的消息没有固定位置，任务刷新消息时被刷到下方"：
  // 流式内容每长高一截，槽位就往下走一截；turn 结束槽位消失，它又回归时间线，
  // 位置再跳一次。
  //
  // 现在只保留"跨界段前移"这一必要修正：
  //  - 注入时刻正在流式的段（尚未落库）落库后 id 会大于注入消息，按 id 序会错误地
  //    掉到注入消息下方；将其前移到对应注入消息之前 —— 注入消息成为时间分割点
  //    （上方 = 注入前内容，下方 = 注入后新输出）。
  //  - 注入消息本身按 id 序自然落位：它在注入前内容之后、注入后新输出之前，固定不动。
  // 结合 TurnGroup 对非首条 user item 的"就地渲染在时间序位置"，视觉与旧槽位一致。
  const injectMarks = useChatStore((s) => s.injectMarks);

  const timelineMessages = useMemo(() => {
    // 跨界段 -> 前移目标注入消息（同一跨界段绑定多条注入时取最小 injectId）
    const crossoverTarget = new Map<number, number>();
    for (const mk of injectMarks) {
      if (mk.crossoverId == null) continue;
      const prev = crossoverTarget.get(mk.crossoverId);
      if (prev == null || mk.injectId < prev) crossoverTarget.set(mk.crossoverId, mk.injectId);
    }
    if (crossoverTarget.size === 0) return messages;
    const msgById = new Map(messages.map((m) => [m.id, m]));
    // 注入消息 -> 前移插入的跨界段列表（按 id 升序）
    const crossoversByTarget = new Map<number, MessageOut[]>();
    for (const crossoverId of crossoverTarget.keys()) {
      const msg = msgById.get(crossoverId);
      if (!msg) continue;
      const target = crossoverTarget.get(crossoverId)!;
      const list = crossoversByTarget.get(target) ?? [];
      list.push(msg);
      crossoversByTarget.set(target, list);
    }
    for (const list of crossoversByTarget.values()) list.sort((a, b) => a.id - b.id);
    const out: MessageOut[] = [];
    for (const m of messages) {
      if (crossoverTarget.has(m.id)) continue;
      const cs = crossoversByTarget.get(m.id);
      if (cs) out.push(...cs);
      out.push(m);
    }
    return out;
  }, [messages, injectMarks]);

  // v30: 被压缩的消息保留在时间线上（不隐藏）；压缩块卡由 SUMMARY 消息渲染。
  // S8b：改用增量构建器——单条消息落库只重建它所在的那个 turn，其余 turn 的 entry
  // 对象逐项复用（引用不变）⇒ 下游 memo 的 TurnGroup 继续命中，不再全量重渲染。
  const buildEntries = useMemo(() => createTimelineBuilder(), []);
  const entries = useMemo(() => buildEntries(timelineMessages), [buildEntries, timelineMessages]);

  /** 强制贴底信号：用户消息条数。
   *
   *  为什么需要：注入消息现在固定留在时间线内，它落进的是**已存在的** turn entry，
   *  entries.length 与末尾 entry 形态都不变 ⇒ MessageFlowCore 里"末尾新增以用户消息
   *  开头的新 turn → 滚底"的判定不会触发，点了「立即发送」或发新消息后视图不会跟到底。
   *  这里用"用户消息条数"作信号：每新增一条用户消息（含注入与乐观占位）即 +1，
   *  核心层据此无条件贴底——语义精确且不会因流式内容变化而误触发。 */
  const userMsgSeq = useMemo(
    () => timelineMessages.reduce((n, m) => (m.sender_type === "user" ? n + 1 : n), 0),
    [timelineMessages],
  );

  const plansByTurn = useChatStore((s) => s.plansByTurn);

  const renderEntry = useCallback(
    (entry: TimelineEntry) => {
      if (entry.kind !== "turn") return <StandaloneEntry entry={entry} />;
      // v12: 已回滚 turn 显示专用横幅（回滚后消息被软删，以此占位区分「回滚了」与「没执行」）
      const rolledBack = turns.find((t) => t.id === entry.turnId)?.status === "rolled_back";
      // 计划卡内嵌到其归属 turn 内部规划说明之后、执行操作之前（彻底根治时序倒挂沉底 Bug）
      const hasTurnPlan = entry.turnId != null && (plansByTurn[entry.turnId] != null || entry.turnId === planTurnId);
      return (
        <TurnGroup
          entry={entry}
          isRunning={runningTurnId === entry.turnId}
          rolledBack={rolledBack}
          subagents={entry.turnId != null ? subagentsByTurn.get(entry.turnId) : undefined}
          actions={actions}
          hasPlan={hasTurnPlan}
        />
      );
    },
    [turns, runningTurnId, subagentsByTurn, planTurnId, plansByTurn, actions]
  );

  // S8：流式文本/信号不再在此计算（已下沉到 <StreamingTail> 的精确 selector）。
  const turnStatus = useChatStore((s) => s.turnStatus);
  // plan-308-1542 需求1：任务执行类错误卡（只进消息流，不弹右上角 Toast）
  const flowError = useChatStore((s) => s.flowError);
  const clearFlowError = useChatStore((s) => s.clearFlowError);

  return (
    <MessageFlowCore
      key={currentSessionId}
      entries={entries}
      running={isRunning}
      renderEntry={renderEntry}
      streamingNode={
        // v42: 压缩期间状态行改显「压缩中…」（优先于「处理中…／等待响应…」；
        // 若同时存在 turn 级瞬态提示（如重试），瞬态提示仍优先）。
        <StreamingTail
          active={Boolean(isRunning && runningTurnId)}
          statusLabel={turnStatus ?? (isCompacting ? "压缩中…" : undefined)}
          persistedText={lastPersistedText(entries)}
        />
      }
      trailingNode={
        // v39: 后台子代理运行期间主会话保持“运行中”——主 turn 未跑但有子代理在跑时，
        // 用与流式状态行同款样式显示「等待子代理结束…」（完成后服务端自动唤醒新一轮）。
        !isRunning && pendingSubagents > 0 ? <SubagentWaitingLine count={pendingSubagents} />
          : activeDebug ? <DebugCard status={activeDebug} />
          // plan-308-1542 需求1：任务执行类错误统一在消息流末尾报（不弹右上角）
          : flowError ? (
            <FlowErrorCard
              text={flowError.text}
              onRetry={() => {
                // 重试 = 重发最近一条用户消息（若可定位），否则仅清除提示
                const lastUser = [...timelineMessages].reverse().find((m) => m.sender_type === "user");
                if (lastUser) void useChatStore.getState().sendTurn(msgText(lastUser.content));
              }}
              onClose={() => clearFlowError()}
            />
          )
          : null
      }
      forceBottomKey={userMsgSeq}
      sessionKey={currentSessionId ?? 0}
      jumpDots
      search
      scrollTarget={scrollTarget}
      clearScrollTarget={clearScrollTarget}
      className={className}
      emptyText="选择或创建会话，开始你的任务"
    />
  );
}

/** 子代理数据源（source="subagent"）：store 消息桶 + REST 历史合并去重，流式尾部用线程缓冲。 */
function SubagentMessageFlow({
  threadId,
  actions,
  className,
}: {
  threadId?: number;
  actions?: "full" | "copy-only" | "none";
  className?: string;
}) {
  const currentSessionId = useChatStore((s) => s.currentSessionId);
  const storeMessages = useChatStore((s) => (threadId != null ? s.subagentMessages[threadId] || [] : []));
  // S8：子代理流式文本同样下沉到 <StreamingTail>（见其注释），此处不再订阅缓冲。
  const subagentMeta = useChatStore((s) => (threadId != null ? s.subagentMeta[threadId] : undefined));

  // v36 修复：与 SubagentPanel / SubagentCard 口径统一——后端存在 in_progress 状态，
  // 只判 running 会把运行中的面板当成终态（计时条消失、汇报卡片提前生效、运行态标记误判）。
  const isRunning = subagentMeta?.status === "running" || subagentMeta?.status === "in_progress";

  // plan-330-1648 M6: REST 历史回填——事件桶只覆盖“本次连接期间”收到的消息，
  // 重开面板/断线/切会话后会有缺口（用户反馈：面板内容不完整）。这里按 threadId 拉一次历史，
  // 与事件桶按 id 去重后并入（升序），不覆盖已有流式状态。
  useEffect(() => {
    if (threadId == null || currentSessionId == null) return;
    let cancelled = false;
    void (async () => {
      try {
        const fetched = await api.listSessionMessages(currentSessionId, threadId);
        if (cancelled || fetched.length === 0) return;
        useChatStore.setState((s) => {
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

  // v36 (plan-321-1600 M2): 末条非空 text 消息 = 子代理的结构化汇报，交结构化卡片渲染。
  // plan-330-1648 M6: **仅在子代理终态启用**——运行中每落一段新文本都会改写“最后一条
  // text”的判定，使面板在“结构化卡片 ↔ 普通 markdown”之间反复切换（用户反馈问题3：
  // 先正常排版、随后内容消失又重新输出）。
  const reportMessageId = useMemo(() => {
    if (isRunning || !subagentMeta) return undefined;
    for (let i = storeMessages.length - 1; i >= 0; i--) {
      const m = storeMessages[i];
      if (m.msg_type === "text" && msgText(m.content).trim()) return m.id;
    }
    return undefined;
  }, [storeMessages, isRunning, subagentMeta]);

  const renderEntry = useCallback(
    (entry: TimelineEntry) => {
      if (entry.kind !== "turn") return <StandaloneEntry entry={entry} />;
      return (
        <TurnGroup
          entry={entry}
          isRunning={isRunning}
          actions={actions || "copy-only"}
          flow="subagent"
          agentId={threadId ?? undefined}
          reportMessageId={reportMessageId}
        />
      );
    },
    [isRunning, actions, reportMessageId, threadId]
  );


  return (
    <MessageFlowCore
      key={threadId ?? "subagent-default"}
      entries={entries}
      running={isRunning}
      renderEntry={renderEntry}
      streamingNode={
        <StreamingTail
          source={threadId ?? -1}
          active={isRunning}
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
        className={props.className}
      />
    );
  }
  return (
    <MainMessageFlow
      actions={props.features?.actions ?? "full"}
      scrollTarget={props.scrollTarget}
      clearScrollTarget={props.clearScrollTarget}
      className={props.className}
    />
  );
}
