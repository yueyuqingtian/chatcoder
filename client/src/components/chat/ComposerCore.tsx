/** ComposerCore（v19 插件化）：全局共用输入框组件。
 * variant="chat"：对话态（排队胶囊、审批覆盖层、停止按钮）。
 * variant="home"：空态首页（项目选择 chip，发送时 createSession → sendTurn）。
 * 两种变体共享 textarea/附件/模型/思考深度/权限模式/占用圆环等全部核心逻辑。
 */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { api, type AttachmentInfo, type ModelOut, type TreeNode, type SkillOut, resolveFileUrl } from "../../api/client";
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
const MODEL_STORAGE_PREFIX = "composer-model:";
const MAX_TEXTAREA_LINES = 7;
const MIN_TEXTAREA_HEIGHT = 36;

/** 剪贴板中的文件可能没有文件名（如直接复制图片像素），生成一个可读的默认名。 */
function namePastedFile(f: File): File {
  if (f.name && f.name.trim() && f.name !== "image.png") return f;
  const ext = (f.type.split("/")[1] || "png").replace(/[^a-z0-9]/gi, "") || "png";
  const ts = new Date().toISOString().replace(/[-:T]/g, "").slice(0, 14);
  return new File([f], `paste-${ts}.${ext}`, { type: f.type || "image/png" });
}

const SLASH_COMMANDS = [
  { cmd: "/chat", desc: "只读审阅模式", group: "command" as const },
  { cmd: "/clear", desc: "清空当前对话", group: "command" as const },
  { cmd: "/compact", desc: "压缩上下文", group: "command" as const },
  { cmd: "/init", desc: "初始化项目文件", group: "command" as const },
  { cmd: "/plan", desc: "制定执行计划", group: "command" as const },
];

