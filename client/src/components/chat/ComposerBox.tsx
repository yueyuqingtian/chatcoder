/** ComposerBox（v5）：对话态输入框，textarea 独占一行，工具栏在底部。 */
import { useCallback, useEffect, useRef, useState } from "react";
import { api, type AttachmentInfo, type ModelOut, resolveFileUrl } from "../../api/client";
import { useChatStore, type UsageDetail } from "../../store/chat";
import { ModelPicker } from "./ModelPicker";
import {
  IconArrowUp, IconStop, IconMic, IconPaperclip,
  IconBrain, IconX, IconPlus, IconShield, IconChevronDown,
} from "../icons";

const REASONING_STORAGE_PREFIX = "reasoning:";
const MODE_STORAGE_PREFIX = "composer-mode:";
const MAX_TEXTAREA_HEIGHT = 200;

/** 剪贴板中的文件可能没有文件名（如直接复制图片像素），生成一个可读的默认名。 */
function namePastedFile(f: File): File {
  if (f.name && f.name.trim() && f.name !== "image.png") return f;
  const ext = (f.type.split("/")[1] || "png").replace(/[^a-z0-9]/gi, "") || "png";
  const ts = new Date().toISOString().replace(/[-:T]/g, "").slice(0, 14);
  return new File([f], `paste-${ts}.${ext}`, { type: f.type || "image/png" });
}

const SLASH_COMMANDS = [
  { cmd: "/chat", desc: "只读审阅模式" },
  { cmd: "/clear", desc: "清空当前对话" },
  { cmd: "/compact", desc: "压缩上下文" },
  { cmd: "/init", desc: "初始化项目文档" },
];

