/** MessageFlow（v20 插件化重写）：消息流公共插件。
 * - source="main"（默认）：主会话全局 store 数据，全功能（虚拟化/搜索/JumpDots/计划卡/流式）。
 * - source="subagent"：子代理线程数据（threadId 读 store 桶 + REST 历史合并去重），
 *   窄面板排版，关闭 JumpDots/搜索/计划卡，操作仅复制。
 * 共享内核 MessageFlowCore：虚拟化 + 贴底滚动 + 跳底按钮 + 入场动画 + 搜索 + JumpDots，
 * 与参考项目「同一渲染引擎 + 数据注入」对齐——中间面板与右面板共用同一注册插件。
 */
import { useEffect, useLayoutEffect, useMemo, useRef, useState, useCallback, memo, type ReactNode } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import type { TimelineEntry } from "./timeline";
import { buildTimeline, msgText } from "./timeline";
import { TurnGroup } from "./TurnGroup";
import { JumpDots } from "./JumpDots";
import { CompactingCard } from "./CompactCard";
import { StreamingText } from "./StreamingText";
import { IconSearch, IconChevronUp, IconChevronDown, IconX, IconArrowDown, IconClipboard } from "../icons";
import { MarkdownContent } from "../MarkdownContent";
import { MsgType } from "@chatcoder/shared";
import { useChatStore } from "../../store/chat";
import { usePanelStore } from "../../store/panel";
import { api, type MessageOut } from "../../api/client";

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
  /** 容器附加类名（子代理面板窄版适配） */
  className?: string;
}

