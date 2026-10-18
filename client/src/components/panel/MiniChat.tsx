/** 主对话小窗（plan-20261016-ux 问题 10）：右侧面板全屏时，右下角浮出当前会话的精简对话，可直接发送。
 *
 * 三种状态（记住用户上次的选择）：
 *  - 隐藏：右下角小胶囊（对话图标 + 运行指示），点击打开，可拖到任意位置；
 *  - 打开：浮窗；点击浮窗外部或按 Esc 收回胶囊——与其它浮层同一交互；标题栏可拖动浮窗；
 *  - 常驻：浮窗一直显示，点击外部不收起，只能手动收起。
 *
 * plan-20261017-goal12 项 1：
 *  - 实时：运行中在末尾给出「正在执行 N 个操作 / 当前状态」，工具调用计数随落库即时更新；
 *  - 贴底：内容高度变化（图片加载、Markdown 重排、流式增长）都会跟随到底部；上滑后出现「新消息」提示；
 *  - 附件：用户消息里的图片显示缩略图（点击放大），文件显示为芯片；输入框支持粘贴 / 选择附件；
 *  - 拖动：标题栏拖动小窗、拖动胶囊，位置以右下角锚点记住，窗口缩放后自动收回可视区。
 *
 * 只展示「对话」本身；工具明细、审批等完整操作仍在主界面（退出全屏即回到原位置）。
 * 发送走与主输入框相同的 sendTurn（运行中自动排队），模式 / 模型 / 思考深度沿用当前会话设置。
 */
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ClipboardEvent, type MutableRefObject, type PointerEvent as ReactPointerEvent, type RefObject } from "react";
import { api, resolveFileUrl, type AttachmentInfo } from "../../api/client";
import { useChatStore } from "../../store/chat";
import { useDraftsStore, normalizeDraftMode } from "../../store/drafts";
import { useMergedStreamText } from "../../store/streamMerged";
import { openGallery } from "../../store/gallery";
import { MarkdownContent } from "../MarkdownContent";
import { StreamingMarkdown } from "../chat/StreamingMarkdown";
import { rewriteDsmlForStream } from "../chat/dsmlStream";
import { msgText } from "../chat/timeline";
import { toolVerb } from "../chat/toolLabels";
import { ToolGlyph } from "../iconkit/ToolGlyphs";
import { attachmentsOf } from "../chat/AttachmentCard";
import { IconArrowDown, IconFileText, IconMessageSquare, IconMinus, IconOperationCluster, IconPaperclip, IconPin, IconSpinner, IconX } from "../icons";
import { IconSendStop } from "../iconkit/MorphIcons";
import { Tooltip } from "../ui";
import type { MessageOut } from "../../api/client";

type MiniState = "hidden" | "open" | "pinned";
const STATE_KEY = "chatcoder.miniChat";
const WIN_ANCHOR_KEY = "chatcoder.miniChat.anchor";
const PILL_ANCHOR_KEY = "chatcoder.miniChat.pillAnchor";
/** 只渲染最近这么多条（小窗是「看一眼、补一句」的场景，完整历史在主界面） */
const MAX_ITEMS = 40;
/** 拖动阈值：指针移动超过该距离才视为拖动，否则仍是点击 */
const DRAG_THRESHOLD = 4;
/** 锚点到视口边缘的最小留白 */
const EDGE = 8;

function loadState(): MiniState {
  try {
    const v = localStorage.getItem(STATE_KEY);
    return v === "hidden" || v === "pinned" ? v : "open";
  } catch { return "open"; }
}

/** 锚点：距视口右、下边缘的距离（窗口缩放后仍贴住同一角） */
interface Anchor { right: number; bottom: number }
const DEFAULT_ANCHOR: Anchor = { right: 16, bottom: 16 };

function clamp(v: number, lo: number, hi: number): number {
  return Math.min(Math.max(v, lo), Math.max(lo, hi));
}
function clampAnchor(a: Anchor): Anchor {
  return {
    right: clamp(a.right, EDGE, window.innerWidth - 120),
    bottom: clamp(a.bottom, EDGE, window.innerHeight - 120),
  };
}
function loadAnchor(key: string): Anchor {
  try {
    const raw = JSON.parse(localStorage.getItem(key) || "null") as Partial<Anchor> | null;
    if (raw && typeof raw.right === "number" && typeof raw.bottom === "number") return clampAnchor({ right: raw.right, bottom: raw.bottom });
  } catch { /* 读不到时用默认锚点 */ }
  return DEFAULT_ANCHOR;
}
function saveAnchor(key: string, a: Anchor) {
  try { localStorage.setItem(key, JSON.stringify(a)); } catch { /* 存储不可用时只在本次生效 */ }
}