export function ComposerBox() {
  const currentSessionId = useChatStore((s) => s.currentSessionId);
  const isRunning = useChatStore((s) => s.isRunning);
  const usage = useChatStore((s) => s.usage);
  const isCompacting = useChatStore((s) => s.isCompacting);
 const sendTurn = useChatStore((s) => s.sendTurn);
 const forceStop = useChatStore((s) => s.forceStop);
 const pendingApproval = useChatStore((s) => s.pendingApproval);
 const respondApproval = useChatStore((s) => s.respondApproval);
 const queuedInputs = useChatStore((s) => s.queuedInputs);
 const updateQueuedInput = useChatStore((s) => s.updateQueuedInput);

  const [input, setInput] = useState("");
  const [attachments, setAttachments] = useState<AttachmentInfo[]>([]);
  const [uploading, setUploading] = useState(false);
  const [models, setModels] = useState<ModelOut[]>([]);
  const [sessionModelId, setSessionModelId] = useState<number | null>(null);
  const [reasoningEffort, setReasoningEffort] = useState<string | null>(null);
  const [showModels, setShowModels] = useState(false);
  const [showReasoning, setShowReasoning] = useState(false);
  const [showSlash, setShowSlash] = useState(false);
  const [slashIndex, setSlashIndex] = useState(0);
  const [showAt, setShowAt] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const [listening, setListening] = useState(false);
  // v2.2: 权限模式三态（执行/规划/只读，对齐 ZCode default/plan/acceptEdits 的模式切换器）
  const [composerMode, setComposerMode] = useState<"default" | "plan" | "readonly">("default");

  const taRef = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const recogRef = useRef<{ stop: () => void } | null>(null);

  useEffect(() => {
    let cancelled = false;
    if (!currentSessionId) { setSessionModelId(null); return; }
    (async () => {
      try {
        const ms = await api.listModels();
        if (cancelled) return;
        setModels(ms);
        let s = null;
        try { s = await api.getSession(currentSessionId); } catch { /* ignore */ }
        if (cancelled) return;
        if (s && s.model_id != null) {
          // 会话已绑定模型：跟随会话
          setSessionModelId(s.model_id);
        } else if (ms.length > 0) {
          // 新会话/未绑定模型：仅本地回显第一个可用模型，不写库——
          // 避免与空态首页的 updateSession 竞态把用户已选模型覆盖掉，
          // 也避免产生多余的「模型已切换」系统消息。
          setSessionModelId(ms[0].id);
        }
      } catch { /* ignore */ }
    })();
    return () => { cancelled = true; };
  }, [currentSessionId]);

  // 外部模型变更同步（空态首页选模型/设置页改绑定等）：store 中会话的 model_id 变化时跟随
  const storeSessionModelId = useChatStore((s) => s.sessions.find((x) => x.id === currentSessionId)?.model_id ?? null);
  useEffect(() => {
    if (storeSessionModelId != null && storeSessionModelId !== sessionModelId) {
      setSessionModelId(storeSessionModelId);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [storeSessionModelId]);

  // v2.2 (对齐 zcode 3.8.3): 命令中心（Cmd+K）插入斜杠命令 / 聚焦输入框
  useEffect(() => {
    const onInsert = (e: Event) => {
      const cmd = (e as CustomEvent<{ cmd?: string }>).detail?.cmd;
      if (cmd) {
        setInput(cmd + " ");
        setShowSlash(false);
        setShowAt(false);
      }
      setTimeout(() => taRef.current?.focus(), 0);
    };
    const onFocus = () => { setTimeout(() => taRef.current?.focus(), 0); };
    window.addEventListener("chatcoder:insert-slash", onInsert);
    window.addEventListener("chatcoder:focus-composer", onFocus);
    return () => {
      window.removeEventListener("chatcoder:insert-slash", onInsert);
      window.removeEventListener("chatcoder:focus-composer", onFocus);
    };
  }, []);

  useEffect(() => {
    if (currentSessionId) {
      // 优先会话级记忆；无则承接全局最近选择（空态首页选的档位跨入会话后不丢失）
      const saved = localStorage.getItem(REASONING_STORAGE_PREFIX + currentSessionId)
        ?? useChatStore.getState().lastReasoningEffort;
      setReasoningEffort(saved);
    } else {
      setReasoningEffort(null);
    }
  }, [currentSessionId]);

  // v2.2: 模式切换器状态按会话持久化（per session）
  useEffect(() => {
    if (currentSessionId) {
      const saved = localStorage.getItem(MODE_STORAGE_PREFIX + currentSessionId);
      setComposerMode(saved === "plan" || saved === "readonly" ? saved : "default");
    } else {
      setComposerMode("default");
    }
  }, [currentSessionId]);

  // v2.2 (对齐 zcode 3.12): Shift+Tab 循环权限模式（ZCode 同款快捷键）
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Tab" && e.shiftKey) {
        const target = e.target as HTMLElement | null;
        // 输入框内不劫持（避免打断正常焦点导航）
        if (target && (target.tagName === "TEXTAREA" || target.tagName === "INPUT")) return;
        e.preventDefault();
        const next = composerMode === "default" ? "plan" : composerMode === "plan" ? "readonly" : "default";
        setMode(next);
      }
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [composerMode, currentSessionId]);

  useEffect(() => {
    const ta = taRef.current;
    if (ta) {
      ta.style.height = "auto";
      ta.style.height = `${Math.min(ta.scrollHeight, MAX_TEXTAREA_HEIGHT)}px`;
    }
  }, [input]);

  // 全局弹窗点击外部关闭：模型菜单/思考深度菜单/斜杠菜单/@菜单
  useEffect(() => {
    if (!showModels && !showReasoning && !showSlash && !showAt) return;
    const handler = (e: MouseEvent) => {
      const target = e.target as Element;
      const withinMenu = target.closest?.(".composer-menu") || target.closest?.(".composer-model-badge") || target.closest?.(".composer-reasoning-btn") || target.closest?.(".composer-usage-ring") || target.closest?.(".composer-input") || target.closest?.(".composer-main");
      if (!withinMenu) {
        setShowModels(false);
        setShowReasoning(false);
        setShowSlash(false);
        setShowAt(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [showModels, showReasoning, showSlash, showAt]);

  const activeModel = models.find((m) => m.id === sessionModelId);
  const supportsReasoning = activeModel?.reasoning_efforts && activeModel.reasoning_efforts.length > 0;
  // v14: 只发附件（不带文字）也可发送
  // v2.2: 运行中也可发送——消息进入排队队列，turn 完成后自动续发
  const canSend = (input.trim().length > 0 || attachments.length > 0) && !uploading;

  const changeModel = async (modelId: number) => {
    setSessionModelId(modelId);
    setShowModels(false);
    if (currentSessionId) {
      // 同步 store，保持与「外部变更跟随」效果一致（防止被回弹成旧模型）
      useChatStore.setState((st) => ({ sessions: st.sessions.map((x) => (x.id === currentSessionId ? { ...x, model_id: modelId } : x)) }));
      try { await api.updateSession(currentSessionId, { model_id: modelId }); } catch { /* ignore */ }
    }
  };

  const changeReasoning = (effort: string | null) => {
    setReasoningEffort(effort);
    // 同步全局最近选择（空态首页 / 跨会话承接）
    useChatStore.setState({ lastReasoningEffort: effort });
    if (currentSessionId) {
      if (effort) localStorage.setItem(REASONING_STORAGE_PREFIX + currentSessionId, effort);
      else localStorage.removeItem(REASONING_STORAGE_PREFIX + currentSessionId);
    }
    setShowReasoning(false);
  };

  const handleSend = () => {
    if (!canSend) return;
    let content = input.trim();
    // /plan 先规划后执行；/chat 只读审阅（显式斜杠命令优先于模式切换器）
    let mode: "readonly" | "plan" | null = null;
    if (content.startsWith("/plan")) { mode = "plan"; content = content.replace(/^\/plan\s*/, "").trim(); }
    else if (content.startsWith("/chat")) { mode = "readonly"; content = content.replace(/^\/chat\s*/, "").trim(); }
    else if (composerMode === "plan") mode = "plan";
    else if (composerMode === "readonly") mode = "readonly";
    // v14: 允许只发附件不带文字；纯命令（/plan、/chat）仍需文字
    if (!content && attachments.length === 0) return;
    const atts = attachments.length > 0 ? attachments.map((a) => ({ ...a })) : undefined;
    sendTurn(content, atts, reasoningEffort ?? undefined, mode);
    setInput("");
    setAttachments([]);
    setShowSlash(false);
    setShowAt(false);
  };

  const [showModeMenu, setShowModeMenu] = useState(false);
  // v1.1: 停止按钮三态——点击后进入"正在停止"，防止重复点击与状态闪跳
  const [stopping, setStopping] = useState(false);
  useEffect(() => { if (!isRunning) setStopping(false); }, [isRunning]);
  const setMode = (next: "default" | "plan" | "readonly") => {
    setComposerMode(next);
    setShowModeMenu(false);
    if (currentSessionId) {
      localStorage.setItem(MODE_STORAGE_PREFIX + currentSessionId, next);
      // v2.2 (对齐 zcode 3.12): 同步会话权限模式，后端审批门即时生效
      api.updateSession(currentSessionId, { permission_mode: next }).catch(() => {});
    }
  };

  const handleCancel = () => {
    setStopping(true);
    forceStop();
    setTimeout(() => setStopping(false), 5000);
  };

  // v14: 上传优先——文件先 POST /api/upload 落盘拿到文件地址，
  // 附件结构从 data_url 改为 {file_id, path, url, ...}，发送时后端直接使用地址
  const addFiles = useCallback(async (files: FileList | File[]) => {
    const arr = Array.from(files);
    if (arr.length === 0) return;
    setUploading(true);
    const uploaded: AttachmentInfo[] = [];
    const failed: string[] = [];
    try {
      for (const f of arr) {
        try {
          const up = await api.uploadFile(f);
          uploaded.push({
            file_id: up.file_id, filename: up.filename, path: up.path,
            url: up.url, size: up.size, mime_type: up.mime_type, type: up.type,
          });
        } catch (e) {
          console.warn("[composer] 上传失败", f.name, e);
          failed.push(`${f.name}: ${e instanceof Error ? e.message : String(e)}`);
        }
      }
      if (uploaded.length > 0) setAttachments((prev) => [...prev, ...uploaded]);
      // v16: 上传失败必须给用户可见反馈，不能静默吞掉（此前后端缺路由时表现为"选了文件没反应"）
      if (failed.length > 0) {
        useChatStore.setState({ error: `附件上传失败（${failed.length} 个）\n${failed.join("\n")}` });
      }
    } finally {
      setUploading(false);
    }
  }, []);

  // v16: 粘贴图片/文件——textarea 默认不处理剪贴板中的图片/文件，
  // 必须显式从 clipboardData.items 提取 File 后走上传流程
  const handlePaste = useCallback((e: React.ClipboardEvent<HTMLTextAreaElement>) => {
    const items = e.clipboardData?.items;
    if (!items || items.length === 0) return;
    const files: File[] = [];
    for (const item of Array.from(items)) {
      if (item.kind === "file") {
        const f = item.getAsFile();
        if (f) files.push(namePastedFile(f));
      }
    }
    if (files.length > 0) {
      e.preventDefault();
      void addFiles(files);
    }
  }, [addFiles]);

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setDragOver(false);
    if (e.dataTransfer.files.length > 0) addFiles(e.dataTransfer.files);
  };

  const toggleVoice = () => {
    if (listening) { recogRef.current?.stop(); return; }
    const SR = (window as unknown as { webkitSpeechRecognition?: new () => unknown }).webkitSpeechRecognition;
    if (!SR) { alert("当前浏览器不支持语音识别"); return; }
    const r = new SR() as { lang: string; continuous: boolean; interimResults: boolean; onresult: (e: { results: ArrayLike<ArrayLike<{ transcript: string }>> }) => void; onend: () => void; onerror: () => void; start: () => void; stop: () => void; };
    r.lang = "zh-CN"; r.continuous = false; r.interimResults = false;
    r.onresult = (e) => { const text = e.results[0]?.[0]?.transcript ?? ""; if (text) setInput((v) => v + text); };
    r.onend = () => setListening(false);
    r.onerror = () => setListening(false);
    setListening(true);
    r.start();
    recogRef.current = { stop: () => r.stop() };
  };

  const usagePct = usage && usage.context_window ? Math.min(100, Math.round((usage.total / usage.context_window) * 100)) : 0;

  // v17: 斜杠命令——仅在输入为 "/xxx"（无空格）时弹出，并按已输入前缀过滤；
  // 选中命令后输入为 "/clear "（含空格），不再触发弹窗，继续输入文字也不会重弹。
  const slashQuery = showSlash && /^\/\S*$/.test(input) ? input.slice(1).toLowerCase() : "";
  const filteredSlash = slashQuery
    ? SLASH_COMMANDS.filter((c) => c.cmd.slice(1).startsWith(slashQuery))
    : SLASH_COMMANDS;
  const slashVisible = showSlash && filteredSlash.length > 0;
  const pickSlash = (cmd: string) => {
    setInput(cmd + " ");
    setShowSlash(false);
    taRef.current?.focus();
  };

  return (
    <div className={`composer${dragOver ? " drag-over" : ""}`} onDragOver={(e) => { e.preventDefault(); setDragOver(true); }} onDragLeave={(e) => { e.preventDefault(); setDragOver(false); }} onDrop={handleDrop}>
      {/* v2.2: 排队消息胶囊——运行中发送的消息，turn 完成后自动续发（可删除） */}
      {queuedInputs.length > 0 && (
        <div className="composer-queue">
          <div className="composer-queue-title">排队中（当前任务完成后自动发送）</div>
          {queuedInputs.map((q) => (
            <div className="composer-queue-item" key={q.id} title={q.content}>
              <span className="composer-queue-text">{q.content || (q.attachments?.length ? `附件 × ${q.attachments.length}` : "")}</span>
              {q.mode === "plan" && <span className="composer-queue-tag">规划</span>}
              {q.mode === "readonly" && <span className="composer-queue-tag">只读</span>}
              <button className="composer-queue-remove" onClick={() => updateQueuedInput(q.id, null)} title="移出队列" type="button">
                <IconX size={11} />
              </button>
            </div>
          ))}
        </div>
      )}
      {attachments.length > 0 && (
        <div className="composer-attachments">
          {attachments.map((a, i) => (
            <div key={a.file_id || i} className={`composer-attach-chip${a.type === "image" ? " image" : ""}`} title={a.filename}>
              {a.type === "image" ? (
                <img
                  src={resolveFileUrl(a.url)} alt={a.filename}
                  className="attach-chip-thumb"
                  onClick={() => window.open(resolveFileUrl(a.url), "_blank", "noopener")}
                />
              ) : (
                <IconPaperclip size={11} />
              )}
              <span className="attach-chip-name" onClick={() => window.open(resolveFileUrl(a.url), "_blank", "noopener")}>{a.filename}</span>
              <button className="remove" onClick={() => setAttachments((prev) => prev.filter((_, j) => j !== i))}><IconX size={10} /></button>
            </div>
          ))}
          {uploading && <span className="composer-attach-uploading">上传中…</span>}
        </div>
      )}
      <div className="composer-main">
        <textarea
          ref={taRef}
          className="composer-input"
          placeholder="提出后续修改要求"
          value={input}
          rows={2}
          onPaste={handlePaste}
          onChange={(e) => { const v = e.target.value; setInput(v); const isSlash = /^\/\S*$/.test(v); setShowSlash(isSlash); if (isSlash) setSlashIndex(0); setShowAt(v.endsWith("@") || /@\S*$/.test(v)); }}
          onKeyDown={(e) => {
            if (slashVisible) {
              if (e.key === "ArrowDown") { e.preventDefault(); setSlashIndex((i) => (i + 1) % filteredSlash.length); return; }
              if (e.key === "ArrowUp") { e.preventDefault(); setSlashIndex((i) => (i - 1 + filteredSlash.length) % filteredSlash.length); return; }
              if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); pickSlash(filteredSlash[Math.min(slashIndex, filteredSlash.length - 1)].cmd); return; }
              if (e.key === "Escape") { e.preventDefault(); setShowSlash(false); return; }
            }
            if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); handleSend(); }
            if (e.key === "Escape") { setShowSlash(false); setShowAt(false); }
          }}
        />
        <div className="composer-toolbar">
          <div className="composer-tools-left">
            <button className="composer-attach" title="添加附件" onClick={() => fileRef.current?.click()}><IconPlus size={16} /></button>
            {/* v13: 权限模式下拉（对齐 ZCode 输入框左侧「完全访问 ▾」） */}
            <div className="composer-mode-wrap">
              <button
                className={`composer-mode-btn mode-${composerMode}`}
                onClick={() => { setShowModeMenu((v) => !v); setShowModels(false); setShowReasoning(false); }}
                title="权限模式"
              >
                <IconShield size={13} />
                {composerMode === "default" ? "完全访问" : composerMode === "plan" ? "计划模式" : "只读模式"}
                <IconChevronDown size={11} />
              </button>
              {showModeMenu && (
                <div className="composer-menu composer-mode-menu">
                  <div className="composer-menu-title">权限模式</div>
                  <button className={composerMode === "default" ? "active" : ""} onClick={() => setMode("default")}>完全访问</button>
                  <button className={composerMode === "plan" ? "active" : ""} onClick={() => setMode("plan")}>计划模式</button>
                  <button className={composerMode === "readonly" ? "active" : ""} onClick={() => setMode("readonly")}>只读模式</button>
                </div>
              )}
            </div>
            <input ref={fileRef} type="file" multiple style={{ display: "none" }} onChange={(e) => { if (e.target.files) addFiles(e.target.files); e.target.value = ""; }} />
          </div>
          <div className="composer-right">
            <ModelPicker
              models={models}
              value={sessionModelId}
              onChange={changeModel}
              open={showModels}
              onToggle={() => { setShowModels((v) => !v); setShowReasoning(false); }}
            />
            {supportsReasoning && (
              <div className="composer-reasoning">
                <button className="composer-reasoning-btn" onClick={() => { setShowReasoning((v) => !v); setShowModels(false); }} title="思考深度">
                  <IconBrain size={11} />{reasoningEffort || "默认"}
                </button>
                {showReasoning && (
                  <div className="composer-menu composer-reasoning-menu">
                    <div className="composer-menu-title">思考深度</div>
                    {activeModel!.reasoning_efforts.map((effort) => (
                      <button key={effort} className={effort === reasoningEffort ? "active" : ""} onClick={() => changeReasoning(effort)}>{effort}</button>
                    ))}
                    <button className={reasoningEffort === null ? "active" : ""} onClick={() => changeReasoning(null)}>默认</button>
                  </div>
                )}
              </div>
            )}
            <div className="composer-usage">
              <UsageRing pct={usagePct} usage={usage} compacting={isCompacting} />
            </div>
            {isCompacting && (
              <span className="composer-compacting-badge" title="上下文接近窗口上限，正在压缩历史对话">
                正在压缩上下文…
              </span>
            )}
            <button className={`composer-ctx${listening ? " listening" : ""}`} onClick={toggleVoice} title={listening ? "停止录音" : "语音输入"}><IconMic size={15} /></button>
            {isRunning ? (
              <button className="composer-send stop" disabled={stopping} onClick={handleCancel}
                      title={stopping ? "正在停止…" : "停止"}>
                {stopping ? <span className="thinking-block-breath" /> : <IconStop size={14} />}
              </button>
            ) : (
              <button className="composer-send" disabled={!canSend} onClick={handleSend} title="发送"><IconArrowUp size={16} /></button>
            )}
          </div>
        </div>
      </div>
      {dragOver && (<div className="composer-drag-overlay"><IconPaperclip size={24} /><span>松开以添加附件</span></div>)}
      {slashVisible && (
        <div className="composer-menu composer-slash">
          <div className="composer-menu-title">命令</div>
          {filteredSlash.map((item, idx) => (
            <button
              key={item.cmd}
              className={idx === slashIndex ? "active" : ""}
              onMouseEnter={() => setSlashIndex(idx)}
              onClick={() => pickSlash(item.cmd)}
            >
              <span>{item.cmd}</span>
              {item.desc && <span className="composer-slash-desc">{item.desc}</span>}
            </button>
          ))}
        </div>
      )}
      {showAt && (<div className="composer-menu composer-at"><div className="composer-menu-title">提及文件</div><div className="composer-menu-empty">继续输入文件名以补全…</div></div>)}
      {pendingApproval && (
        pendingApproval.detail.kind === "question" ? (
          <QuestionCard
            detail={pendingApproval.detail}
            onCancel={() => respondApproval(pendingApproval.approvalId, false)}
            onSubmit={(answers) => respondApproval(pendingApproval.approvalId, true, false, answers)}
          />
        ) : (
        <div className="approval-overlay">
          <div className="approval-card">
            <div className="approval-title">工具审批请求</div>
            <div className="approval-tool">
              <span className="approval-tool-name">{String(pendingApproval.detail.tool ?? "unknown")}</span>
              <span className={`approval-risk risk-${String(pendingApproval.detail.risk_level ?? "low")}`}>
                {String(pendingApproval.detail.risk_level ?? "low")} 风险
              </span>
            </div>
            {pendingApproval.detail.agent_name != null && (
              <div className="approval-agent">
                {String(pendingApproval.detail.agent_name)} 申请执行此工具
              </div>
            )}
            {pendingApproval.detail.args != null && (
              <pre className="approval-args">{formatApprovalArgs(pendingApproval.detail.args)}</pre>
            )}
            {typeof pendingApproval.detail.summary === "string" && (
              <div className="approval-summary">{pendingApproval.detail.summary}</div>
            )}
            <div className="approval-actions">
              <button className="btn-ghost" onClick={() => respondApproval(pendingApproval.approvalId, false)}>拒绝</button>
              <button className="btn-ghost" onClick={() => respondApproval(pendingApproval.approvalId, true, true)} title="自动生成执行策略规则，同类操作不再询问">始终允许</button>
              <button className="btn-primary" onClick={() => respondApproval(pendingApproval.approvalId, true)}>允许执行</button>
            </div>
          </div>
        </div>
        )
      )}
    </div>
  );
}

