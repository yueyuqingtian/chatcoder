/** MessageFlow（v5 重写）：消息流容器。
 * - 去掉绝对定位虚拟滚动，改用普通滚动容器，彻底解决折叠/重叠/错位问题
 * - 左侧 JumpDots（≥2 turns 时显示）
 * - Ctrl/Cmd+F 会话内搜索
 * - 消息间紧凑排列，工具调用与文字左对齐
 */
import { useEffect, useMemo, useRef, useState, useCallback } from "react";
import type { TimelineEntry } from "./timeline";
import { buildTimeline, msgText } from "./timeline";
import { TurnGroup } from "./TurnGroup";
import { JumpDots } from "./JumpDots";
import { IconSearch, IconChevronUp, IconChevronDown, IconX } from "../icons";
import { MarkdownContent } from "../MarkdownContent";
import { useChatStore } from "../../store/chat";

/** 流式文本展示（token.delta 实时渲染）
 * v1.3: 不再内联渲染思考块，避免与 TurnGroup 的 ThinkingBlock 重复。
 * 思考流式内容由最后一个 active ThinkingBlock 通过 store 消费。
 * 此组件只负责正文文本流 + 等待状态 + 留白。
 */
function StreamingText() {
  const buffers = useChatStore((s) => s.streamingBuffers);
  const thinkingBuffers = useChatStore((s) => s.thinkingBuffers);
  const runningTurnId = useChatStore((s) => s.runningTurnId);
  const isRunning = useChatStore((s) => s.isRunning);
  if (!isRunning || !runningTurnId) return null;

  const isThinking = Object.keys(thinkingBuffers).length > 0;
  const text = Object.values(buffers).join("");

  return (
    <div className="mf-entry" style={{ minHeight: "60px", paddingBottom: 16 }}>
      <div className="turn-group">
        {text && (
          <div className="turn-item turn-item-text">
            <div className="turn-agent-text">
              <MarkdownContent>{text}</MarkdownContent>
              <span className="thinking-block-breath" style={{ display: "inline-block", marginLeft: 2, verticalAlign: "middle" }} />
            </div>
          </div>
        )}
        {!text && !isThinking && (
          <div className="turn-item turn-item-text" style={{ color: "var(--text-3)", fontSize: 12, padding: "8px 0" }}>
            <span className="thinking-block-breath" style={{ marginRight: 6 }} />
            等待响应…
          </div>
        )}
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
  const runningTurnId = useChatStore((s) => s.runningTurnId);
  const isRunning = useChatStore((s) => s.isRunning);
  const streamingBuffers = useChatStore((s) => s.streamingBuffers);
  const thinkingBuffers = useChatStore((s) => s.thinkingBuffers);
  const scrollRef = useRef<HTMLDivElement>(null);
  const entryRefs = useRef<Map<number, HTMLDivElement>>(new Map());
  const [searchOpen, setSearchOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [matchIdx, setMatchIdx] = useState(0);

  const entries = useMemo(() => buildTimeline(messages), [messages]);

  const matches = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return [] as { entryIdx: number }[];
    const out: { entryIdx: number }[] = [];
    entries.forEach((e, i) => {
      const text = e.kind === "turn"
        ? e.items.map((it) => (it.kind === "user" || it.kind === "text" || it.kind === "summary" || it.kind === "thinking" ? msgText(it.msg.content) : "")).join(" ")
        : msgText(e.msg.content);
      if (text.toLowerCase().includes(q)) out.push({ entryIdx: i });
    });
    return out;
  }, [entries, query]);

  // v6.3/v6.4: 统一滚动管理 —— 合并 3 个竞争 effect 为 1 个，消除鬼畜抖动
  // v6.4: 流式输出时直接同步滚动（不走 rAF），避免大量 delta 时 rAF 被频繁取消导致滚动滞后
  const prevCount = useRef(0);
  const prevRunning = useRef(false);
  const rafRef = useRef<number | null>(null);
  // 缓存"是否靠近底部"的判断，流式 delta 高频到达时避免重复计算 + 重复滚动
  const nearBottomRef = useRef(true);
  // 节流：流式输出时最多每 16ms 滚动一次（一帧），避免每个 token 都触发回流
  const lastScrollTsRef = useRef(0);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;

    // 判断是否靠近底部（用户是否在查看历史）
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 300;
    nearBottomRef.current = nearBottom;

    // 情况1：新消息条数增加 → 滚到底
    const newEntries = entries.length > prevCount.current;
    // 情况2：开始运行（false→true）→ 强制滚到底
    const justStarted = isRunning && !prevRunning.current;
    // 情况3：流式输出中 + 靠近底部 → 跟随滚动
    const streaming = isRunning && nearBottom;

    if (streaming) {
      // v6.4: 流式输出 —— 同步直接滚动 + 时间节流
      // 大量 token delta 到达时，每个都触发 effect，但只在一帧内滚动一次
      const now = performance.now();
      if (now - lastScrollTsRef.current >= 16) {
        lastScrollTsRef.current = now;
        el.scrollTop = el.scrollHeight;
      }
    } else if (newEntries || justStarted) {
      // 非流式场景（新消息/开始运行）用 rAF 合并，避免抖动
      if (rafRef.current !== null) {
        cancelAnimationFrame(rafRef.current);
      }
      rafRef.current = requestAnimationFrame(() => {
        const el2 = scrollRef.current;
        if (el2) el2.scrollTop = el2.scrollHeight;
        rafRef.current = null;
      });
    }

    prevCount.current = entries.length;
    prevRunning.current = isRunning;
  }, [entries.length, isRunning, streamingBuffers, thinkingBuffers]);

  const jumpToEntry = useCallback((entry: TimelineEntry) => {
    const idx = entries.findIndex((e) => e === entry);
    if (idx >= 0) {
      const el = entryRefs.current.get(idx);
      if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }, [entries]);

  const gotoMatch = (dir: 1 | -1) => {
    if (matches.length === 0) return;
    setMatchIdx((prev) => {
      const next = (prev + dir + matches.length) % matches.length;
      const el = entryRefs.current.get(matches[next].entryIdx);
      if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
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

      <div className="message-flow" ref={scrollRef}>
        {entries.length === 0 && !isRunning ? (
          <div className="mf-empty">
            <p>选择或创建会话，开始你的任务</p>
          </div>
        ) : (
          <div className="mf-list">
            {entries.map((entry, i) => (
              <div
                key={entry.kind === "turn" ? `turn-${entry.turnId ?? i}` : `std-${entry.msg.id ?? i}`}
                ref={(el) => { if (el) entryRefs.current.set(i, el); else entryRefs.current.delete(i); }}
                className="mf-entry"
              >
                {entry.kind === "turn" ? (
                  <TurnGroup entry={entry} isRunning={runningTurnId === entry.turnId} />
                ) : (
                  <StandaloneEntry entry={entry} />
                )}
              </div>
            ))}
            <StreamingText />
          </div>
        )}
      </div>
    </div>
  );
}