/** 拖动：指针按下后超过阈值才开始移动；移动中直接写 DOM（不走 React 重渲染），松手后换算回锚点并保存 */
function useAnchorDrag(anchor: Anchor, setAnchor: (a: Anchor) => void, storageKey: string) {
  const ref = useRef<HTMLElement | null>(null) as MutableRefObject<HTMLElement | null>;
  const drag = useRef<{ id: number; sx: number; sy: number; left: number; top: number; w: number; h: number; moved: boolean } | null>(null);
  /** 刚结束一次拖动：紧随其后的 click 不应触发（胶囊拖动松手不展开） */
  const justDragged = useRef(false);

  const onPointerDown = (e: ReactPointerEvent<HTMLElement>) => {
    if (e.button !== 0) return;
    const el = ref.current;
    if (!el) return;
    // 标题栏里的按钮、输入框仍照常响应，不参与拖动
    const hit = (e.target as HTMLElement).closest("button, input, textarea, select, a, [data-no-drag]");
    if (hit && hit !== el) return;
    const r = el.getBoundingClientRect();
    drag.current = { id: e.pointerId, sx: e.clientX, sy: e.clientY, left: r.left, top: r.top, w: r.width, h: r.height, moved: false };
    e.currentTarget.setPointerCapture(e.pointerId);
  };
  const onPointerMove = (e: ReactPointerEvent<HTMLElement>) => {
    const d = drag.current;
    const el = ref.current;
    if (!d || !el || d.id !== e.pointerId) return;
    const dx = e.clientX - d.sx;
    const dy = e.clientY - d.sy;
    if (!d.moved && Math.hypot(dx, dy) < DRAG_THRESHOLD) return;
    d.moved = true;
    el.classList.add("is-dragging");
    el.style.left = `${clamp(d.left + dx, EDGE, window.innerWidth - d.w - EDGE)}px`;
    el.style.top = `${clamp(d.top + dy, EDGE, window.innerHeight - d.h - EDGE)}px`;
    el.style.right = "auto";
    el.style.bottom = "auto";
  };
  const finish = (e: ReactPointerEvent<HTMLElement>) => {
    const d = drag.current;
    const el = ref.current;
    if (!d || d.id !== e.pointerId) return;
    drag.current = null;
    if (!d.moved || !el) return;
    el.classList.remove("is-dragging");
    const r = el.getBoundingClientRect();
    const next = clampAnchor({ right: window.innerWidth - r.right, bottom: window.innerHeight - r.bottom });
    // 清掉手写的定位，交还给 React 的锚点样式
    el.style.left = "";
    el.style.top = "";
    el.style.right = "";
    el.style.bottom = "";
    justDragged.current = true;
    window.setTimeout(() => { justDragged.current = false; }, 0);
    setAnchor(next);
    saveAnchor(storageKey, next);
  };

  return {
    ref,
    justDragged,
    style: { right: anchor.right, bottom: anchor.bottom } as const,
    handlers: { onPointerDown, onPointerMove, onPointerUp: finish, onPointerCancel: finish },
  };
}

type MiniItem =
  | { kind: "user"; key: string; text: string; atts: AttachmentInfo[] }
  | { kind: "ai"; key: string; text: string }
  | { kind: "tools"; key: string; count: number; last: string }
  | { kind: "image"; key: string; url: string; title: string }
  | { kind: "error"; key: string; text: string };

