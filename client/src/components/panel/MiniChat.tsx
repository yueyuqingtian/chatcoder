/** 主对话小窗（plan-20261016-ux 问题 10）：右侧面板全屏时，右下角浮出当前会话的精简对话，可直接发送。
 *
 * 三种状态（记住用户上次的选择）：
 *  - 隐藏：右下角小胶囊（对话图标 + 运行指示），点击打开；
 *  - 打开：浮窗；点击浮窗外部或按 Esc 收回胶囊——与其它浮层同一交互；
 *  - 常驻：浮窗一直显示，点击外部不收起，只能手动收起。
 *
 * 只展示「对话」本身：用户消息、AI 正文、每段工具调用合成一行「执行了 N 个操作」、流式尾部；
 * 工具明细、附件、引用、审批等完整操作仍在主界面（退出全屏即回到原位置）。
 * 发送走与主输入框相同的 sendTurn（运行中自动排队），模式 / 模型 / 思考深度沿用当前会话设置。
 */
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useChatStore } from "../../store/chat";
import { useDraftsStore, normalizeDraftMode } from "../../store/drafts";
import { useMergedStreamText } from "../../store/streamMerged";
import { MarkdownContent } from "../MarkdownContent";
import { StreamingMarkdown } from "../chat/StreamingMarkdown";
import { rewriteDsmlForStream } from "../chat/dsmlStream";
import { msgText } from "../chat/timeline";
import { IconMessageSquare, IconMinus, IconOperationCluster, IconPin, IconSpinner } from "../icons";
import { IconSendStop } from "../iconkit/MorphIcons";
import { Tooltip } from "../ui";
import type { MessageOut } from "../../api/client";

type MiniState = "hidden" | "open" | "pinned";
const STATE_KEY = "chatcoder.miniChat";
/** 只渲染最近这么多条（小窗是「看一眼、补一句」的场景，完整历史在主界面） */
const MAX_ITEMS = 40;

function loadState(): MiniState {
  try {
    const v = localStorage.getItem(STATE_KEY);
    return v === "hidden" || v === "pinned" ? v : "open";
  } catch { return "open"; }
}

type MiniItem =
  | { kind: "user"; key: string; text: string }
  | { kind: "ai"; key: string; text: string }
  | { kind: "tools"; key: string; count: number }
  | { kind: "error"; key: string; text: string };

/** 主线程消息 → 小窗条目：工具调用按段合并，思考 / 工具结果 / 系统消息不展示 */
function buildItems(messages: MessageOut[]): MiniItem[] {
  const out: MiniItem[] = [];
  let tools = 0;
  let toolsKey = "";
  const flushTools = () => {
    if (tools > 0) out.push({ kind: "tools", key: toolsKey, count: tools });
    tools = 0;
  };
  for (const m of messages) {
    if (m.thread_id != null) continue;
    const content = (m.content ?? {}) as Record<string, unknown>;
    if (m.sender_type === "user") {
      flushTools();
      const text = msgText(m.content).trim();
      const atts = Array.isArray(content.attachments) ? content.attachments.length : 0;
      if (text || atts) out.push({ kind: "user", key: `u${m.id}`, text: text || `[${atts} 个附件]` });
      continue;
    }
    if (m.sender_type !== "agent") continue;
    if (m.msg_type === "tool_call") {
      if (tools === 0) toolsKey = `t${m.id}`;
      tools++;
      continue;
    }
    if (m.msg_type === "text" && !content.thinking) {
      const text = msgText(m.content).trim();
      if (!text) continue;
      flushTools();
      out.push({ kind: "ai", key: `a${m.id}`, text });
      continue;
    }
    if (m.msg_type === "error") {
      flushTools();
      out.push({ kind: "error", key: `e${m.id}`, text: msgText(m.content).trim() || "执行出错" });
    }
  }
  flushTools();
  return out.slice(-MAX_ITEMS);
}

