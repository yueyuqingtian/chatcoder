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
import { buildTimeline, msgText } from "./timeline";
import { TurnGroup } from "./TurnGroup";
import { JumpDots } from "./JumpDots";
import { CompactingCard } from "./CompactCard";
import { StreamingText } from "./StreamingText";
import { IconSearch, IconChevronUp, IconChevronDown, IconX, IconArrowDown } from "../icons";
import { MarkdownContent } from "../MarkdownContent";
import { MsgType } from "@chatcoder/shared";
import { useChatStore } from "../../store/chat";
import type { MessageOut } from "../../api/client";
import { AttachmentCard, attachmentsOf } from "./AttachmentCard";

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
          <MarkdownContent>{msgText(entry.msg.content)}</MarkdownContent>
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
  /** v41: 运行中注入的用户消息（"立即发送"），占流式段之后的槽位--时间直觉上位于实时内容下方 */
  injectedNode?: ReactNode | null;
  /** 额外尾部（如压缩中卡片），占最后一项 */
  trailingNode?: ReactNode | null;
  /** 会话标识：变化时强制跳底（主界面传 currentSessionId，子代理传 threadId） */
  sessionKey: string | number;
  /** 流式信号：内容变化时贴底跟随（避免整对象依赖；v40 允许数字——缓冲长度即可） */
  streamSignal: string | number;
  /** 功能开关（默认按模式由外层传入） */
  jumpDots?: boolean;
  search?: boolean;
  /** 滚动目标（主界面任务卡点击穿透 / turn 导航） */
  scrollTarget?: { threadId?: number; turnId?: number } | null;
  clearScrollTarget?: () => void;
  className?: string;
  emptyText?: string;
}