/** 主线程消息 → 小窗条目：工具调用按段合并，思考 / 工具结果 / 系统消息不展示 */
function buildItems(messages: MessageOut[]): MiniItem[] {
  const out: MiniItem[] = [];
  let tools = 0;
  let toolsKey = "";
  let lastTool = "";
  const flushTools = () => {
    if (tools > 0) out.push({ kind: "tools", key: toolsKey, count: tools, last: lastTool });
    tools = 0;
  };
  for (const m of messages) {
    if (m.thread_id != null) continue;
    const content = (m.content ?? {}) as Record<string, unknown>;
    if (m.sender_type === "user") {
      flushTools();
      const text = msgText(m.content).trim();
      const atts = attachmentsOf(content);
      if (text || atts.length > 0) out.push({ kind: "user", key: `u${m.id}`, text, atts });
      continue;
    }
    if (m.sender_type !== "agent") continue;
    if (m.msg_type === "tool_call") {
      if (tools === 0) toolsKey = `t${m.id}`;
      tools++;
      lastTool = typeof content.tool === "string" ? content.tool : "";
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
      continue;
    }
    // 工具产出的截图（浏览器 / 桌面）：小窗里以缩略图呈现，点击放大
    if (m.msg_type === "tool_result") {
      const img = (content.image ?? null) as { url?: unknown; title?: unknown } | null;
      if (img && typeof img.url === "string" && img.url) {
        flushTools();
        out.push({ kind: "image", key: `i${m.id}`, url: img.url, title: typeof img.title === "string" ? img.title : "截图" });
      }
    }
  }
  flushTools();
  return out.slice(-MAX_ITEMS);
}