export function MiniChat() {
  const [state, setState] = useState<MiniState>(loadState);
  const setMini = (next: MiniState) => {
    setState(next);
    try { localStorage.setItem(STATE_KEY, next); } catch { /* 存储不可用时只在本次生效 */ }
  };
  const currentSessionId = useChatStore((s) => s.currentSessionId);
  const session = useChatStore((s) => s.sessions.find((x) => x.id === s.currentSessionId));
  const messages = useChatStore((s) => s.messages);
  const isRunning = useChatStore((s) => s.isRunning || s.runningTurnId != null);
  const queued = useChatStore((s) => s.queuedInputs.length);
  const streamText = useMergedStreamText();
  const items = useMemo(() => buildItems(messages), [messages]);
  const shownStream = useMemo(() => (isRunning ? rewriteDsmlForStream(streamText) : ""), [isRunning, streamText]);

  const [input, setInput] = useState("");
  const rootRef = useRef<HTMLDivElement>(null);
  const bodyRef = useRef<HTMLDivElement>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);
  const stickRef = useRef(true);
  const open = state !== "hidden";

  // 打开（非常驻）时：点击浮窗外部或 Esc 收回胶囊
  useEffect(() => {
    if (state !== "open") return;
    const onDown = (e: PointerEvent) => {
      const t = e.target as Node | null;
      if (t && rootRef.current?.contains(t)) return;
      // 浮层（下拉 / 弹窗）不算外部：小窗里的表格「展开」等弹窗挂在 body 下
      if ((t as HTMLElement | null)?.closest?.("[data-radix-popper-content-wrapper], [role='dialog']")) return;
      setMini("hidden");
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setMini("hidden"); };
    document.addEventListener("pointerdown", onDown, true);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onDown, true);
      document.removeEventListener("keydown", onKey);
    };
  }, [state]);

  // 贴底跟随：新消息 / 流式增长时保持在底部；用户上滑后不打扰，滚回底部恢复
  useLayoutEffect(() => {
    const el = bodyRef.current;
    if (el && stickRef.current) el.scrollTop = el.scrollHeight;
  }, [items, shownStream, open]);
  const onScroll = () => {
    const el = bodyRef.current;
    if (el) stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
  };

  // 输入框随内容增高（最多约 5 行）
  useLayoutEffect(() => {
    const ta = taRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = `${Math.min(ta.scrollHeight, 120)}px`;
  }, [input, open]);

  if (currentSessionId == null) return null;

  const send = () => {
    const text = input.trim();
    if (!text) return;
    const st = useChatStore.getState();
    const mode = normalizeDraftMode(session?.permission_mode);
    const effort = useDraftsStore.getState().getDraft(`s${currentSessionId}`)?.reasoningEffort ?? st.lastReasoningEffort ?? undefined;
    stickRef.current = true;
    // 与主输入框同口径：agent 透传 null（引擎默认全量工具），其余模式名原样透传；运行中由 sendTurn 自动排队
    void st.sendTurn(text, [], effort, mode === "agent" ? null : mode, session?.model_id ?? st.lastModelId ?? null, []);
    setInput("");
  };
  const stop = () => {
    const st = useChatStore.getState();
    if (st.isRunning) void st.cancelTurn();
    void st.stopSessionExecution();
  };
  const canSend = input.trim().length > 0;
  const sendMode: "send" | "stop" | "queue" = canSend ? (isRunning ? "queue" : "send") : (isRunning ? "stop" : "send");

  if (!open) {
    return (
      <button type="button" className="mini-chat-pill" onClick={() => setMini("open")} aria-label="打开对话小窗">
        <IconMessageSquare size={16} />
        <span>对话</span>
        {isRunning && <IconSpinner size={12} />}
      </button>
    );
  }

  return (
    <div ref={rootRef} className={"mini-chat" + (state === "pinned" ? " is-pinned" : "")} role="dialog" aria-label="主对话小窗">
      <div className="mini-chat-head">
        <IconMessageSquare size={16} />
        <span className="mini-chat-title">{session?.title || "当前对话"}</span>
        <span className="mini-chat-state" data-running={isRunning ? "1" : "0"}>
          {isRunning ? "执行中" : queued > 0 ? `${queued} 条待发送` : ""}
        </span>
        <Tooltip content={state === "pinned" ? "取消常驻（点击外部会收起）" : "常驻显示"} side="bottom">
          <button
            type="button"
            className={"ui-icon-btn mini-chat-btn" + (state === "pinned" ? " is-on" : "")}
            aria-label="常驻显示"
            aria-pressed={state === "pinned"}
            onClick={() => setMini(state === "pinned" ? "open" : "pinned")}
          >
            <IconPin size={15} />
          </button>
        </Tooltip>
        <Tooltip content="收起" side="bottom">
          <button type="button" className="ui-icon-btn mini-chat-btn" aria-label="收起对话小窗" onClick={() => setMini("hidden")}>
            <IconMinus size={15} />
          </button>
        </Tooltip>
      </div>
      <div className="mini-chat-body" ref={bodyRef} onScroll={onScroll}>
        {items.length === 0 && !shownStream && (
          <div className="mini-chat-empty">还没有对话，在下方输入开始</div>
        )}
        {items.map((it) => {
          if (it.kind === "user") return <div key={it.key} className="mini-chat-user">{it.text}</div>;
          if (it.kind === "tools") {
            return (
              <div key={it.key} className="mini-chat-tools">
                <IconOperationCluster size={14} />
                <span>执行了 {it.count} 个操作</span>
              </div>
            );
          }
          if (it.kind === "error") return <div key={it.key} className="mini-chat-error">{it.text}</div>;
          // 复用消息流的 Markdown 作用域（行内代码 / 代码块 / 表格同款样式），行距在 .mini-chat 里收紧
          return <div key={it.key} className="mini-chat-ai turn-agent-text"><MarkdownContent>{it.text}</MarkdownContent></div>;
        })}
        {shownStream && (
          <div className="mini-chat-ai turn-agent-text is-streaming"><StreamingMarkdown>{shownStream}</StreamingMarkdown></div>
        )}
        {isRunning && (
          <div className="mini-chat-tools is-running">
            <IconSpinner size={12} />
            <span>{shownStream ? "处理中…" : "等待响应…"}</span>
          </div>
        )}
      </div>
      <form
        className="mini-chat-composer"
        onSubmit={(e) => { e.preventDefault(); if (canSend) send(); else if (isRunning) stop(); }}
      >
        <textarea
          ref={taRef}
          className="mini-chat-input"
          rows={1}
          value={input}
          placeholder={isRunning ? "补充要求，当前这轮结束后发送" : "继续对话，Enter 发送"}
          aria-label="对话小窗输入框"
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            // 与主输入框一致：Enter 发送、Shift+Enter 换行，输入法组字中不发送
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing && e.keyCode !== 229) {
              e.preventDefault();
              if (canSend) send();
            }
          }}
        />
        <Tooltip content={sendMode === "stop" ? "停止" : sendMode === "queue" ? "排队发送（Enter）" : "发送（Enter）"} side="top">
          <button
            type="submit"
            className={"cc-send" + (sendMode === "queue" ? " is-queue" : "")}
            disabled={!canSend && !isRunning}
            aria-label={sendMode === "stop" ? "停止" : "发送"}
          >
            <IconSendStop mode={sendMode} size={16} />
          </button>
        </Tooltip>
      </form>
    </div>
  );
}