const StandaloneEntry = memo(function StandaloneEntry({ entry }: { entry: Extract<TimelineEntry, { kind: "standalone" }> }) {
  // 系统消息（模型切换 divider 等，前方没有任何 turn 时落到这里）：渲染为分割线
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

/** 计划确认卡（主界面专属）：plan 模式 turn 完成后内嵌在其所属 turn 行尾展示。 */
function PlanCard() {
  const [expanded, setExpanded] = useState(false);
  const pendingPlan = useChatStore((s) => s.pendingPlan);
  const pendingSplit = useChatStore((s) => s.pendingSplit);
  const tasks = useChatStore((s) => s.tasks);
  const currentSessionId = useChatStore((s) => s.currentSessionId);
  const confirmPlan = useChatStore((s) => s.confirmPlan);
  const confirmTaskSplit = useChatStore((s) => s.confirmTaskSplit);
  const dismissPlan = useChatStore((s) => s.dismissPlan);
  const splitSteps = pendingSplit
    ? tasks.filter((task) => task.parent_task_id === pendingSplit.groupTaskId && !task.is_hidden)
    : [];
  if (!pendingPlan && !pendingSplit) return null;
  // plan-95: 展示守卫——提案组已不是 proposed（已确认/已取消）时不渲染，
  // 防止 tasks 刷新滞后导致旧卡短暂复现
  if (pendingSplit) {
    const group = tasks.find((t) => t.id === pendingSplit.groupTaskId);
    if (group && group.status !== "proposed") return null;
  }
  // 计划文档与会话绑定：优先使用后端广播的实际文档路径（AI 可能写时间戳文件名），
  // 缺省回退约定名 ai/chatcoder-plan-<sessionId>.md。
  const planDocPath = pendingSplit?.planDocPath
    ?? (currentSessionId != null ? `ai/chatcoder-plan-${currentSessionId}.md` : "ai/chatcoder-plan.md");
  const title = pendingPlan?.task
    ?? tasks.find((task) => task.id === pendingSplit?.requestTaskId)?.title
    ?? "任务执行计划";
  const confirm = () => {
    if (pendingSplit) {
      void confirmTaskSplit(true, splitSteps.map((step) => ({ task_id: step.id, title: step.title })));
    } else if (pendingPlan) {
      void confirmPlan(pendingPlan.task);
    }
  };
  const cancel = () => {
    if (pendingSplit) void confirmTaskSplit(false);
    else dismissPlan();
  };
  return (
    <div className="turn-group">
      <div className="plan-inline-card">
        <div className="plan-inline-head">
          <IconClipboard size={13} /> 计划
        </div>
        <button type="button" className="plan-inline-title" onClick={() => setExpanded((v) => !v)} aria-expanded={expanded}>{title}</button>
        {expanded && <div className="plan-inline-preview"><code>{planDocPath}</code><br />展开右侧文件面板可查看完整计划内容。</div>}
        <div className="plan-inline-desc">
          AI 已在项目根目录 <code>ai/</code> 目录生成计划文档 <code>{planDocPath}</code>，请审阅后确认是否按计划执行。
        </div>
        <div className="plan-inline-actions">
          <button
            className="plan-inline-view"
            onClick={() => {
              usePanelStore.getState().setPreviewPath(planDocPath);
              usePanelStore.getState().openPanel();
              usePanelStore.getState().openTab("files");
            }}
          >
            查看完整计划 →
          </button>
          <span className="plan-inline-spacer" />
          {splitSteps.length > 0 && expanded && (
            <div className="plan-inline-steps">
              {splitSteps.map((step, index) => <div key={step.id}>{index + 1}. {step.title}</div>)}
            </div>
          )}
          <button className="btn-ghost" onClick={cancel}>取消</button>
          <button className="plan-inline-confirm" onClick={confirm}>确认执行</button>
        </div>
      </div>
    </div>
  );
}

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
  /** 额外尾部（如计划卡），占最后一项 */
  trailingNode?: ReactNode | null;
  /** 会话标识：变化时强制跳底（主界面传 currentSessionId，子代理传 threadId） */
  sessionKey: string | number;
  /** 流式信号：内容变化时贴底跟随（避免整对象依赖） */
  streamSignal: string;
  /** 功能开关（默认按模式由外层传入） */
  jumpDots?: boolean;
  search?: boolean;
  /** 滚动目标（主界面任务卡点击穿透 / turn 导航） */
  scrollTarget?: { threadId?: number; turnId?: number } | null;
  clearScrollTarget?: () => void;
  /** 容器附加类名 */
  className?: string;
  /** 空态文案（entries 为空且未运行） */
  emptyText: string;
}

function MessageFlowCore({
  entries, running, renderEntry, streamingNode, trailingNode = null,
  sessionKey, streamSignal, jumpDots = true, search = true,
  scrollTarget = null, clearScrollTarget, className, emptyText,
}: MessageFlowCoreProps) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const [searchOpen, setSearchOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [matchIdx, setMatchIdx] = useState(0);
  const [nearBottom, setNearBottom] = useState(true);
  const seenKeysRef = useRef<Set<string>>(new Set());

  // 虚拟化：count = entries + 运行中 StreamingText 占位 + 额外尾部占位
  const count = entries.length + (running ? 1 : 0) + (trailingNode ? 1 : 0);

  const entryKeyAt = useCallback((index: number): string =>
    index < entries.length
      ? (entries[index].kind === "turn" ? `turn-${entries[index].turnId ?? index}` : `std-${entries[index].msg.id ?? index}`)
      : (index === entries.length && running ? "streaming" : "trailing"),
  [entries, running]);

  const virtualizer = useVirtualizer({
    count,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => 120,
    overscan: 8,
    getItemKey: entryKeyAt,
  });

  // 统一滚动管理：靠近底部自动跟随，否则由用户滚动
  const prevCount = useRef(0);
  const prevRunning = useRef(false);
  const prevSessionRef = useRef<string | number | null>(null);
  const nearBottomRef = useRef(true);
  const lastScrollTsRef = useRef(0);

  const scrollToBottom = useCallback(() => {
    if (entries.length === 0) return;
    nearBottomRef.current = true; // 锁定贴底
    setNearBottom(true);
    virtualizer.scrollToIndex(entries.length - 1, { align: "end" });
    requestAnimationFrame(() => {
      const el = scrollRef.current;
      if (el) el.scrollTop = el.scrollHeight;
    });
  }, [entries.length, virtualizer]);

  const handleScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const near = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
    nearBottomRef.current = near;
    setNearBottom((prev) => (prev === near ? prev : near));
  }, []);

  // 虚拟列表测量更新时保持贴底（进入会话即显示最底部，全程无滚动动画）
  const totalSize = virtualizer.getTotalSize();
  useLayoutEffect(() => {
    if (nearBottomRef.current && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [totalSize]);

  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (!el) return;

    if (prevSessionRef.current !== sessionKey) {
      prevSessionRef.current = sessionKey;
      prevCount.current = entries.length;
      prevRunning.current = running;
      scrollToBottom();
      return;
    }

    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
    nearBottomRef.current = nearBottom;

    const newEntries = entries.length > prevCount.current;
    const justStarted = running && !prevRunning.current;
    const streaming = running && nearBottom;

    if (streaming) {
      const now = performance.now();
      if (now - lastScrollTsRef.current >= 16) {
        lastScrollTsRef.current = now;
        el.scrollTop = el.scrollHeight;
      }
    } else if (newEntries || justStarted) {
      if (prevCount.current === 0) {
        scrollToBottom();
      } else {
        requestAnimationFrame(() => {
          if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
        });
      }
    }

    prevCount.current = entries.length;
    prevRunning.current = running;
  }, [sessionKey, entries.length, running, streamSignal, virtualizer, scrollToBottom]);

  const jumpToEntry = useCallback((entry: TimelineEntry) => {
    const idx = entries.findIndex((e) => e === entry);
    if (idx >= 0) virtualizer.scrollToIndex(idx, { align: "start" });
  }, [entries, virtualizer]);

  const matches = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return [] as number[];
    const out: number[] = [];
    entries.forEach((e, i) => {
      const text = e.kind === "turn"
        ? e.items.map((it) => (it.kind === "user" || it.kind === "text" || it.kind === "summary" || it.kind === "thinking" ? msgText(it.msg.content) : "")).join(" ")
        : msgText(e.msg.content);
      if (text.toLowerCase().includes(q)) out.push(i);
    });
    return out;
  }, [entries, query]);

  const gotoMatch = (dir: 1 | -1) => {
    if (matches.length === 0) return;
    setMatchIdx((prev) => {
      const next = (prev + dir + matches.length) % matches.length;
      virtualizer.scrollToIndex(matches[next], { align: "start" });
      return next;
    });
  };

  // 滚动目标：任务卡步骤点击穿透 / turn 导航
  useEffect(() => {
    if (!scrollTarget) return;
    const { threadId, turnId } = scrollTarget;
    let idx = -1;
    if (threadId != null) {
      idx = entries.findIndex((e) => {
        if (e.kind !== "turn") return false;
        return e.items.some((it) => {
          if ("msg" in it) return it.msg.thread_id === threadId;
          if (it.kind === "tools") {
            return it.nodes.some((n) =>
              n.kind === "leaf" ? n.leaf.threadId === threadId : n.leaves.some((leaf) => leaf.threadId === threadId),
            );
          }
          return false;
        });
      });
    } else if (turnId != null) {
      idx = entries.findIndex((e) => e.kind === "turn" && e.turnId === turnId);
    }
    if (idx >= 0) {
      virtualizer.scrollToIndex(idx, { align: "start" });
      window.setTimeout(() => clearScrollTarget?.(), 800);
    } else {
      clearScrollTarget?.();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scrollTarget]);

  useEffect(() => {
    if (!search) return;
    const handler = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "f") {
        e.preventDefault();
        setSearchOpen(true);
      }
      if (e.key === "Escape") setSearchOpen(false);
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [search]);

  const renderRow = (index: number) => {
    if (index < entries.length) return renderEntry(entries[index], index);
    if (index === entries.length && running) return streamingNode;
    return trailingNode;
  };

  return (
    <div className={"message-flow-wrap" + (className ? ` ${className}` : "")}>
      {jumpDots && <JumpDots entries={entries} onJump={jumpToEntry} />}

      {search && searchOpen && (
        <div className="mf-search">
          <IconSearch size={12} />
          <input
            autoFocus
            placeholder="搜索消息内容…"
            value={query}
            onChange={(e) => { setQuery(e.target.value); setMatchIdx(0); }}
            onKeyDown={(e) => {
              if (e.key === "Enter") gotoMatch(e.shiftKey ? -1 : 1);
            }}
          />
          {matches.length > 0 && (
            <span className="mf-search-count">{matchIdx + 1}/{matches.length}</span>
          )}
          <button className="mf-search-nav" onClick={() => gotoMatch(-1)} title="上一个">
            <IconChevronUp size={12} />
          </button>
          <button className="mf-search-nav" onClick={() => gotoMatch(1)} title="下一个">
            <IconChevronDown size={12} />
          </button>
          <button className="mf-search-nav" onClick={() => { setSearchOpen(false); setQuery(""); }} title="关闭">
            <IconX size={12} />
          </button>
        </div>
      )}

      <div className="message-flow" ref={scrollRef} onScroll={handleScroll}>
        {entries.length === 0 && !running ? (
          <div className="mf-empty">
            <p>{emptyText}</p>
          </div>
        ) : (
          <div className="mf-virtual" style={{ height: virtualizer.getTotalSize(), position: "relative", width: "100%" }}>
            {virtualizer.getVirtualItems().map((vi) => {
              // 新条目入场动画：仅首次出现的 key 挂动画类，虚拟列表滚动重挂载不重播
              const k = String(vi.key);
              const seen = seenKeysRef.current;
              const isNew = vi.index < entries.length && !seen.has(k);
              if (!seen.has(k)) seen.add(k);
              return (
                <div
                  key={vi.key}
                  data-index={vi.index}
                  ref={virtualizer.measureElement}
                  style={{ position: "absolute", top: 0, left: 0, width: "100%", transform: `translateY(${vi.start}px)` }}
                >
                  <div className={"mf-list" + (isNew ? " mf-entry-new" : "")}>{renderRow(vi.index)}</div>
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* 跳转底部：用户向上翻阅历史时出现，点击回到最新消息并恢复自动跟随 */}
      {!nearBottom && (entries.length > 0 || running) && (
        <button className="mf-jump-bottom" onClick={scrollToBottom} title="回到底部">
          <IconArrowDown size={14} />
        </button>
      )}
    </div>
  );
}

/** 主会话数据源（source="main"）：读全局 store，行为与 v19 一致。 */
function MainMessageFlow({ className }: { className?: string }) {
  const messages = useChatStore((s) => s.messages);
  const currentSessionId = useChatStore((s) => s.currentSessionId);
  const runningTurnId = useChatStore((s) => s.runningTurnId);
  const isRunning = useChatStore((s) => s.isRunning);
  const scrollTarget = useChatStore((s) => s.scrollTarget);
  const clearScrollTarget = useChatStore((s) => s.clearScrollTarget);
  const turns = useChatStore((s) => s.turns);
  const subagentMeta = useChatStore((s) => s.subagentMeta);
  const streamingBuffers = useChatStore((s) => s.streamingBuffers);
  const thinkingBuffers = useChatStore((s) => s.thinkingBuffers);
  const hasPlanCard = useChatStore((s) => s.pendingPlan != null || s.pendingSplit != null);
  // plan-95: 计划卡归属 turn——卡片内嵌到该 turn 行尾随时间线滚动，
  // 不再固定在消息流最底部（与后续新消息脱节）
  const planTurnId = useChatStore((s) => s.pendingSplit?.turnId ?? s.pendingPlan?.turnId ?? null);
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

  // v30: 被压缩的消息保留在时间线上（不隐藏）；压缩块卡由 SUMMARY 消息渲染
  const entries = useMemo(() => buildTimeline(messages), [messages]);

  const renderEntry = useCallback((entry: TimelineEntry) => {
    if (entry.kind !== "turn") return <StandaloneEntry entry={entry} />;
    // v12: 已回滚 turn 显示专用横幅（回滚后消息被软删，以此占位区分「回滚了」与「没执行」）
    const rolledBack = turns.find((t) => t.id === entry.turnId)?.status === "rolled_back";
    // plan-95: 计划卡内嵌到其归属 turn 的行尾（同工具调用一样按时间线定位）
    const planCardHere = entry.turnId != null && entry.turnId === planTurnId;
    return (
      <>
        <TurnGroup
          entry={entry}
          isRunning={runningTurnId === entry.turnId}
          rolledBack={rolledBack}
          subagents={entry.turnId != null ? subagentsByTurn.get(entry.turnId) : undefined}
        />
        {planCardHere && <PlanCard />}
      </>
    );
  }, [turns, runningTurnId, subagentsByTurn, planTurnId]);

  const streamSignal = useMemo(
    () => Object.values(streamingBuffers).join("") + "|" + Object.values(thinkingBuffers).join(""),
    [streamingBuffers, thinkingBuffers],
  );
  const thinkingText = Object.values(thinkingBuffers).join("").trim();
  const text = Object.values(streamingBuffers).join("");
  const turnStatus = useChatStore((s) => s.turnStatus);

  return (
    <MessageFlowCore
      entries={entries}
      running={isRunning}
      renderEntry={renderEntry}
      streamingNode={<StreamingText active={Boolean(isRunning && runningTurnId)} thinking={thinkingText} text={text} statusLabel={turnStatus ?? undefined} />}
      trailingNode={
        <>
          {/* plan-95: 归属 turn 不在时间线上时兑底渲染到尾部；正常情况内嵌于 turn 行 */}
          {hasPlanCard && planTurnId == null && <PlanCard />}
          {isCompacting && <CompactingCard info={compactingInfo} />}
        </>
      }
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
function SubagentMessageFlow({ threadId, actions, className }: {
  threadId?: number;
  actions?: "full" | "copy-only" | "none";
  className?: string;
}) {
  const currentSessionId = useChatStore((s) => s.currentSessionId);
  const liveMessages = useChatStore((s) => (threadId != null ? s.subagentMessages[threadId] : undefined));
  const meta = useChatStore((s) => (threadId != null ? s.subagentMeta[threadId] : undefined));
  const thinking = useChatStore((s) => (threadId != null ? s.subagentThinking[threadId] ?? "" : ""));
  const stream = useChatStore((s) => (threadId != null ? s.subagentStreams[threadId] ?? "" : ""));
  const [history, setHistory] = useState<MessageOut[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // 首次挂载拉取历史消息（实时桶增量追加，合并去重）
  useEffect(() => {
    if (!currentSessionId || threadId == null) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    api.listSessionMessages(currentSessionId, threadId)
      .then((msgs) => { if (!cancelled) setHistory(msgs); })
      .catch((e) => { if (!cancelled) setError(String(e)); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [currentSessionId, threadId]);

  const messages = useMemo(() => {
    const seen = new Set<number>();
    const out: MessageOut[] = [];
    for (const m of [...(history ?? []), ...(liveMessages ?? [])]) {
      if (seen.has(m.id)) continue;
      seen.add(m.id);
      out.push(m);
    }
    out.sort((a, b) => (Number(a.id) || 0) - (Number(b.id) || 0));
    return out;
  }, [history, liveMessages]);

  const entries = useMemo(() => buildTimeline(messages), [messages]);
  const running = meta?.status === "running" || meta?.status === "in_progress";

  const renderEntry = useCallback((entry: TimelineEntry, index: number) => {
    if (entry.kind !== "turn") return <StandaloneEntry entry={entry} />;
    return (
      <TurnGroup
        entry={entry}
        isRunning={running && index === entries.length - 1}
        actions={actions ?? "copy-only"}
      />
    );
  }, [running, entries.length, actions]);

  if (threadId == null) return <div className="mf-empty"><p>未指定子代理</p></div>;
  if (loading && messages.length === 0) return <div className="mf-empty"><p>加载子代理消息流…</p></div>;
  if (error && messages.length === 0) return <div className="mf-empty"><p>加载失败：{error}</p></div>;

  return (
    <MessageFlowCore
      entries={entries}
      running={running}
      renderEntry={renderEntry}
      streamingNode={running ? <StreamingText active thinking={thinking} text={stream} /> : null}
      sessionKey={threadId}
      streamSignal={thinking + "|" + stream}
      jumpDots={false}
      search={false}
      className={className}
      emptyText="该子代理暂无消息"
    />
  );
}

/** 插件入口：按 source 分发数据源（主会话全局 store / 子代理线程桶）。 */
export function MessageFlow(props: MessageStreamProps) {
  if (props.source === "subagent") {
    return (
      <SubagentMessageFlow
        threadId={props.threadId}
        actions={props.features?.actions}
        className={props.className}
      />
    );
  }
  return <MainMessageFlow className={props.className} />;
}
