/** MessageFlow（v6 虚拟化重写）：消息流容器。
 * - 改用 @tanstack/react-virtual 虚拟化列表，仅渲染可视区消息，解决大量消息时拖拽/折叠卡死。
 * - 动态高度：measureElement + ResizeObserver 自动测量。
 * - 滚动策略：靠近底部自动跟随（流式/新消息），不在底部由用户滚动。
 * - 左侧 JumpDots、Ctrl+F 会话内搜索保留。
 */
import { useEffect, useLayoutEffect, useMemo, useRef, useState, useCallback, memo } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import type { TimelineEntry } from "./timeline";
import { buildTimeline, msgText } from "./timeline";
import { TurnGroup } from "./TurnGroup";
import { JumpDots } from "./JumpDots";
import { IconSearch, IconChevronUp, IconChevronDown, IconX, IconArrowDown, IconBrain, IconClipboard } from "../icons";
import { MarkdownContent } from "../MarkdownContent";
import { MsgType } from "@chatcoder/shared";
import { useChatStore } from "../../store/chat";
import { usePanelStore } from "../../store/panel";

/** 流式展示（thinking/token delta 实时渲染），作为虚拟列表最后一个虚拟项。
 * v7: 当前轮次的"思考中"以思考块形式实时显示在列表最底部（正在进行的动作），
 * 上面已完成的历史按时间顺序排列——不会出现"上面在思考、下面已有消息/工具调用"的错位观感。
 */