/** v2.2 (对齐 zcode 3.14): AskUserQuestion 选项卡——模型发起结构化提问 */
function QuestionCard({ detail, onCancel, onSubmit }: {
  detail: Record<string, unknown>;
  onCancel: () => void;
  onSubmit: (answers: Record<string, unknown>) => void;
}) {
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const questions = (Array.isArray(detail.questions) ? detail.questions : []) as Array<{
    question?: unknown; options?: unknown; allow_custom?: unknown;
  }>;
  const agentName = detail.agent_name != null ? String(detail.agent_name) : "AI";
  const allAnswered = questions.every((_q, i) => {
    const v = answers[String(i)];
    return v != null && v !== "";
  });

  return (
    <div className="approval-overlay">
      <div className="approval-card question-card">
        <div className="approval-title">需要你确认几个问题</div>
        <div className="approval-agent">{agentName} 需要澄清以下问题以继续任务</div>
        {questions.map((q, i) => {
          const options = Array.isArray(q.options) ? (q.options as string[]) : [];
          const allowCustom = Boolean(q.allow_custom);
          const val = answers[String(i)] ?? "";
          return (
            <div className="question-item" key={i}>
              <div className="question-text">{i + 1}. {String(q.question ?? "")}</div>
              {options.length > 0 && (
                <div className="question-options">
                  {options.map((opt) => (
                    <button
                      key={opt}
                      type="button"
                      className={`question-option${val === opt ? " active" : ""}`}
                      onClick={() => setAnswers((prev) => ({ ...prev, [String(i)]: opt }))}
                    >
                      {opt}
                    </button>
                  ))}
                </div>
              )}
              {allowCustom && (
                <input
                  className="question-custom"
                  placeholder="或输入自定义回答…"
                  value={val && !options.includes(val) ? val : ""}
                  onChange={(e) => setAnswers((prev) => ({ ...prev, [String(i)]: e.target.value }))}
                />
              )}
            </div>
          );
        })}
        <div className="approval-actions">
          <button className="btn-ghost" onClick={onCancel}>取消</button>
          <button className="btn-primary" disabled={!allAnswered} onClick={() => onSubmit(answers)}>
            提交回答
          </button>
        </div>
      </div>
    </div>
  );
}