function isImageAtt(a: AttachmentInfo): boolean {
  return a.type === "image" || (a.mime_type ?? "").startsWith("image/");
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
  const turnStatus = useChatStore((s) => s.turnStatus);
  /** 运行中的工具数（实时）：工具结果落库前也能看到「正在执行」 */
  const liveTools = useChatStore((s) => Object.keys(s.runningToolResults).length);
  const queued = useChatStore((s) => s.queuedInputs.length);
  const streamText = useMergedStreamText();
  const items = useMemo(() => buildItems(messages), [messages]);
  const shownStream = useMemo(() => (isRunning ? rewriteDsmlForStream(streamText) : ""), [isRunning, streamText]);

  const [input, setInput] = useState("");
  const [atts, setAtts] = useState<AttachmentInfo[]>([]);
  const [uploading, setUploading] = useState(false);
  const [unseen, setUnseen] = useState(false);
  const [winAnchor, setWinAnchor] = useState<Anchor>(() => loadAnchor(WIN_ANCHOR_KEY));
  const [pillAnchor, setPillAnchor] = useState<Anchor>(() => loadAnchor(PILL_ANCHOR_KEY));
  const winDrag = useAnchorDrag(winAnchor, setWinAnchor, WIN_ANCHOR_KEY);
  const pillDrag = useAnchorDrag(pillAnchor, setPillAnchor, PILL_ANCHOR_KEY);

  const rootRef = useRef<HTMLDivElement | null>(null) as MutableRefObject<HTMLDivElement | null>;
  const bodyRef = useRef<HTMLDivElement>(null);
  const flowRef = useRef<HTMLDivElement>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const stickRef = useRef(true);
  const open = state !== "hidden";

  // 视口缩小后把锚点收回可视区（小窗与胶囊共用同一条规则）
  useEffect(() => {
    const onResize = () => {
      setWinAnchor((a) => clampAnchor(a));
      setPillAnchor((a) => clampAnchor(a));
    };
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

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

  /** 贴底：把滚动条拉到底（仅当用户处于贴底状态） */
  const stickToBottom = () => {
    const el = bodyRef.current;
    if (el && stickRef.current) el.scrollTop = el.scrollHeight;
  };

  // 新消息 / 流式增长：贴底时跟随；不贴底时记下「有新消息」
  useLayoutEffect(() => {
    if (stickRef.current) {
      stickToBottom();
      setUnseen(false);
    } else if (items.length > 0 || shownStream) {
      setUnseen(true);
    }
  }, [items, shownStream, open]);

  // 内容高度变化（图片加载、Markdown 重排、表格展开）同样跟随到底部——只靠数据变化会漏掉这些
  useEffect(() => {
    const inner = flowRef.current;
    if (!inner || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => stickToBottom());
    ro.observe(inner);
    return () => ro.disconnect();
  }, [open]);

  const onScroll = () => {
    const el = bodyRef.current;
    if (!el) return;
    stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
    if (stickRef.current) setUnseen(false);
  };
  const jumpToLatest = () => {
    stickRef.current = true;
    setUnseen(false);
    stickToBottom();
  };

  // 输入框随内容增高（最多约 5 行）
  useLayoutEffect(() => {
    const ta = taRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = `${Math.min(ta.scrollHeight, 120)}px`;
  }, [input, open]);

  if (currentSessionId == null) return null;

  const addFiles = async (files: File[]) => {
    if (files.length === 0) return;
    setUploading(true);
    const got: AttachmentInfo[] = [];
    for (const f of files) {
      try { got.push(await api.uploadFile(f)); } catch { /* 单个文件失败不阻断其余附件 */ }
    }
    if (got.length > 0) setAtts((prev) => [...prev, ...got]);
    setUploading(false);
  };

  const onPaste = (e: ClipboardEvent<HTMLTextAreaElement>) => {
    const items = e.clipboardData?.items;
    if (!items) return;
    const files: File[] = [];
    for (let i = 0; i < items.length; i++) {
      if (items[i].kind === "file") {
        const f = items[i].getAsFile();
        if (f) files.push(f);
      }
    }
    if (files.length > 0) {
      e.preventDefault();
      void addFiles(files);
    }
  };

  const openImage = (list: AttachmentInfo[], idx: number) => {
    const imgs = list.filter(isImageAtt);
    openGallery(imgs.map((a) => ({ url: resolveFileUrl(a.url), name: a.filename, size: a.size })), Math.max(0, imgs.indexOf(list[idx])));
  };

  const send = () => {
    const text = input.trim();
    if (!text && atts.length === 0) return;
    const st = useChatStore.getState();
    const mode = normalizeDraftMode(session?.permission_mode);
    const effort = useDraftsStore.getState().getDraft(`s${currentSessionId}`)?.reasoningEffort ?? st.lastReasoningEffort ?? undefined;
    stickRef.current = true;
    // 与主输入框同口径：agent 透传 null（引擎默认全量工具），其余模式名原样透传；运行中由 sendTurn 自动排队
    void st.sendTurn(text, atts as unknown as Record<string, unknown>[], effort, mode === "agent" ? null : mode, session?.model_id ?? st.lastModelId ?? null, []);
    setInput("");
    setAtts([]);
  };
  const stop = () => {
    const st = useChatStore.getState();
    if (st.isRunning) void st.cancelTurn();
    void st.stopSessionExecution();
  };
  const canSend = input.trim().length > 0 || atts.length > 0;
  const sendMode: "send" | "stop" | "queue" = canSend ? (isRunning ? "queue" : "send") : (isRunning ? "stop" : "send");

  /** 运行态文案：优先用后端给出的状态，其次按实时工具数，最后才是泛化的等待 */
  const liveLine = turnStatus
    || (liveTools > 0 ? `正在执行 ${liveTools} 个操作` : shownStream ? "处理中…" : "等待响应…");

  if (!open) {
    return (
      <button
        ref={pillDrag.ref as RefObject<HTMLButtonElement>}
        type="button"
        className="mini-chat-pill"
        style={pillDrag.style}
        onPointerDown={pillDrag.handlers.onPointerDown}
        onPointerMove={pillDrag.handlers.onPointerMove}
        onPointerUp={pillDrag.handlers.onPointerUp}
        onPointerCancel={pillDrag.handlers.onPointerCancel}
        onClick={() => { if (!pillDrag.justDragged.current) setMini("open"); }}
        aria-label="打开对话小窗（可拖动）"
      >
        <IconMessageSquare size={16} />
        <span>对话</span>
        {isRunning && <IconSpinner size={12} />}
      </button>
    );
  }

  return (
    <div
      ref={(el) => { rootRef.current = el; winDrag.ref.current = el; }}
      className={"mini-chat" + (state === "pinned" ? " is-pinned" : "")}
      style={winDrag.style}
      role="dialog"
      aria-label="主对话小窗"
    >
      <div
        className="mini-chat-head"
        onPointerDown={winDrag.handlers.onPointerDown}
        onPointerMove={winDrag.handlers.onPointerMove}
        onPointerUp={winDrag.handlers.onPointerUp}
        onPointerCancel={winDrag.handlers.onPointerCancel}
      >
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
        <div className="mini-chat-flow" ref={flowRef}>
          {items.length === 0 && !shownStream && (
            <div className="mini-chat-empty">还没有对话，在下方输入开始</div>
          )}
          {items.map((it) => {
            if (it.kind === "user") {
              return (
                <div key={it.key} className="mini-chat-user-wrap">
                  {it.atts.length > 0 && (
                    <div className="mini-chat-atts">
                      {it.atts.map((a, i) => isImageAtt(a) ? (
                        <button key={`${a.url}-${i}`} type="button" className="mini-chat-thumb" aria-label={`查看图片 ${a.filename}`} onClick={() => openImage(it.atts, i)}>
                          <img src={resolveFileUrl(a.url)} alt={a.filename} loading="lazy" decoding="async" />
                        </button>
                      ) : (
                        <Tooltip key={`${a.url}-${i}`} content={a.filename} side="top">
                          <span className="mini-chat-file">
                            <IconFileText size={13} />
                            <span>{a.filename}</span>
                          </span>
                        </Tooltip>
                      ))}
                    </div>
                  )}
                  {it.text && <div className="mini-chat-user">{it.text}</div>}
                </div>
              );
            }
            if (it.kind === "tools") {
              return (
                // plan-20261017-ux-overhaul：与主消息流同一套工具图标与可读动作名，不再只给一个笼统计数
                <div key={it.key} className="mini-chat-tools">
                  <span className="mini-chat-tools-ic">
                    {it.last ? <ToolGlyph tool={it.last} size={14} /> : <IconOperationCluster size={14} />}
                  </span>
                  <span>
                    {it.last ? toolVerb(it.last) : "执行了操作"}
                    {it.count > 1 && <em className="mini-chat-tools-more">等 {it.count} 个操作</em>}
                  </span>
                </div>
              );
            }
            if (it.kind === "error") return <div key={it.key} className="mini-chat-error">{it.text}</div>;
          if (it.kind === "image") {
            return (
              <div key={it.key} className="mini-chat-shot">
                <button type="button" className="mini-chat-thumb is-shot" aria-label={`查看${it.title}`}
                  onClick={() => openGallery([{ url: resolveFileUrl(it.url), name: it.title }], 0)}>
                  <img src={resolveFileUrl(it.url)} alt={it.title} loading="lazy" decoding="async" />
                </button>
              </div>
            );
          }
            // 复用消息流的 Markdown 作用域（行内代码 / 代码块 / 表格同款样式），行距在 .mini-chat 里收紧
            return <div key={it.key} className="mini-chat-ai turn-agent-text"><MarkdownContent>{it.text}</MarkdownContent></div>;
          })}
          {shownStream && (
            <div className="mini-chat-ai turn-agent-text is-streaming"><StreamingMarkdown>{shownStream}</StreamingMarkdown></div>
          )}
          {isRunning && (
            <div className="mini-chat-tools is-running" role="status">
              <IconSpinner size={12} />
              <span>{liveLine}</span>
            </div>
          )}
        </div>
      </div>
      {unseen && !stickRef.current && (
        <button type="button" className="mini-chat-jump" onClick={jumpToLatest}>
          <IconArrowDown size={13} />
          <span>有新消息</span>
        </button>
      )}
      {atts.length > 0 && (
        <div className="mini-chat-pending" aria-label="待发送附件">
          {atts.map((a, i) => (
            <span key={`${a.url}-${i}`} className={"mini-chat-pending-item" + (isImageAtt(a) ? " is-image" : "")}>
              {isImageAtt(a)
                ? <img src={resolveFileUrl(a.url)} alt={a.filename} />
                : <IconFileText size={13} />}
              <span className="mini-chat-pending-name">{a.filename}</span>
              <button type="button" className="mini-chat-pending-x" aria-label={`移除 ${a.filename}`}
                onClick={() => setAtts((prev) => prev.filter((_, j) => j !== i))}>
                <IconX size={10} />
              </button>
            </span>
          ))}
        </div>
      )}
      <form
        className="mini-chat-composer"
        onSubmit={(e) => { e.preventDefault(); if (canSend) send(); else if (isRunning) stop(); }}
      >
        <input
          ref={fileRef}
          type="file"
          multiple
          hidden
          onChange={(e) => {
            const files = Array.from(e.target.files ?? []);
            e.target.value = "";
            void addFiles(files);
          }}
        />
        <Tooltip content="添加附件（也可直接粘贴）" side="top">
          <button type="button" className="mini-chat-attach" aria-label="添加附件" disabled={uploading} onClick={() => fileRef.current?.click()}>
            {uploading ? <IconSpinner size={13} /> : <IconPaperclip size={15} />}
          </button>
        </Tooltip>
        <textarea
          ref={taRef}
          className="mini-chat-input"
          rows={1}
          value={input}
          placeholder={isRunning ? "补充要求，当前这轮结束后发送" : "继续对话，Enter 发送"}
          aria-label="对话小窗输入框"
          onChange={(e) => setInput(e.target.value)}
          onPaste={onPaste}
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