/** 将 TreeNode 递归展平为路径列表（用于 @ 文件搜索补全） */
function flattenTree(nodes: TreeNode[], rootPath: string, prefix = ""): string[] {
  const result: string[] = [];
  const root = rootPath.replace(/\\/g, "/").replace(/\/+$/, "");
  for (const n of nodes) {
    const full = prefix ? `${prefix}/${n.name}` : n.name;
    // 后端 path 是绝对路径，引用时只发送项目根目录下的相对路径
    const absolute = n.path.replace(/\\/g, "/");
    const relative = absolute.startsWith(`${root}/`) ? absolute.slice(root.length + 1) : full;
    result.push(relative);
    if (n.children) result.push(...flattenTree(n.children, rootPath, full));
  }
  return result;
}

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
  const [atQuery, setAtQuery] = useState("");
  const [atIndex, setAtIndex] = useState(0);
  const [atFiles, setAtFiles] = useState<string[]>([]);
  const [atLoading, setAtLoading] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const [listening, setListening] = useState(false);
  const [composerMode, setComposerMode] = useState<"default" | "plan" | "readonly">("default");
  const [selectedProject, setSelectedProject] = useState<number | null>(currentProjectId);
  const [showProjectMenu, setShowProjectMenu] = useState(false);
  const [skills, setSkills] = useState<SkillOut[]>([]);

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
  // 审批/计划卡打开前输入框是否持有焦点（用于卡关闭后恢复焦点）
  const prevApprovalRef = useRef(pendingApproval);
  const prevPlanRef = useRef(pendingPlan);

  // 审批/计划遮罩消失后恢复输入框焦点：避免批准/拒绝后点击输入框需两次
  useEffect(() => {
    const hadApproval = prevApprovalRef.current != null;
    prevApprovalRef.current = pendingApproval;
    if (hadApproval && pendingApproval == null) {
      setTimeout(() => taRef.current?.focus(), 0);
    }
  }, [pendingApproval]);
  useEffect(() => {
    const hadPlan = prevPlanRef.current != null;
    prevPlanRef.current = pendingPlan;
    if (hadPlan && pendingPlan == null) {
      setTimeout(() => taRef.current?.focus(), 0);
    }
  }, [pendingPlan]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const ms = await api.listModels();
        if (cancelled) return;
        setModels(ms);
        if (!currentSessionId) {
          const saved = localStorage.getItem(MODEL_STORAGE_PREFIX + "home");
          const savedId = saved ? Number(saved) : null;
          setSessionModelId(savedId != null && ms.some((m) => m.id === savedId) ? savedId : ms[0]?.id ?? null);
          return;
        }
        let s = null;
        try { s = await api.getSession(currentSessionId); } catch { /* ignore */ }
        if (cancelled) return;
        if (s && s.model_id != null) setSessionModelId(s.model_id);
        else if (ms.length > 0) setSessionModelId(ms[0].id);
      } catch { /* ignore */ }
    })();
    return () => { cancelled = true; };
  }, [currentSessionId, isHome]);

  // 加载技能列表（用于 / 命令菜单展示）
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const list = await api.listSkills();
        if (!cancelled) setSkills(list.filter((s) => s.is_active));
      } catch { /* ignore */ }
    })();
    return () => { cancelled = true; };
  }, []);

  // @ 文件搜索：当项目 ID 变化时预加载文件树
  const atFilesCache = useRef<{ pid: number; files: string[] } | null>(null);
  const loadAtFiles = useCallback(async (pid: number) => {
    if (atFilesCache.current?.pid === pid) {
      setAtFiles(atFilesCache.current.files);
      return;
    }
    setAtLoading(true);
    try {
      const tree = await api.getProjectTree(pid, 8);
      const flat = flattenTree(tree.children, tree.path);
      atFilesCache.current = { pid, files: flat };
      setAtFiles(flat);
    } catch { setAtFiles([]); }
    finally { setAtLoading(false); }
  }, []);

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
    // 发送成功后清掉"未入会话"阶段（home/new）的草稿，避免下次新建任务残留上次内容
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
  // 回滚/外部注入：store.composerDraft 变化时回填输入框（同会话不切 draftKey）
  const composerDraft = useChatStore((s) => s.composerDraft);
  useEffect(() => {
    if (!composerDraft) return;
    setInput(composerDraft);
    useChatStore.setState({ composerDraft: "" });
  }, [composerDraft]);
  // v2.2: 回滚撤销的图片/附件一并回填输入框（一次性消费，按 file_id 去重后清空 store）
  const composerAttachments = useChatStore((s) => s.composerAttachments);
  useEffect(() => {
    if (!composerAttachments || composerAttachments.length === 0) return;
    setAttachments((prev) => {
      const merged = [...prev];
      for (const att of composerAttachments) {
        if (!merged.some((x) => x.file_id === att.file_id)) merged.push(att);
      }
      return merged;
    });
    useChatStore.setState({ composerAttachments: [] });
  }, [composerAttachments]);
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

  // 会话权限模式变化时同步输入框权限显示（如确认执行后端切为 accept_edits 后显示「完全访问」）
  const storeSessionPermissionMode = useChatStore((s) =>
    currentSessionId != null ? (s.sessions.find((x) => x.id === currentSessionId)?.permission_mode ?? null) : null
  );
  useEffect(() => {
    if (!currentSessionId || !storeSessionPermissionMode) return;
    const next = storeSessionPermissionMode === "readonly" ? "readonly"
      : storeSessionPermissionMode === "plan" ? "plan"
      : "default";
    setComposerMode((prev) => (prev === next ? prev : next));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentSessionId, storeSessionPermissionMode]);

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

  const resizeTextarea = useCallback((element?: HTMLTextAreaElement) => {
    const ta = element ?? taRef.current;
    if (!ta) return;
    const styles = window.getComputedStyle(ta);
    const lineHeight = Number.parseFloat(styles.lineHeight) || 20;
    const paddingY = (Number.parseFloat(styles.paddingTop) || 0) + (Number.parseFloat(styles.paddingBottom) || 0);
    const borderY = (Number.parseFloat(styles.borderTopWidth) || 0) + (Number.parseFloat(styles.borderBottomWidth) || 0);
    const maxHeight = Math.ceil(lineHeight * MAX_TEXTAREA_LINES + paddingY + borderY);
    // 先解除旧高度和滚动限制，再读取真实内容高度。onInput 直接传入当前
    // 原生 textarea，避免 React 状态更新/重渲染时测量到旧节点或旧 value。
    ta.style.height = "0px";
    ta.style.maxHeight = "none";
    ta.style.overflowY = "hidden";
    const contentHeight = ta.scrollHeight;
    const minHeight = isHome ? 38 : MIN_TEXTAREA_HEIGHT;
    const nextHeight = Math.min(Math.max(contentHeight, minHeight), maxHeight);
    ta.style.height = `${nextHeight}px`;
    ta.style.maxHeight = `${maxHeight}px`;
    ta.style.overflowY = contentHeight > maxHeight ? "auto" : "hidden";
  }, [isHome]);

  // 焦点兜底：点击输入框时同步 Electron WebContents 与 DOM 焦点；
  // 不 preventDefault，也不主动 blur，避免 Chromium 焦点事件风暴。
  const handleTextareaMouseDown = useCallback(() => {
    const ta = taRef.current;
    if (!ta || document.activeElement === ta) return;
    const active = document.activeElement;
    if (active && active.tagName === "IFRAME") {
      try { (active as HTMLIFrameElement).contentWindow?.blur(); } catch { /* 跨域 iframe：忽略 */ }
    }
    const restore = () => {
      try { void window.chatcoderAPI?.fixTextInput?.(); } catch { /* 浏览器模式忽略 */ }
      if (!ta.isConnected) return;
      try { ta.focus({ preventScroll: true }); } catch { ta.focus(); }
    };
    restore();
    requestAnimationFrame(restore);
    window.setTimeout(restore, 80);
  }, []);

  // 窗口失焦/聚焦恢复：失焦时记录输入框是否持有焦点，重新聚焦时恢复，避免"切回后首次点击只激活窗口"
  useEffect(() => {
    const onWindowBlur = () => {
      const el = document.activeElement;
      if (el === taRef.current) (window as Window & { __composerFocused?: boolean }).__composerFocused = true;
    };
    const onWindowFocus = () => {
      const flag = (window as Window & { __composerFocused?: boolean }).__composerFocused;
      if (!flag) return;
      // 无论输入框是否仍挂载，标志都只应消费一次（避免跨组件实例残留）
      (window as Window & { __composerFocused?: boolean }).__composerFocused = false;
      if (taRef.current) {
        requestAnimationFrame(() => taRef.current?.focus());
      }
    };
    window.addEventListener("blur", onWindowBlur);
    window.addEventListener("focus", onWindowFocus);
    return () => {
      window.removeEventListener("blur", onWindowBlur);
      window.removeEventListener("focus", onWindowFocus);
      // 卸载时清理残留标志，防止被下一个挂载的输入框实例错误消费
      (window as Window & { __composerFocused?: boolean }).__composerFocused = false;
    };
  }, []);

  const handleTextareaWheel = useCallback((e: React.WheelEvent<HTMLTextAreaElement>) => {
    const ta = e.currentTarget;
    if (ta.scrollHeight <= ta.clientHeight) return;
    e.preventDefault();
    e.stopPropagation();
    const unit = e.deltaMode === WheelEvent.DOM_DELTA_LINE ? 16 : e.deltaMode === WheelEvent.DOM_DELTA_PAGE ? ta.clientHeight : 1;
    ta.scrollTop = Math.max(0, Math.min(ta.scrollHeight - ta.clientHeight, ta.scrollTop + e.deltaY * unit));
  }, []);

  useLayoutEffect(() => {
    resizeTextarea();
  }, [input, attachments.length, resizeTextarea]);

  useEffect(() => {
    const ta = taRef.current;
    const composer = ta?.closest(".composer-main");
    if (!composer || typeof ResizeObserver === "undefined") return;
    // 观察输入卡片而不是 textarea 自身，避免 resizeTextarea 修改高度时触发观察循环；
    // rAF 节流：连续布局变化合并为一次高度校准，防止振荡。
    let raf = 0;
    const observer = new ResizeObserver(() => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => resizeTextarea());
    });
    observer.observe(composer);
    return () => { cancelAnimationFrame(raf); observer.disconnect(); };
  }, [resizeTextarea]);

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
    if (isHome) {
      localStorage.setItem(MODEL_STORAGE_PREFIX + "home", String(modelId));
    } else if (currentSessionId) {
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
      api.updateSession(currentSessionId, { permission_mode: next }).catch(() => { /* ignore */ });
    }
  };
  const handleCancel = () => {
    setStopping(true);
    forceStop();
  };
  const addFiles = useCallback(async (files: FileList | File[]) => {
    const list = Array.from(files);
    if (list.length === 0) return;
    setUploading(true);
    const ok: AttachmentInfo[] = [];
    const failed: string[] = [];
    try {
      for (const f of list) {
        try {
          const res = await api.uploadFile(f);
          ok.push({ file_id: res.file_id, filename: res.filename, path: res.path, url: res.url, size: res.size, mime_type: res.mime_type, type: res.type });
        } catch (e) {
          console.warn("[composer] 上传失败", f.name, e);
          failed.push(`${f.name}: ${e instanceof Error ? e.message : String(e)}`);
        }
      }
      if (ok.length > 0) setAttachments((prev) => [...prev, ...ok]);
      if (failed.length > 0) useChatStore.setState({ error: `附件上传失败（${failed.length} 个）\n${failed.join("\n")}` });
    } finally { setUploading(false); }
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
    if (files.length > 0) { e.preventDefault(); void addFiles(files); }
  }, [addFiles]);
  const handleDrop = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setDragOver(false);
    if (e.dataTransfer.files.length > 0) void addFiles(e.dataTransfer.files);
  };


  const toggleVoice = () => {
    if (listening) { recogRef.current?.stop(); return; }
    const SR = (window as unknown as { webkitSpeechRecognition?: new () => unknown }).webkitSpeechRecognition;
    if (!SR) { useChatStore.setState({ error: "当前浏览器不支持语音识别" }); return; }
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
  // 合并内置命令 + 后端技能到 / 菜单
  const allSlashItems = useMemo(() => {
    const skillItems = skills.map((s) => ({
      cmd: `/${s.name}`,
      desc: s.display_name || s.description || s.name,
      group: "skill" as const,
    }));
    return [...SLASH_COMMANDS, ...skillItems];
  }, [skills]);
  const filteredSlash = slashQuery
    ? allSlashItems.filter((c) => c.cmd.slice(1).toLowerCase().startsWith(slashQuery) || (c.desc && c.desc.toLowerCase().includes(slashQuery)))
    : allSlashItems;
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

  // @ 文件搜索：过滤与选中
  const filteredAtFiles = useMemo(() => {
    if (!atQuery) return atFiles.slice(0, 20);
    const q = atQuery.toLowerCase();
    return atFiles.filter((f) => f.toLowerCase().includes(q)).slice(0, 20);
  }, [atFiles, atQuery]);

  const pickAtFile = (filePath: string) => {
    const el = taRef.current;
    const pos = el?.selectionStart ?? input.length;
    const before = input.slice(0, pos);
    const match = before.match(/@([^\s]*)$/);
    const start = match ? pos - match[0].length : pos;
    const next = input.slice(0, start) + `@${filePath} ` + input.slice(pos);
    setInput(next);
    setShowAt(false);
    setAtQuery("");
    requestAnimationFrame(() => {
      el?.focus();
      const caretPos = start + filePath.length + 2; // @filePath + space
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
          const created = await createProject(path);
          if (created) setSelectedProject(created.id);
        }
      }
    } catch { /* ignore */ }
    finally { setAddingDir(false); }
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
    const attachmentPayload = attachments.length > 0 ? attachments.map((a) => ({ ...a })) : undefined;

    if (isHome) {
      const pid = activeProjectId;
      if (pid == null || sending) return;
      setSending(true);
      try {
        const sessionId = await createSession(pid);
        if (!sessionId) return;
        const perm = mode ?? (composerMode === "default" ? "accept_edits" : composerMode);
        try {
          await api.updateSession(sessionId, { model_id: sessionModelId ?? undefined, permission_mode: perm });
          if (sessionModelId != null) useChatStore.setState((st) => ({ sessions: st.sessions.map((x) => (x.id === sessionId ? { ...x, model_id: sessionModelId } : x)) }));
        } catch { /* ignore */ }
        await sendTurn(content, attachmentPayload, reasoningEffort ?? undefined, mode);
        setInput("");
        setAttachments([]);
        clearDraft();
        setShowSlash(false);
        setShowAt(false);
        onStarted?.();
      } catch { /* ignore */ }
      finally { setSending(false); }
      return;
    }
    await sendTurn(content, attachmentPayload, reasoningEffort ?? undefined, mode);
    setInput("");
    setAttachments([]);
    clearDraft();
    setShowSlash(false);
    setShowAt(false);
  };

  const modeLabel = composerMode === "default" ? (isHome ? "完全访问" : "完全访问") : composerMode === "plan" ? "计划模式" : "只读模式";

  return (
    <div className={`composer${dragOver ? " drag-over" : ""}${isHome ? " composer-home" : ""}`} onDragOver={(e) => { e.preventDefault(); setDragOver(true); }} onDragLeave={(e) => { e.preventDefault(); setDragOver(false); }} onDrop={handleDrop}>
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
              {activeProjects.length > 0 && <div className="context-menu-divider" />}
              {activeProjects.map((p) => (
                <div key={p.id} className={`context-menu-item${p.id === activeProjectId ? " active" : ""}`} onClick={() => { setSelectedProject(p.id); useChatStore.setState({ currentProjectId: p.id }); setShowProjectMenu(false); }} title={p.path}>
                  <span>{shortPathName(p.path)}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
      {!isHome && queuedInputs.length > 0 && (
        <div className="composer-queue">
          <div className="composer-queue-title">排队中（当前任务完成后自动发送）</div>
          {queuedInputs.map((q) => (
            <div key={q.id} className="composer-queue-item" title={q.content}>
              <span className="composer-queue-text">{q.content || (q.attachments?.length ? `附件 × ${q.attachments.length}` : "")}</span>
              {q.mode === "plan" && <span className="composer-queue-tag">规划</span>}
              {q.mode === "readonly" && <span className="composer-queue-tag">只读</span>}
              <button className="composer-queue-remove" onClick={() => updateQueuedInput(q.id, null)} title="移出队列" type="button"><IconX size={11} /></button>
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
      <div className="composer-main">
        {attachments.length > 0 && (
          <div className="composer-attachments">
            {attachments.map((att, idx) => (
              <div key={att.file_id ?? idx} className={`composer-attach-chip${att.type === "image" ? " image" : ""}`} title={att.filename}>
                {att.type === "image"
                  ? <img src={resolveFileUrl(att.url)} alt={att.filename} className="attach-chip-thumb" onClick={() => setPreview(att)} />
                  : <IconPaperclip size={11} />}
                <span className="attach-chip-name" onClick={() => setPreview(att)}>{att.filename}</span>
                <button className="remove" onClick={() => setAttachments((prev) => prev.filter((_, i) => i !== idx))}><IconX size={10} /></button>
              </div>
            ))}
            {uploading && <span className="composer-attach-uploading">上传中…</span>}
          </div>
        )}
        <textarea
          ref={taRef}
          className="composer-input"
          placeholder={isHome ? "向 ChatCoder 提问，使用 @ 添加上下文，使用 / 选择命令或能力" : "提出后续修改要求"}
          value={input}
          rows={1}
          onPaste={handlePaste}
          onWheel={handleTextareaWheel}
          onMouseDown={handleTextareaMouseDown}
          onInput={(e) => resizeTextarea(e.currentTarget)}
          onChange={(e) => {
            const v = e.target.value; setInput(v);
            const pos = e.target.selectionStart;
            const isSlash = /(?:^|\s)\/[^\s]*$/.test(v.slice(0, pos));
            setShowSlash(isSlash); if (isSlash) setSlashIndex(0);
            // @ 文件搜索：提示 @ 后面的查询词
            const atMatch = v.slice(0, pos).match(/@([^\s]*)$/);
            if (atMatch) {
              setShowAt(true);
              setAtQuery(atMatch[1].toLowerCase());
              setAtIndex(0);
              // 触发文件树加载
              const pid = isHome ? activeProjectId : currentProjectId;
              if (pid != null) void loadAtFiles(pid);
            } else {
              setShowAt(false);
              setAtQuery("");
            }
          }}
          onKeyDown={(e) => {
            if (slashVisible) {
              if (e.key === "ArrowDown") { e.preventDefault(); setSlashIndex((i) => (i + 1) % filteredSlash.length); return; }
              if (e.key === "ArrowUp") { e.preventDefault(); setSlashIndex((i) => (i - 1 + filteredSlash.length) % filteredSlash.length); return; }
              if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); pickSlash(filteredSlash[Math.min(slashIndex, filteredSlash.length - 1)].cmd); return; }
              if (e.key === "Escape") { e.preventDefault(); setShowSlash(false); return; }
            }
            if (showAt && filteredAtFiles.length > 0) {
              if (e.key === "ArrowDown") { e.preventDefault(); setAtIndex((i) => (i + 1) % filteredAtFiles.length); return; }
              if (e.key === "ArrowUp") { e.preventDefault(); setAtIndex((i) => (i - 1 + filteredAtFiles.length) % filteredAtFiles.length); return; }
              if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); pickAtFile(filteredAtFiles[Math.min(atIndex, filteredAtFiles.length - 1)]); return; }
              if (e.key === "Escape") { e.preventDefault(); setShowAt(false); return; }
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
            <input ref={fileRef} type="file" multiple style={{ display: "none" }} onChange={(e) => { if (e.target.files) void addFiles(e.target.files); e.target.value = ""; }} />
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
          {/* 内置命令分组 */}
          {filteredSlash.some((c) => c.group === "command") && (
            <div className="composer-menu-title">命令</div>
          )}
          {filteredSlash.filter((c) => c.group === "command").map((item, idx) => (
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
          {/* 技能分组 */}
          {filteredSlash.some((c) => c.group === "skill") && (
            <div className="composer-menu-title" style={{ marginTop: 4 }}>技能</div>
          )}
          {filteredSlash.filter((c) => c.group === "skill").map((item) => {
            const globalIdx = filteredSlash.indexOf(item);
            return (
              <button
                key={item.cmd}
                className={globalIdx === slashIndex ? "active" : ""}
                onMouseEnter={() => setSlashIndex(globalIdx)}
                onClick={() => pickSlash(item.cmd)}
              >
                <span>{item.cmd}</span>
                {item.desc && <span className="composer-slash-desc">{item.desc}</span>}
              </button>
            );
          })}
        </div>
      )}
      {showAt && (
        <div className="composer-menu composer-at">
          <div className="composer-menu-title">提及文件</div>
          {atLoading && <div className="composer-menu-empty">加载文件树…</div>}
          {!atLoading && filteredAtFiles.length === 0 && (
            <div className="composer-menu-empty">{atQuery ? "未找到匹配文件" : "继续输入文件名以补全…"}</div>
          )}
          {!atLoading && filteredAtFiles.map((f, idx) => (
            <button
              key={f}
              className={idx === atIndex ? "active" : ""}
              onMouseEnter={() => setAtIndex(idx)}
              onClick={() => pickAtFile(f)}
            >
              <span>{f}</span>
            </button>
          ))}
        </div>
      )}
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
              <button className="btn-ghost" onClick={() => respondApproval(pendingApproval.approvalId, false)}>取消</button>
              <button className="btn-ghost" onClick={() => respondApproval(pendingApproval.approvalId, true)}>仅本次执行</button>
              <button className="btn-ghost" onClick={() => respondApproval(pendingApproval.approvalId, true, true, undefined, "session")} title="自动生成会话级执行策略规则，本会话内同类操作不再询问">当前会话允许</button>
              <button className="btn-ghost" onClick={() => respondApproval(pendingApproval.approvalId, true, true, undefined, "global")} title="自动生成全局执行策略规则，所有会话同类操作不再询问">始终允许</button>
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
          <button className="btn-primary" disabled={!allAnswered} onClick={() => onSubmit(answers)}>提交</button>
        </div>
      </div>
    </div>
  );
}