function fmtTokens(n: number): string {
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
}

/** v2.2 (对齐 zcode 3.10): 用量分类中文标签 */
const BREAKDOWN_LABELS: Record<string, string> = {
  system: "系统提示",
  tools: "工具定义",
  history: "历史对话",
  tool_results: "工具结果",
  thinking: "思考",
  input: "当前输入",
};

/** 格式化审批请求中的工具参数（JSON 摘要，命令类参数高亮可读性）。 */
function formatApprovalArgs(args: unknown): string {
  if (typeof args === "string") return args;
  try {
    const str = JSON.stringify(args, null, 2);
    return str.length > 800 ? str.slice(0, 800) + "\n…" : str;
  } catch {
    return String(args);
  }
}

/** token 占用圆环：无文字，hover 显示百分比 tooltip，点击弹窗详情 */
function UsageRing({ pct, usage, compacting }: { pct: number; usage: UsageDetail | null; compacting?: boolean }) {
  const [showDetail, setShowDetail] = useState(false);
  const ringRef = useRef<HTMLButtonElement>(null);
  const radius = 10;
  const circ = 2 * Math.PI * radius;
  const offset = circ - (pct / 100) * circ;
  const color = compacting ? "var(--warning)" : pct > 80 ? "var(--error)" : pct > 50 ? "var(--warning)" : "var(--success)";

  useEffect(() => {
    if (!showDetail) return;
    const handler = (e: MouseEvent) => {
      if (ringRef.current && !ringRef.current.contains(e.target as Node)) {
        setShowDetail(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [showDetail]);

  return (
    <div className="composer-usage-ring-wrap">
      <button
        ref={ringRef}
        className={`composer-usage-ring${compacting ? " compacting" : ""}`}
        title={compacting ? `正在压缩上下文（${pct}%）` : `上下文占用 ${pct}%`}
        onClick={() => setShowDetail((v) => !v)}
        style={{ position: "relative", width: 26, height: 26, display: "flex", alignItems: "center", justifyContent: "center", borderRadius: "50%", border: "none", background: "transparent", cursor: "pointer" }}
      >
        <svg width="26" height="26" viewBox="0 0 26 26">
          <circle cx="13" cy="13" r={radius} fill="none" stroke="var(--border)" strokeWidth="2.5" />
          <circle
            cx="13" cy="13" r={radius} fill="none" stroke={color} strokeWidth="2.5"
            strokeDasharray={circ} strokeDashoffset={offset}
            strokeLinecap="round" transform="rotate(-90 13 13)"
            style={{ transition: "stroke-dashoffset var(--dur-med, 0.3s) ease" }}
          />
        </svg>
      </button>
      {showDetail && (
        <div className="composer-usage-detail" style={{ display: "block" }}>
          {usage ? (
            <>
              <div className="composer-usage-title">上下文占用 {pct}%{usage.source === "est" ? "（估算）" : ""}{compacting ? " · 正在压缩" : ""}</div>
              <div className="composer-usage-row"><span>输入</span><span>{fmtTokens(usage.input)}</span></div>
              <div className="composer-usage-row"><span>缓存输入</span><span>{fmtTokens(usage.cached_input)}</span></div>
              <div className="composer-usage-row"><span>输出</span><span>{fmtTokens(usage.output)}</span></div>
              <div className="composer-usage-row"><span>推理</span><span>{fmtTokens(usage.reasoning_output)}</span></div>
              <div className="composer-usage-row total"><span>当前占用</span><span>{fmtTokens(usage.total)}</span></div>
              <div className="composer-usage-row"><span>窗口</span><span>{fmtTokens(usage.context_window)}</span></div>
              {usage.breakdown && Object.keys(usage.breakdown).length > 0 && (
                <div className="composer-usage-breakdown">
                  <div className="composer-usage-title" style={{ marginTop: 6 }}>用量分类</div>
                  {Object.entries(usage.breakdown).map(([key, val]) => (
                    <div className="composer-usage-row" key={key}>
                      <span>{BREAKDOWN_LABELS[key] ?? key}</span>
                      <span>{fmtTokens(val)}</span>
                    </div>
                  ))}
                </div>
              )}
              {usage.agent_name && <div className="composer-usage-agent">{usage.agent_name}</div>}
              <div className="composer-usage-note">
                重启/切会话后若无运行记录，显示为本地估算值；发送消息后将恢复为 API 真实占用。压缩上下文会使占用合理下降。
              </div>
            </>
          ) : (
            <div className="composer-usage-title" style={{ color: "var(--text-3)" }}>
              暂无上下文占用数据
              <div style={{ fontSize: 10, marginTop: 4, color: "var(--text-3)" }}>发送消息后将显示用量</div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