function StreamingText() {
  const buffers = useChatStore((s) => s.streamingBuffers);
  const thinkingBuffers = useChatStore((s) => s.thinkingBuffers);
  const runningTurnId = useChatStore((s) => s.runningTurnId);
  const isRunning = useChatStore((s) => s.isRunning);
  const bodyRef = useRef<HTMLDivElement>(null);
  const thinkingText = Object.values(thinkingBuffers).join("").trim();
  const text = Object.values(buffers).join("");

  // 思考流自动滚到底部（zcode 风格）
  useEffect(() => {
    const el = bodyRef.current;
    if (el && thinkingText) {
      el.scrollTop = el.scrollHeight;
    }
  }, [thinkingText]);

  if (!isRunning || !runningTurnId) return null;

  return (
    <div className="turn-group" style={{ minHeight: "36px", paddingBottom: 8 }}>
      {/* 当前轮次思考中：实时思考内容（思考落库后此块消失，思考块按时间顺序出现在消息流中） */}
      {thinkingText && (
        <div className="thinking-block active open">
          <div className="thinking-block-head" style={{ cursor: "default" }}>
            <span className="thinking-block-icon"><IconBrain size={12} /></span>
            <span className="thinking-block-title">
              <span className="thinking-block-breath" />
              <span className="thinking-block-status">思考中…</span>
            </span>
            <span className="thinking-block-chev" />
          </div>
          <div className="thinking-block-body" ref={bodyRef} style={{ maxHeight: 160, overflowY: "auto" }}>
            <pre className="thinking-block-content">{thinkingText}</pre>
          </div>
        </div>
      )}
      {text && (
        <div className="turn-item turn-item-text">
          <div className="turn-agent-text">
            <MarkdownContent>{text}</MarkdownContent>
            <span className="stream-caret"><i /><i /><i /></span>
          </div>
        </div>
      )}
      {!thinkingText && (
        <div className="turn-status-line">
          <span className="thinking-block-breath" style={{ marginRight: 6 }} />
          <span className="thinking-block-status">{text ? "处理中…" : "等待响应…"}</span>
        </div>
      )}
    </div>
  );
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

/** 计划确认卡（对齐 zcode 图6）：plan 模式 turn 完成后内嵌在消息流底部展示，
 *  「查看完整计划 →」打开右侧文件预览，确认执行/取消。 */
function PlanCard() {
  const pendingPlan = useChatStore((s) => s.pendingPlan);
  const confirmPlan = useChatStore((s) => s.confirmPlan);
  const dismissPlan = useChatStore((s) => s.dismissPlan);
  if (!pendingPlan) return null;
  return (
    <div className="turn-group">
      <div className="plan-inline-card">
        <div className="plan-inline-head">
          <IconClipboard size={13} /> 计划
        </div>
        <div className="plan-inline-title">{pendingPlan.task}</div>
        <div className="plan-inline-desc">
          AI 已在项目根目录 <code>ai/</code> 目录生成计划文档 <code>chatcoder-plan.md</code>，请审阅后确认是否按计划执行。
        </div>
        <div className="plan-inline-actions">
          <button
            className="plan-inline-view"
            onClick={() => {
              usePanelStore.getState().setPreviewPath("ai/chatcoder-plan.md");
              usePanelStore.getState().openPanel();
              usePanelStore.getState().openTab("files");
            }}
          >
            查看完整计划 →
          </button>
          <span className="plan-inline-spacer" />
          <button className="btn-ghost" onClick={() => dismissPlan()}>取消</button>
          <button className="plan-inline-confirm" onClick={() => confirmPlan(pendingPlan.task)}>确认执行</button>
        </div>
      </div>
    </div>
  );
}

export function MessageFlow() {
  const messages = useChatStore((s) => s.messages);
  const currentSessionId = useChatStore((s) => s.currentSessionId);
  const runningTurnId = useChatStore((s) => s.runningTurnId);
  const isRunning = useChatStore((s) => s.isRunning);
  const scrollTarget = useChatStore((s) => s.scrollTarget);
  const clearScrollTarget = useChatStore((s) => s.clearScrollTarget);
  // v12: 已回滚 turn 标识（时间线横幅 + 产物灰置）
  const turns = useChatStore((s) => s.turns);
  const streamingBuffers = useChatStore((s) => s.streamingBuffers);
  const thinkingBuffers = useChatStore((s) => s.thinkingBuffers);
  const hasPlanCard = useChatStore((s) => s.pendingPlan != null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const [searchOpen, setSearchOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [matchIdx, setMatchIdx] = useState(0);
  // 是否贴近底部（驱动"跳转底部"悬浮按钮显隐，与 nearBottomRef 同步）
  const [nearBottom, setNearBottom] = useState(true);
  // 已见过的时间线条目 key：入场动画只对真正新增的条目播放一次，
  // 虚拟列表滚动导致的重挂载不会重播（key 已在集合中）。
  const seenKeysRef = useRef<Set<string>>(new Set());

  const entries = useMemo(() => buildTimeline(messages), [messages]);

  // v2.2: 任务卡步骤点击穿透——按 scrollTarget 滚动到对应条目
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
      // 滚动完成后清除目标（避免重复触发）
      window.setTimeout(() => clearScrollTarget(), 800);
    } else {
      clearScrollTarget();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scrollTarget]);

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

  // 虚拟化：count = entries + 运行中的 StreamingText 占位 + 计划确认卡占位
  const count = entries.length + (isRunning ? 1 : 0) + (hasPlanCard ? 1 : 0);

  // 条目 key（虚拟列表 key 与"新条目入场"追踪共用）
  const entryKeyAt = useCallback((index: number): string =>
    index < entries.length
      ? (entries[index].kind === "turn" ? `turn-${entries[index].turnId ?? index}` : `std-${entries[index].msg.id ?? index}`)
      : (index === entries.length && isRunning ? "streaming" : "plan-card"),
  [entries, isRunning]);

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
  const prevSessionRef = useRef<number | null>(null);
  const nearBottomRef = useRef(true);
  const lastScrollTsRef = useRef(0);

  const scrollToBottom = useCallback(() => {
    if (entries.length === 0) return;
    nearBottomRef.current = true; // 锁定贴底
    setNearBottom(true);
    virtualizer.scrollToIndex(entries.length - 1, { align: "end" });
    // 双保险：虚拟列表测量完成后再次贴底
    requestAnimationFrame(() => {
      const el = scrollRef.current;
      if (el) el.scrollTop = el.scrollHeight;
    });
  }, [entries.length, virtualizer]);

  // 用户手动滚动：离开底部则解锁自动贴底，之后新消息不再打扰
  const handleScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const near = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
    nearBottomRef.current = near;
    setNearBottom((prev) => (prev === near ? prev : near));
  }, []);

  // 虚拟列表测量更新（totalSize 变化）时，若处于贴底锁定状态则保持贴底
  // 效果：进入会话即显示最底部，全程无滚动动画
  const totalSize = virtualizer.getTotalSize();
  useLayoutEffect(() => {
    if (nearBottomRef.current && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [totalSize]);

  // 使用 useLayoutEffect：在浏览器绘制前完成滚动定位，避免首帧显示顶部再滚动
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (!el) return;

    // 会话切换或刷新后首次加载：无论消息数增减都直接跳到对话最底部
    if (prevSessionRef.current !== currentSessionId) {
      prevSessionRef.current = currentSessionId;
      prevCount.current = entries.length;
      prevRunning.current = isRunning;
      scrollToBottom();
      return;
    }

    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
    nearBottomRef.current = nearBottom;

    const newEntries = entries.length > prevCount.current;
    const justStarted = isRunning && !prevRunning.current;
    const streaming = isRunning && nearBottom;

    if (streaming) {
      // 流式输出用时间节流，避免鬼畜抖动
      const now = performance.now();
      if (now - lastScrollTsRef.current >= 16) {
        lastScrollTsRef.current = now;
        el.scrollTop = el.scrollHeight;
      }
    } else if (newEntries || justStarted) {
      if (prevCount.current === 0) {
        // 首次加载（消息异步到达）：虚拟列表定位更可靠
        scrollToBottom();
      } else {
        requestAnimationFrame(() => {
          if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
        });
      }
    }

    prevCount.current = entries.length;
    prevRunning.current = isRunning;
  }, [currentSessionId, entries.length, isRunning, streamingBuffers, thinkingBuffers, virtualizer, scrollToBottom]);

  const jumpToEntry = useCallback((entry: TimelineEntry) => {
    const idx = entries.findIndex((e) => e === entry);
    if (idx >= 0) virtualizer.scrollToIndex(idx, { align: "start" });
  }, [entries, virtualizer]);

  const gotoMatch = (dir: 1 | -1) => {
    if (matches.length === 0) return;
    setMatchIdx((prev) => {
      const next = (prev + dir + matches.length) % matches.length;
      virtualizer.scrollToIndex(matches[next], { align: "start" });
      return next;
    });
  };

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "f") {
        e.preventDefault();
        setSearchOpen(true);
      }
      if (e.key === "Escape") setSearchOpen(false);
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, []);

  const renderRow = (index: number) => {
    if (index < entries.length) {
      const entry = entries[index];
      if (entry.kind !== "turn") return <StandaloneEntry entry={entry} />;
      // v12: 已回滚 turn 显示专用横幅（回滚后消息被软删，以此占位区分「回滚了」与「没执行」）
      const rolledBack = turns.find((t) => t.id === entry.turnId)?.status === "rolled_back";
      return <TurnGroup entry={entry} isRunning={runningTurnId === entry.turnId} rolledBack={rolledBack} />;
    }
    if (index === entries.length && isRunning) return <StreamingText />;
    return <PlanCard />;
  };

  return (
    <div className="message-flow-wrap">
      <JumpDots entries={entries} onJump={jumpToEntry} />

      {searchOpen && (
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
        {entries.length === 0 && !isRunning ? (
          <div className="mf-empty">
            <p>选择或创建会话，开始你的任务</p>
          </div>
        ) : (
          <div className="mf-virtual" style={{ height: virtualizer.getTotalSize(), position: "relative", width: "100%" }}>
            {virtualizer.getVirtualItems().map((vi) => {
              // 新条目入场动画：仅首次出现的 key 挂动画类，虚拟列表重挂载不重播
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
      {!nearBottom && (entries.length > 0 || isRunning) && (
        <button className="mf-jump-bottom" onClick={scrollToBottom} title="回到底部">
          <IconArrowDown size={14} />
        </button>
      )}
    </div>
  );
}
