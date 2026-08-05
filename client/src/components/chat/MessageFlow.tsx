/** MessageFlow（v6 虚拟化重写）：消息流容器。
 * - 改用 @tanstack/react-virtual 虚拟化列表，仅渲染可视区消息，解决大量消息时拖拽/折叠卡死。
 * - 动态高度：measureElement + ResizeObserver 自动测量。
 * - 滚动策略：靠近底部自动跟随（流式/新消息），不在底部由用户滚动。
 * - 左侧 JumpDots、Ctrl+F 会话内搜索保留。
 */
import { useEffect, useLayoutEffect, useMemo, useRef, useState, useCallback } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import type { TimelineEntry } from "./timeline";
import { buildTimeline, msgText } from "./timeline";
import { TurnGroup } from "./TurnGroup";
import { JumpDots } from "./JumpDots";
import { IconSearch, IconChevronUp, IconChevronDown, IconX } from "../icons";
import { MarkdownContent } from "../MarkdownContent";
import { useChatStore } from "../../store/chat";

/** 流式文本展示（token.delta 实时渲染）+ 底部状态行，作为虚拟列表最后一个虚拟项。 */
function StreamingText() {
  const buffers = useChatStore((s) => s.streamingBuffers);
  const thinkingBuffers = useChatStore((s) => s.thinkingBuffers);
  const runningTurnId = useChatStore((s) => s.runningTurnId);
  const isRunning = useChatStore((s) => s.isRunning);
  if (!isRunning || !runningTurnId) return null;

  const thinkingText = Object.values(thinkingBuffers).join("").trim();
  const text = Object.values(buffers).join("");

  // 状态优先级：思考中 > 处理中 > 等待响应
  const status = thinkingText ? "思考中…" : text ? "处理中…" : "等待响应…";

  return (
    <div className="turn-group" style={{ minHeight: "36px", paddingBottom: 8 }}>
      {text && (
        <div className="turn-item turn-item-text">
          <div className="turn-agent-text">
            <MarkdownContent>{text}</MarkdownContent>
            <span className="thinking-block-breath" style={{ display: "inline-block", marginLeft: 2, verticalAlign: "middle" }} />
          </div>
        </div>
      )}
      <div className="turn-status-line">
        <span className="thinking-block-breath" style={{ marginRight: 6 }} />
        <span>{status}</span>
      </div>
    </div>
  );
}

function StandaloneEntry({ entry }: { entry: Extract<TimelineEntry, { kind: "standalone" }> }) {
  return (
    <div className="turn-group">
      <div className="turn-item turn-item-text">
        <div className="turn-agent-text">
          <MarkdownContent>{msgText(entry.msg.content)}</MarkdownContent>
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
  const streamingBuffers = useChatStore((s) => s.streamingBuffers);
  const thinkingBuffers = useChatStore((s) => s.thinkingBuffers);
  const scrollRef = useRef<HTMLDivElement>(null);
  const [searchOpen, setSearchOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [matchIdx, setMatchIdx] = useState(0);

  const entries = useMemo(() => buildTimeline(messages), [messages]);

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

  // 虚拟化：count = entries + 运行中的 StreamingText 占位
  const count = entries.length + (isRunning ? 1 : 0);

  const virtualizer = useVirtualizer({
    count,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => 120,
    overscan: 8,
    getItemKey: (index) =>
      index < entries.length
        ? (entries[index].kind === "turn" ? `turn-${entries[index].turnId ?? index}` : `std-${entries[index].msg.id ?? index}`)
        : "streaming",
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
    nearBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
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
      return entry.kind === "turn" ? (
        <TurnGroup entry={entry} isRunning={runningTurnId === entry.turnId} />
      ) : (
        <StandaloneEntry entry={entry} />
      );
    }
    return <StreamingText />;
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
            {virtualizer.getVirtualItems().map((vi) => (
              <div
                key={vi.key}
                data-index={vi.index}
                ref={virtualizer.measureElement}
                style={{ position: "absolute", top: 0, left: 0, width: "100%", transform: `translateY(${vi.start}px)` }}
              >
                <div className="mf-list">{renderRow(vi.index)}</div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