function MessageFlowCore({
  entries,
  running,
  renderEntry,
  streamingNode,
  injectedNode,
  trailingNode,
  sessionKey,
  streamSignal,
  jumpDots = true,
  search = true,
  scrollTarget,
  clearScrollTarget,
  className,
  emptyText = "暂无消息",
}: MessageFlowCoreProps) {
  const parentRef = useRef<HTMLDivElement>(null);
  /** plan-547: 虚拟内容容器（RO 监听测高变化保持贴底） */
  const innerRef = useRef<HTMLDivElement>(null);
  const [autoScroll, setAutoScroll] = useState(true);
  /** autoScroll 的 ref 镜像：ResizeObserver 回调读取，避免每次回调 setState */
  const autoScrollRef = useRef(true);
  /** 程序补滚标记：补滚期间 onScroll 不翻转跟随状态 */
  const programmaticScrollRef = useRef(false);
  const [showScrollBottom, setShowScrollBottom] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
  const [searchKeyword, setSearchKeyword] = useState("");
  const [activeMatchIndex, setActiveMatchIndex] = useState(0);
  /** 问题12: scrollspy——当前视口焦点 entry 下标，用于 JumpDots 自动聚焦对应小横条 */
  const [activeEntryIndex, setActiveEntryIndex] = useState(0);

  const hasStreaming = Boolean(running && streamingNode);
  const hasInjected = Boolean(injectedNode);
  const hasTrailing = Boolean(trailingNode);
  const totalCount =
    entries.length + (hasStreaming ? 1 : 0) + (hasInjected ? 1 : 0) + (hasTrailing ? 1 : 0);

  const virtualizer = useVirtualizer({
    count: totalCount,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 140,
    overscan: 6,
  });

  const scrollToBottom = useCallback((smooth = false) => {
    const el = parentRef.current;
    if (!el) return;
    if (smooth) {
      el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
    } else {
      programmaticScrollRef.current = true;
      el.scrollTop = el.scrollHeight;
      // plan-547: 双帧补滚——虚拟列表动态测量在渲染后才把总高度撑大，
      // 立即设置的 scrollTop 会"离底"，复查两帧保证贴底（消除滚动往返抖动）
      requestAnimationFrame(() => {
        requestAnimationFrame(() => {
          const el2 = parentRef.current;
          if (!el2) { programmaticScrollRef.current = false; return; }
          // 问题4: 补滚窗口内用户已滚动（scrollTop 偏离贴底值）→ 放弃补滚，尊重用户上滑
          if (el2.scrollTop !== el2.scrollHeight) {
            programmaticScrollRef.current = false;
            autoScrollRef.current = false;
            setAutoScroll(false);
            return;
          }
          el2.scrollTop = el2.scrollHeight;
          programmaticScrollRef.current = false;
        });
      });
    }
    setAutoScroll(true);
    autoScrollRef.current = true;
    setShowScrollBottom(false);
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

  const onScroll = useCallback(() => {
    // plan-547: 程序补滚产生的 scroll 事件不参与跟随判定
    if (programmaticScrollRef.current) return;
    const el = parentRef.current;
    if (!el) return;
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
    const isNearBottom = distance < 60;
    setAutoScroll(isNearBottom);
    autoScrollRef.current = isNearBottom;
    setShowScrollBottom(distance > 120);
    updateActiveEntry(el);
  }, [updateActiveEntry]);

  /** plan-547: 内容总高度变化（虚拟测量/图片加载/展开）时若处于跟随态则保持贴底 */
  const hasContent = totalCount > 0;
  useEffect(() => {
    if (!hasContent) return;
    const inner = innerRef.current;
    const el = parentRef.current;
    if (!inner || !el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => {
      if (!autoScrollRef.current) return;
      // 问题4: 内容变化但用户已滚离底部（距底 > 60px，与 onScroll 判定同口径）→
      // 不再强制贴底，尊重用户上滑（此前 autoScrollRef 未及时翻转导致被拽回抖动）
      const dist = el.scrollHeight - el.scrollTop - el.clientHeight;
      if (dist > 60) {
        autoScrollRef.current = false;
        setAutoScroll(false);
        return;
      }
      el.scrollTop = el.scrollHeight;
    });
    ro.observe(inner);
    return () => ro.disconnect();
  }, [hasContent]);

  useLayoutEffect(() => {
    scrollToBottom(false);
  }, [sessionKey, scrollToBottom]);

  useEffect(() => {
    // 问题4: 流式内容变化时仅当用户确实位于底部附近才贴底；若 autoScroll 状态滞后
    // 仍为 true 而用户已滚离（距底 > 60px），立即识别并停止贴底，避免被拽回抖动
    if (!autoScroll) return;
    const el = parentRef.current;
    if (!el) return;
    const dist = el.scrollHeight - el.scrollTop - el.clientHeight;
    if (dist > 60) {
      autoScrollRef.current = false;
      setAutoScroll(false);
      return;
    }
    scrollToBottom(false);
  }, [streamSignal, autoScroll, scrollToBottom]);

  useEffect(() => {
    if (autoScroll) scrollToBottom(false);
  }, [entries.length, autoScroll, scrollToBottom]);

  /** 问题12: 内容/滚动变化后刷新 scrollspy 焦点，保证 JumpDots 自动跟随 */
  useEffect(() => {
    const el = parentRef.current;
    if (el) updateActiveEntry(el);
  }, [entries.length, updateActiveEntry]);

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

      <div ref={parentRef} className="message-flow" onScroll={onScroll}>
        {totalCount === 0 ? (
          <div className="flow-empty">{emptyText}</div>
        ) : (
          <div
            ref={innerRef}
            className="message-flow-virtual-inner"
            style={{
              height: `${virtualizer.getTotalSize()}px`,
              position: "relative",
              width: "100%",
            }}
          >
            {virtualizer.getVirtualItems().map((item) => {
              // v41 槽位顺序：已落库 entries -> 流式段 -> 注入用户消息 -> 尾部卡片
              const streamStart = entries.length;
              const injectedStart = streamStart + (hasStreaming ? 1 : 0);
              const trailingStart = injectedStart + (hasInjected ? 1 : 0);
              const isStreamSlot = hasStreaming && item.index === streamStart;
              const isInjectedSlot = hasInjected && item.index === injectedStart;
              const isTrailingSlot = hasTrailing && item.index === trailingStart;
              let node: ReactNode;
              if (isStreamSlot) {
                node = streamingNode;
              } else if (isInjectedSlot) {
                node = injectedNode;
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
                  style={{
                    position: "absolute",
                    top: 0,
                    left: 0,
                    width: "100%",
                    transform: `translateY(${item.start}px)`,
                  }}
                >
                  <div className="mf-list">{node}</div>
                </div>
              );
            })}
          </div>
        )}
      </div>

      {showScrollBottom && (
        <button
          type="button"
          className="flow-scroll-bottom-btn"
          onClick={() => scrollToBottom(true)}
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
  const currentSessionId = useChatStore((s) => s.currentSessionId);
  const subagentMeta = useChatStore((s) => s.subagentMeta);
  const streamingBuffers = useChatStore((s) => s.streamingBuffers);
  const thinkingBuffers = useChatStore((s) => s.thinkingBuffers);
  // plan-95: 计划卡归属 turn——卡片内嵌到该 turn 行尾随时间线滚动，
  // 不再固定在消息流最底部（与后续新消息脱节）
  const planTurnId = useChatStore((s) => s.pendingPlan?.turnId ?? null);
  // v30: 压缩中进度（compact.started 载荷）——消息流尾部渲染"压缩中"卡片
  const isCompacting = useChatStore((s) => s.isCompacting);
  const compactingInfo = useChatStore((s) => s.compactingInfo);

  const subagentsByTurn = useMemo(() => {
    const map = new Map<number, Array<{ agentId: number; name: string; status: string }>>();
    for (const [aid, m] of Object.entries(subagentMeta)) {
      if (m.turnId == null) continue;
      const list = map.get(m.turnId) ?? [];
      list.push({ agentId: Number(aid), name: m.name, status: m.status });
      map.set(m.turnId, list);
    }
    return map;
  }, [subagentMeta]);

  // v42: 注入分割渲染（"立即发送"的消息是时间分割点）：
  // - pending 注入（注入时刻正在流式的段尚未落库）：注入消息剥离出时间线，
  //   渲染到流式段之后--消息流是两段式（已落库 entries + 未落库流式段），
  //   流式段固定在 entries 之后，留在 entries 内会显示在实时内容上方；
  // - 跨界段（注入时刻流式中、之后落库的消息，mark.crossoverId）：前移到
  //   对应注入消息之前--其 id 大于注入消息，按 id 序会错误地掉到注入下方。
  // 其余消息按 id 序：注入前内容天然在注入上方，注入后新刷新的工具调用
  // 与消息天然在注入下方。turn 结束后标记保留（顺序不跳变），新 turn 开始清空。
  const injectMarks = useChatStore((s) => s.injectMarks);

  // pending 注入仅运行中生效：turn 结束后流式槽消失，注入消息回归时间线
  // id 序位置（跨界段已有 crossoverId 绑定的仍前移，顺序不跳变）
  const pendingInjectIds = useMemo(
    () =>
      new Set(
        isRunning
          ? injectMarks
              .filter((mk) => mk.crossoverId == null && mk.pendingAgents.length > 0)
              .map((mk) => mk.injectId)
          : [],
      ),
    [injectMarks, isRunning],
  );

  const timelineMessages = useMemo(() => {
    // 跨界段 -> 前移目标注入消息（同一跨界段绑定多条注入时取最小 injectId）
    const crossoverTarget = new Map<number, number>();
    for (const mk of injectMarks) {
      if (mk.crossoverId == null) continue;
      const prev = crossoverTarget.get(mk.crossoverId);
      if (prev == null || mk.injectId < prev) crossoverTarget.set(mk.crossoverId, mk.injectId);
    }
    if (pendingInjectIds.size === 0 && crossoverTarget.size === 0) return messages;
    const msgById = new Map(messages.map((m) => [m.id, m]));
    // 目注入消息 -> 前移插入的跨界段列表（按 id 升序）
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
      if (pendingInjectIds.has(m.id) || crossoverTarget.has(m.id)) continue;
      const cs = crossoversByTarget.get(m.id);
      if (cs) out.push(...cs);
      out.push(m);
    }
    return out;
  }, [messages, injectMarks, pendingInjectIds]);

  // pending 注入消息（跨界段还在流式）：渲染在流式段之后的独立槽位；
  // turn 结束（isRunning=false）后回归时间线（流式槽已消失，无需让位）
  const injectedMsgs = useMemo(
    () => (isRunning ? messages.filter((m) => pendingInjectIds.has(m.id)) : []),
    [messages, pendingInjectIds, isRunning],
  );

  // v30: 被压缩的消息保留在时间线上（不隐藏）；压缩块卡由 SUMMARY 消息渲染
  const entries = useMemo(() => buildTimeline(timelineMessages), [timelineMessages]);

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
          planAnchorMsgId={entry.turnId != null ? plansByTurn[entry.turnId]?.anchorMsgId ?? null : null}
        />
      );
    },
    [turns, runningTurnId, subagentsByTurn, planTurnId, plansByTurn, actions]
  );

  // v40: 流式信号只取缓冲长度（数字），避免每次渲染把全量流式文本 join 成大字符串造成 GC 抖动
  const streamSignal = useMemo(
    () =>
      Object.values(streamingBuffers).reduce((n, s) => n + s.length, 0) +
      Object.values(thinkingBuffers).reduce((n, s) => n + s.length, 0),
    [streamingBuffers, thinkingBuffers]
  );
  const thinkingText = Object.values(thinkingBuffers).join("").trim();
  const text = Object.values(streamingBuffers).join("");
  const turnStatus = useChatStore((s) => s.turnStatus);

  return (
    <MessageFlowCore
      entries={entries}
      running={isRunning}
      renderEntry={renderEntry}
      streamingNode={
        <StreamingText
          active={Boolean(isRunning && runningTurnId)}
          thinking={thinkingText}
          text={text}
          statusLabel={turnStatus ?? undefined}
        />
      }
      injectedNode={
        injectedMsgs.length > 0 ? (
          <div className="turn-group">
            {injectedMsgs.map((m) => (
              <div key={m.id} className="turn-item turn-item-user">
                <div className="turn-user-bubble">
                  {msgText(m.content) && <div className="turn-user-text">{msgText(m.content)}</div>}
                  {attachmentsOf(m.content).map((a) => (
                    <AttachmentCard key={a.file_id || a.url} att={a} />
                  ))}
                </div>
              </div>
            ))}
          </div>
        ) : null
      }
      trailingNode={isCompacting ? <CompactingCard info={compactingInfo} /> : null}
      sessionKey={currentSessionId ?? 0}
      streamSignal={streamSignal}
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
  const thinkingBuffer = useChatStore((s) => (threadId != null ? s.subagentThinking[threadId] || "" : ""));
  const streamingBuffer = useChatStore((s) => (threadId != null ? s.subagentStreams[threadId] || "" : ""));
  const subagentMeta = useChatStore((s) => (threadId != null ? s.subagentMeta[threadId] : undefined));

  const isRunning = subagentMeta?.status === "running";

  // 将子代理消息按 timeline 构建（子代理消息通常是平铺的 tool/text/thinking）
  const entries = useMemo(() => buildTimeline(storeMessages), [storeMessages]);

  const renderEntry = useCallback(
    (entry: TimelineEntry) => {
      if (entry.kind !== "turn") return <StandaloneEntry entry={entry} />;
      return <TurnGroup entry={entry} isRunning={isRunning} actions={actions || "copy-only"} />;
    },
    [isRunning, actions]
  );

  const streamSignal = (thinkingBuffer?.length || 0) + (streamingBuffer?.length || 0);

  return (
    <MessageFlowCore
      entries={entries}
      running={isRunning}
      renderEntry={renderEntry}
      streamingNode={
        <StreamingText
          active={isRunning}
          thinking={thinkingBuffer}
          text={streamingBuffer}
          statusLabel={isRunning ? "子代理执行中…" : undefined}
        />
      }
      sessionKey={`${currentSessionId ?? 0}:${threadId ?? 0}`}
      streamSignal={streamSignal}
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
