/** ComposerCore（v19 插件化）：全局共用输入框组件。
 * variant="chat"：对话态（排队胶囊、审批覆盖层、停止按钮）；
 * variant="home"：空态首页（项目选择 chip，发送时先 createSession 再 sendTurn）。
 * 两种变体共享 textarea/附件/模型/思考深度/权限模式/占用圆环等全部核心逻辑。
 */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { api, type AttachmentInfo, type ModelOut, resolveFileUrl } from "../../api/client";
import { useChatStore, type UsageDetail } from "../../store/chat";
import { ModelPicker } from "./ModelPicker";
import { Modal } from "../Modal";
import {
  IconArrowUp, IconStop, IconMic, IconPaperclip,
  IconBrain, IconX, IconPlus, IconShield, IconChevronDown, IconFolder,
} from "../icons";

const REASONING_STORAGE_PREFIX = "reasoning:";
const MODE_STORAGE_PREFIX = "composer-mode:";
const DRAFT_STORAGE_PREFIX = "composer-draft:";
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
  { cmd: "/plan", desc: "制定执行计划" },
];

export function ComposerCore({ variant, onStarted }: {
  variant: "home" | "chat";
  onStarted?: () => void;
}) {
  const isHome = variant === "home";
  const currentSessionId = useChatStore((s) => s.currentSessionId);
  const isRunning = useChatStore((s) => s.isRunning);
  const usage = useChatStore((s) => s.usage);
  const isCompacting = useChatStore((s) => s.isCompacting);
  const sendTurn = useChatStore((s) => s.sendTurn);
  const forceStop = useChatStore((s) => s.forceStop);
  const pendingApproval = useChatStore((s) => s.pendingApproval);
  const pendingPlan = useChatStore((s) => s.pendingPlan);
  const confirmPlan = useChatStore((s) => s.confirmPlan);
  const dismissPlan = useChatStore((s) => s.dismissPlan);
  const respondApproval = useChatStore((s) => s.respondApproval);
  const queuedInputs = useChatStore((s) => s.queuedInputs);
  const updateQueuedInput = useChatStore((s) => s.updateQueuedInput);
  // home 变体：项目选择
  const projects = useChatStore((s) => s.projects);
  const currentProjectId = useChatStore((s) => s.currentProjectId);
  const createSession = useChatStore((s) => s.createSession);
  const createProject = useChatStore((s) => s.createProject);

  const [input, setInput] = useState("");
  const [attachments, setAttachments] = useState<AttachmentInfo[]>([]);
  const [preview, setPreview] = useState<AttachmentInfo | null>(null);
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
  const [composerMode, setComposerMode] = useState<"default" | "plan" | "readonly">("default");
  const [selectedProject, setSelectedProject] = useState<number | null>(currentProjectId);
  const [showProjectMenu, setShowProjectMenu] = useState(false);

  const activeProjects = useMemo(() => projects.filter((p) => !p.archived), [projects]);
  const validProjectId = useCallback((id: number | null) =>
    id != null && activeProjects.some((p) => p.id === id) ? id : null, [activeProjects]);
  const activeProjectId = validProjectId(selectedProject) ?? validProjectId(currentProjectId);

  useEffect(() => {
    if (!isHome) return;
    const next = validProjectId(selectedProject) ?? validProjectId(currentProjectId);
    if (next !== selectedProject) setSelectedProject(next);
  }, [currentProjectId, isHome, selectedProject, validProjectId]);
  const [addingDir, setAddingDir] = useState(false);
  const [sending, setSending] = useState(false);

  const taRef = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const recogRef = useRef<{ stop: () => void } | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const ms = await api.listModels();
        if (cancelled) return;
        setModels(ms);
        if (!currentSessionId) { setSessionModelId(ms[0]?.id ?? null); return; }
        let s = null;
        try { s = await api.getSession(currentSessionId); } catch { /* ignore */ }
        if (cancelled) return;
        if (s && s.model_id != null) setSessionModelId(s.model_id);
        else if (ms.length > 0) setSessionModelId(ms[0].id);
      } catch { /* ignore */ }
    })();
    return () => { cancelled = true; };
  }, [currentSessionId]);

  const storeSessionModelId = useChatStore((s) => s.sessions.find((x) => x.id === currentSessionId)?.model_id ?? null);
  useEffect(() => {
    if (storeSessionModelId != null && storeSessionModelId !== sessionModelId) {
      setSessionModelId(storeSessionModelId);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [storeSessionModelId]);

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

  const draftKey = `${DRAFT_STORAGE_PREFIX}${isHome ? "home" : (currentSessionId ?? "new")}`;
  const clearDraft = useCallback(() => {
    // 发送成功后清掉“未入会话”阶段（home/new）的草稿，避免下次新建任务残留上次内容
    try {
      localStorage.removeItem(`${DRAFT_STORAGE_PREFIX}home`);
      localStorage.removeItem(`${DRAFT_STORAGE_PREFIX}new`);
      localStorage.removeItem(draftKey);
    } catch { /* ignore */ }
    useChatStore.setState({ composerDraft: "" });
  }, [draftKey]);
  useEffect(() => {
    try {
      const saved = localStorage.getItem(draftKey);
      const draft = saved ? JSON.parse(saved) as { input?: string; attachments?: AttachmentInfo[] } : null;
      setInput(draft?.input ?? useChatStore.getState().composerDraft ?? "");
      setAttachments(Array.isArray(draft?.attachments) ? draft.attachments : []);
    } catch {
      setInput(useChatStore.getState().composerDraft ?? "");
      setAttachments([]);
    }
  }, [draftKey]);
  useEffect(() => {
    try { localStorage.setItem(draftKey, JSON.stringify({ input, attachments })); } catch { /* storage quota/private mode */ }
  }, [attachments, draftKey, input]);

  useEffect(() => {
    if (currentSessionId) {
      const saved = localStorage.getItem(REASONING_STORAGE_PREFIX + currentSessionId)
        ?? useChatStore.getState().lastReasoningEffort;
      setReasoningEffort(saved);
    } else {
      setReasoningEffort(useChatStore.getState().lastReasoningEffort);
    }
  }, [currentSessionId]);

  useEffect(() => {
    if (currentSessionId) {
      const saved = localStorage.getItem(MODE_STORAGE_PREFIX + currentSessionId);
      setComposerMode(saved === "plan" || saved === "readonly" ? saved : "default");
    } else {
      setComposerMode("default");
    }
  }, [currentSessionId]);

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Tab" && e.shiftKey) {
        const target = e.target as HTMLElement | null;
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

  const resizeTextarea = useCallback(() => {
    const ta = taRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = `${Math.min(Math.max(ta.scrollHeight, 44), MAX_TEXTAREA_HEIGHT)}px`;
    ta.style.overflowY = ta.scrollHeight > MAX_TEXTAREA_HEIGHT ? "auto" : "hidden";
  }, []);

  useLayoutEffect(() => {
    resizeTextarea();
  }, [input, resizeTextarea]);

  useEffect(() => {
    if (!showModels && !showReasoning && !showSlash && !showAt && !showProjectMenu) return;
    const handler = (e: MouseEvent) => {
      const target = e.target as Element;
      const withinMenu = target.closest?.(".composer-menu") || target.closest?.(".composer-model-badge") || target.closest?.(".composer-reasoning-btn") || target.closest?.(".composer-usage-ring") || target.closest?.(".composer-input") || target.closest?.(".composer-main") || target.closest?.(".es-project-trigger") || target.closest?.(".es-project-menu");
      if (!withinMenu) {
        setShowModels(false);
        setShowReasoning(false);
        setShowSlash(false);
        setShowAt(false);
        setShowProjectMenu(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [showModels, showReasoning, showSlash, showAt, showProjectMenu]);

  const activeModel = models.find((m) => m.id === sessionModelId);
  const supportsReasoning = !!activeModel?.reasoning_efforts && activeModel.reasoning_efforts.length > 0;
  const canSend = (input.trim().length > 0 || attachments.length > 0)
    && !uploading
    && !sending
    && (!isHome || activeProjectId != null);

  const changeModel = async (modelId: number) => {
    setSessionModelId(modelId);
    setShowModels(false);
    if (currentSessionId && !isHome) {
      useChatStore.setState((st) => ({ sessions: st.sessions.map((x) => (x.id === currentSessionId ? { ...x, model_id: modelId } : x)) }));
      try { await api.updateSession(currentSessionId, { model_id: modelId }); } catch { /* ignore */ }
    }
  };

  const changeReasoning = (effort: string | null) => {
    setReasoningEffort(effort);
    useChatStore.setState({ lastReasoningEffort: effort });
    if (currentSessionId) {
      if (effort) localStorage.setItem(REASONING_STORAGE_PREFIX + currentSessionId, effort);
      else localStorage.removeItem(REASONING_STORAGE_PREFIX + currentSessionId);
    }
    setShowReasoning(false);
  };

  const [showModeMenu, setShowModeMenu] = useState(false);
  const [stopping, setStopping] = useState(false);
  useEffect(() => { if (!isRunning) setStopping(false); }, [isRunning]);
  const setMode = (next: "default" | "plan" | "readonly") => {
    setComposerMode(next);
    setShowModeMenu(false);
    if (currentSessionId && !isHome) {
      localStorage.setItem(MODE_STORAGE_PREFIX + currentSessionId, next);
      api.updateSession(currentSessionId, { permission_mode: next }).catch(() => {});
    }
  };

  const handleCancel = () => {
    setStopping(true);
    forceStop();
    setTimeout(() => setStopping(false), 5000);
  };

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
      if (failed.length > 0) {
        useChatStore.setState({ error: `附件上传失败（${failed.length} 个）\n${failed.join("\n")}` });
      }
    } finally {
      setUploading(false);
    }
  }, []);

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

  const caret = taRef.current?.selectionStart ?? input.length;
  const slashMatch = input.slice(0, caret).match(/(?:^|\s)(\/[^\s]*)$/);
  const slashQuery = showSlash && slashMatch ? slashMatch[1].slice(1).toLowerCase() : "";
  const filteredSlash = slashQuery
    ? SLASH_COMMANDS.filter((c) => c.cmd.slice(1).startsWith(slashQuery))
    : SLASH_COMMANDS;
  const slashVisible = showSlash && filteredSlash.length > 0;
  const pickSlash = (cmd: string) => {
    const el = taRef.current;
    const pos = el?.selectionStart ?? input.length;
    const before = input.slice(0, pos);
    const match = before.match(/(?:^|\s)(\/[^\s]*)$/);
    const start = match ? pos - match[1].length : pos;
    const next = input.slice(0, start) + cmd + " " + input.slice(pos);
    setInput(next);
    setShowSlash(false);
    requestAnimationFrame(() => {
      el?.focus();
      const caretPos = start + cmd.length + 1;
      el?.setSelectionRange(caretPos, caretPos);
    });
  };

  // home 变体：项目选择
  const activeProject = activeProjects.find((p) => p.id === activeProjectId);
  const shortPathName = (path: string) => {
    const parts = path.replace(/\\/g, "/").replace(/\/$/, "").split("/");
    return parts[parts.length - 1] || path;
  };
  const handleAddDirectory = async () => {
    if (addingDir) return;
    setAddingDir(true);
    try {
      const path = await window.chatcoderAPI?.selectDirectory?.();
      if (typeof path === "string" && path) {
        const norm = (p: string) => p.replace(/\\/g, "/").replace(/\/+$/, "").toLowerCase();
        const existing = activeProjects.find((p) => norm(p.path) === norm(path));
        if (existing) {
          setSelectedProject(existing.id);
          useChatStore.setState({ currentProjectId: existing.id });
        } else {
          const proj = await createProject(path);
          if (proj) setSelectedProject(proj.id);
        }
      }
    } catch { /* ignore */ } finally {
      setAddingDir(false);
    }
  };

  const handleSend = async () => {
    if (!canSend) return;
    let content = input.trim();
    let mode: "readonly" | "plan" | null = null;
    if (content.startsWith("/plan")) { mode = "plan"; content = content.replace(/^\/plan\s*/, "").trim(); }
    else if (content.startsWith("/chat")) { mode = "readonly"; content = content.replace(/^\/chat\s*/, "").trim(); }
    else if (composerMode === "plan") mode = "plan";
    else if (composerMode === "readonly") mode = "readonly";
    if (!content && attachments.length === 0) return;
    const atts = attachments.length > 0 ? attachments.map((a) => ({ ...a })) : undefined;

    if (isHome) {
      const pid = activeProjectId;
      if (pid == null || sending) return;
      setSending(true);
      try {
        const sid = await createSession(pid, "新任务");
        if (!sid) return;
        const permissionMode = mode ?? (composerMode === "default" ? "accept_edits" : composerMode);
        try {
          await api.updateSession(sid, { model_id: sessionModelId ?? undefined, permission_mode: permissionMode });
          if (sessionModelId != null) {
            useChatStore.setState((st) => ({ sessions: st.sessions.map((x) => (x.id === sid ? { ...x, model_id: sessionModelId } : x)) }));
          }
        } catch { /* ignore */ }
        await sendTurn(content, atts, reasoningEffort ?? undefined, mode);
        setInput("");
        setAttachments([]);
        clearDraft();
        setShowSlash(false);
        setShowAt(false);
        onStarted?.();
      } catch { /* error handled by store */ } finally {
        setSending(false);
      }
      return;
    }

    sendTurn(content, atts, reasoningEffort ?? undefined, mode);
    setInput("");
    setAttachments([]);
    clearDraft();
    setShowSlash(false);
    setShowAt(false);
  };

  const modeLabel = composerMode === "default" ? (isHome ? "完全访问" : "完全访问") : composerMode === "plan" ? "计划模式" : "只读模式";

  return (
    <div className={`composer${dragOver ? " drag-over" : ""}${isHome ? " composer-home" : ""}`} onDragOver={(e) => { e.preventDefault(); setDragOver(true); }} onDragLeave={(e) => { e.preventDefault(); setDragOver(false); }} onDrop={handleDrop}>
      {/* home 变体：项目选择 chip */}
      {isHome && (
        <div className="es-card-project">
          <button className="es-project-trigger" onClick={() => setShowProjectMenu(!showProjectMenu)} title={activeProject?.path ?? "选择项目"}>
            <IconFolder size={13} />
            <span className="es-project-name">{activeProject ? shortPathName(activeProject.path) : "选择项目…"}</span>
            <IconChevronDown size={11} />
          </button>
          {showProjectMenu && (
            <div className="context-menu es-project-menu" onClick={() => setShowProjectMenu(false)}>
              <div className="context-menu-item" onClick={() => { void handleAddDirectory(); }}>
                <IconFolder size={12} /> <span>{addingDir ? "添加中…" : "选择本地目录…"}</span>
              </div>
              {projects.length > 0 && <div className="context-menu-divider" />}
              {projects.map((p) => (
                <div key={p.id} className={"context-menu-item" + (p.id === activeProjectId ? " active" : "")} onClick={() => { setSelectedProject(p.id); useChatStore.setState({ currentProjectId: p.id }); setShowProjectMenu(false); }} title={p.path}>
                  <span>{shortPathName(p.path)}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
      {/* chat 变体：排队消息胶囊 */}
      {!isHome && queuedInputs.length > 0 && (
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
      {!isHome && pendingPlan && (
        <div className="composer-plan-prompt">
          <div className="composer-plan-prompt-head"><IconBrain size={14} /> 计划已生成</div>
          <div className="composer-plan-prompt-task">{pendingPlan.task}</div>
          <div className="composer-plan-prompt-actions">
            <button type="button" className="btn-ghost" onClick={() => { setInput(pendingPlan.task); dismissPlan(); taRef.current?.focus(); }}>补充内容</button>
            <button type="button" className="btn-ghost" onClick={dismissPlan}>停止</button>
            <button type="button" className="btn-primary" onClick={() => void confirmPlan(pendingPlan.task)}>执行计划</button>
          </div>
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
                  onClick={() => setPreview(a)}
                />
              ) : (
                <IconPaperclip size={11} />
              )}
              <span className="attach-chip-name" onClick={() => setPreview(a)}>{a.filename}</span>
              <button className="remove" onClick={() => setAttachments((prev) => prev.filter((_, j) => j !== i))}><IconX size={10} /></button>
            </div>
          ))}
          {uploading && <span className="composer-attach-uploading">上传中…</span>}
        </div>
      )}
      <div className="composer-main">
        <textarea
          ref={taRef}
          className={isHome ? "composer-input empty-state-textarea" : "composer-input"}
          placeholder={isHome ? "向 ChatCoder 提问，使用 @ 添加上下文，使用 / 选择命令或能力" : "提出后续修改要求"}
          value={input}
          rows={isHome ? 1 : 2}
          onPaste={handlePaste}
          onChange={(e) => { const v = e.target.value; setInput(v); const pos = e.target.selectionStart; const isSlash = /(?:^|\s)\/[^\s]*$/.test(v.slice(0, pos)); setShowSlash(isSlash); if (isSlash) setSlashIndex(0); setShowAt(v.slice(0, pos).endsWith("@") || /@\S*$/.test(v.slice(0, pos))); }}
          onKeyDown={(e) => {
            if (slashVisible) {
              if (e.key === "ArrowDown") { e.preventDefault(); setSlashIndex((i) => (i + 1) % filteredSlash.length); return; }
              if (e.key === "ArrowUp") { e.preventDefault(); setSlashIndex((i) => (i - 1 + filteredSlash.length) % filteredSlash.length); return; }
              if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); pickSlash(filteredSlash[Math.min(slashIndex, filteredSlash.length - 1)].cmd); return; }
              if (e.key === "Escape") { e.preventDefault(); setShowSlash(false); return; }
            }
            if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); void handleSend(); }
            if (e.key === "Escape") { setShowSlash(false); setShowAt(false); }
          }}
        />
        <div className="composer-toolbar">
          <div className="composer-tools-left">
            <button className="composer-attach" title="添加附件" onClick={() => fileRef.current?.click()}><IconPlus size={16} /></button>
            <div className="composer-mode-wrap">
              <button
                className={`composer-mode-btn mode-${composerMode}`}
                onClick={() => { setShowModeMenu((v) => !v); setShowModels(false); setShowReasoning(false); }}
                title="权限模式"
              >
                <IconShield size={13} />
                {modeLabel}
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
            {!isHome && (
              <div className="composer-usage">
                <UsageRing pct={usagePct} usage={usage} compacting={isCompacting} />
              </div>
            )}
            {!isHome && isCompacting && (
              <span className="composer-compacting-badge" title="上下文接近窗口上限，正在压缩历史对话">
                正在压缩上下文…
              </span>
            )}
            <button className={`composer-ctx${listening ? " listening" : ""}`} onClick={toggleVoice} title={listening ? "停止录音" : "语音输入"}><IconMic size={15} /></button>
            {!isHome && isRunning ? (
              <button className="composer-send stop" disabled={stopping} onClick={handleCancel}
                      title={stopping ? "正在停止…" : "停止"}>
                {stopping ? <span className="thinking-block-breath" /> : <IconStop size={14} />}
              </button>
            ) : (
              <button className="composer-send" disabled={!canSend} onClick={() => void handleSend()} title="发送"><IconArrowUp size={16} /></button>
            )}
          </div>
        </div>
      </div>
      {dragOver && (<div className="composer-drag-overlay"><IconPaperclip size={24} /><span>松开以添加附件</span></div>)}
      <Modal open={preview !== null} onClose={() => setPreview(null)} title={preview?.filename ?? "图片预览"} width={900}>
        {preview?.type === "image" && <img className="composer-image-preview" src={resolveFileUrl(preview.url)} alt={preview.filename} />}
      </Modal>
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
      {!isHome && pendingApproval && (
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
function formatK(value: number): string {
  return (Math.max(0, value) / 1000).toFixed(value >= 10000 ? 0 : 1).replace(/\.0$/, "");
}

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
      {showDetail && usage && (
        <div className="composer-usage-detail" style={{ display: "block" }}>
          <div className="usage-detail-title">上下文占用 {pct}%</div>
          <div>输入：{formatK(usage.input)}k</div>
          <div>缓存：{formatK(usage.cached_input)}k</div>
          <div>输出：{formatK(usage.output)}k</div>
          <div>思考输出：{formatK(usage.reasoning_output)}k</div>
          <div>合计：{formatK(usage.total)}k / 窗口：{formatK(usage.context_window)}k</div>
          <div>模型：{usage.agent_name || "默认"} · 来源：{usage.source || "未知"}</div>
          {usage.breakdown && Object.entries(usage.breakdown).map(([key, value]) => <div key={key}>{key}：{formatK(value)}k</div>)}
        </div>
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
