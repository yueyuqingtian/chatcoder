/** ComposerCore：输入框共享内核。
 * - 支持三种形态：主页面全功能（有模型/能力/会话/附件）/ 首页居中 / 工具栏简化版
 * - 状态全量接入 useChatStore
 * - 附件上传经 /api/projects/:id/files/upload 或 /api/chat/attachments/upload
 * - 结构化提问时直接替换输入框为全功能向导卡片（对齐参考图 paste-20260829121505.png）
 */
import {
  useRef,
  useState,
  useCallback,
  useEffect,
  useMemo,
  type DragEvent,
  type WheelEvent,
  type MouseEvent as ReactMouseEvent,
} from "react";
import {
  IconArrowUp,
  IconStop,
  IconPaperclip,
  IconPlus,
  IconMic,
  IconChevronDown,
  IconChevronLeft,
  IconChevronRight,
  IconX,
  IconFolder,
  IconShield,
  IconBrain,
  IconTarget,
  IconCheck,
  IconImage,
  IconCode,
  IconTerminal,
  IconClipboard,
} from "../icons";
import { Modal } from "../Modal";
import { ModelPicker } from "./ModelPicker";
import { useChatStore, type UsageDetail } from "../../store/chat";
import { useDraftsStore } from "../../store/drafts";
import { api, resolveFileUrl, type AttachmentInfo, type TreeNode } from "../../api/client";

export interface ComposerCoreProps {
  variant?: "default" | "chat" | "home" | "compact";
  projectId?: number | null;
  sessionId?: number | null;
  /** 首页变体：会话创建并发出首条消息后回调（宿主用于离开空态页） */
  onStarted?: () => void;
}

export function ComposerCore({ variant = "default", onStarted }: ComposerCoreProps) {
  const isHome = variant === "home";
  const taRef = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const isRunning = useChatStore((s) => s.isRunning);
  const currentSessionId = useChatStore((s) => s.currentSessionId);
  const currentProjectId = useChatStore((s) => s.currentProjectId);
  const projects = useChatStore((s) => s.projects);
  const models = useChatStore((s) => s.models);
  const lastReasoningEffort = useChatStore((s) => s.lastReasoningEffort);
  const lastModelId = useChatStore((s) => s.lastModelId);
  const queuedInputs = useChatStore((s) => s.queuedInputs);
  const isCompacting = useChatStore((s) => s.isCompacting);
  const usage = useChatStore((s) => s.usage);
  const pendingApproval = useChatStore((s) => s.pendingApproval);
  const pendingPlan = useChatStore((s) => s.pendingPlan);
  const confirmPlanTurn = useChatStore((s) => s.confirmPlanTurn);
  const dismissPlan = useChatStore((s) => s.dismissPlan);
  const sessions = useChatStore((s) => s.sessions);

  const currentSession = sessions.find((s) => s.id === currentSessionId);

  /** plan-546: 草稿 key——home 变体固定 "home"，会话变体取 sessionId（"new"=无会话兜底，不持久化） */
  const draftKey = isHome ? "home" : currentSessionId != null ? String(currentSessionId) : "new";
  /** 挂载时读取一次草稿：重挂载即恢复文字/附件/深度；home 另含模型/模式/工作目录 */
  const initialDraft = useRef(
    draftKey !== "new" ? useDraftsStore.getState().getDraft(draftKey) : null
  ).current;

  // plan-676: 首页本地目标（无会话 id 不能调 goal API，先入 home 草稿，随创建一次落准）
  const [homeGoalText, setHomeGoalText] = useState<string | null>(() => initialDraft?.goalText ?? null);

  // plan-671: 目标模式状态（胶囊与菜单入口的数据源）
  const goalText = isHome ? homeGoalText : (currentSession?.goal_text ?? null);
  const goalStatus = isHome ? (homeGoalText ? "active" : "none") : (currentSession?.goal_status ?? "none");
  const goalTurnsUsed = currentSession?.goal_turns_used ?? 0;
  const [showGoalModal, setShowGoalModal] = useState(false);
  const [goalInput, setGoalInput] = useState("");
  /** goal.completed 后的 3 秒过渡展示（label「已完成」→ 淡出） */
  const [goalCompletedVisible, setGoalCompletedVisible] = useState(false);
  const prevGoalStatusRef = useRef(goalStatus);
  useEffect(() => {
    if (prevGoalStatusRef.current !== "completed" && goalStatus === "completed") {
      setGoalCompletedVisible(true);
      const t = setTimeout(() => setGoalCompletedVisible(false), 3000);
      prevGoalStatusRef.current = goalStatus;
      return () => clearTimeout(t);
    }
    prevGoalStatusRef.current = goalStatus;
  }, [goalStatus]);
  const goalPillVisible = (isHome ? homeGoalText != null : currentSessionId != null)
    && (goalStatus === "active" || (goalStatus === "completed" && goalCompletedVisible));
  /** 续跑轮次上限（胶囊可见时从后端拉取，避免硬编码配置） */
  const [goalMaxTurns, setGoalMaxTurns] = useState(10);
  useEffect(() => {
    if (goalPillVisible && currentSessionId != null) {
      api.getSessionGoal(currentSessionId)
        .then((out) => { if (out.max_turns > 0) setGoalMaxTurns(out.max_turns); })
        .catch(() => { /* 拉取失败沿用当前值 */ });
    }
  }, [goalPillVisible, currentSessionId]);

  const applyGoalOut = (out: { text?: string | null; status: string; turns_used?: number }) => {
    if (currentSessionId == null) return;
    useChatStore.setState((s) => ({
      sessions: s.sessions.map((x) => (x.id === currentSessionId
        ? {
            ...x,
            goal_text: out.text ?? null,
            goal_status: (out.status || "none") as typeof x.goal_status,
            goal_turns_used: out.turns_used ?? 0,
          }
        : x)),
    }));
  };

  const submitGoal = () => {
    const text = goalInput.trim();
    if (!text) return;
    // plan-676: 首页无会话——本地暂存入 home 草稿，随会话创建一次落准
    if (isHome || currentSessionId == null) {
      setHomeGoalText(text);
      useDraftsStore.getState().patchDraft("home", { goalText: text });
      setShowGoalModal(false);
      return;
    }
    void api.setSessionGoal(currentSessionId, text)
      .then((out) => applyGoalOut(out))
      .catch(() => { /* 设定失败保持现状，WS 不触发 */ });
    setShowGoalModal(false);
  };

  const cancelGoal = () => {
    // plan-676: 首页移除 = 清本地暂存与草稿
    if (isHome || currentSessionId == null) {
      setHomeGoalText(null);
      useDraftsStore.getState().patchDraft("home", { goalText: null });
      return;
    }
    void api.cancelSessionGoal(currentSessionId)
      .then((out) => applyGoalOut(out))
      .catch(() => { /* 取消失败保持现状 */ });
  };

  const completeGoal = () => {
    if (currentSessionId == null) return;
    void api.completeSessionGoal(currentSessionId)
      .then((out) => applyGoalOut(out))
      .catch(() => { /* 完成失败保持现状 */ });
  };

  // v40: 输入与附件为组件本地状态；回滚回填经 composerBackfill 按 key 一次性消费
  // plan-546: 初值从草稿恢复（组件按会话 key 重挂载，草稿 store 保证跨导航/重启不丢）
  const [input, setInput] = useState(() => initialDraft?.text ?? "");
  const [attachments, setAttachments] = useState<AttachmentInfo[]>(() => initialDraft?.attachments ?? []);
  /** 本会话/首页独立的思考深度（null=跟随全局最近值） */
  const [effort, setEffort] = useState<string | null>(() => initialDraft?.reasoningEffort ?? null);
  /** 展示与发送用的档位：本 key 草稿 → 全局最近 → 默认 */
  const activeEffort = effort ?? lastReasoningEffort ?? null;

  /** 首页变体在会话创建前暂存的模型选择（发送时写入新会话） */
  const [homeModelId, setHomeModelId] = useState<number | null>(() => initialDraft?.modelId ?? null);

  const [composerMode, setComposerMode] = useState<"default" | "plan" | "readonly" | "accept_edits">(
    isHome
      ? initialDraft?.mode ?? "default"
      : (currentSession?.permission_mode as "default" | "plan" | "readonly" | "accept_edits") || "default"
  );

  useEffect(() => {
    if (currentSession?.permission_mode) {
      setComposerMode(currentSession.permission_mode as "default" | "plan" | "readonly" | "accept_edits");
    }
  }, [currentSession?.permission_mode, currentSessionId]);

  useEffect(() => {
    const onComposerModeEvt = (e: Event) => {
      const mode = (e as CustomEvent<{ mode: "default" | "plan" | "readonly" }>).detail?.mode;
      if (mode) setComposerMode(mode);
    };
    window.addEventListener("chatcoder:composer-mode", onComposerModeEvt);
    return () => window.removeEventListener("chatcoder:composer-mode", onComposerModeEvt);
  }, []);

  const sendTurn = useChatStore((s) => s.sendTurn);
  const cancelTurn = useChatStore((s) => s.cancelTurn);
  const updateQueuedInput = useChatStore((s) => s.updateQueuedInput);
  const flushQueuedInput = useChatStore((s) => s.flushQueuedInput);
  const respondApproval = useChatStore((s) => s.respondApproval);

  const composerBrowserRefs = useChatStore((s) => s.composerBrowserRefs);
  const removeComposerBrowserRef = useChatStore((s) => s.removeComposerBrowserRef);
  const clearComposerBrowserRefs = useChatStore((s) => s.clearComposerBrowserRefs);
  const [browserRefPreview, setBrowserRefPreview] = useState<any | null>(null);

  const [uploading, setUploading] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const [preview, setPreview] = useState<AttachmentInfo | null>(null);
  const [showModels, setShowModels] = useState(false);
  const [showReasoning, setShowReasoning] = useState(false);
  const [showSlash, setShowSlash] = useState(false);
  const [slashIndex, setSlashIndex] = useState(0);
  const [showProjectMenu, setShowProjectMenu] = useState(false);
  const [showModeMenu, setShowModeMenu] = useState(false);
  const [addingDir, setAddingDir] = useState(false);
  const [listening, setListening] = useState(false);
  const [showAt, setShowAt] = useState(false);
  const [atIndex, setAtIndex] = useState(0);
  const [atQuery, setAtQuery] = useState("");
  const [atFiles, setAtFiles] = useState<string[]>([]);
  const [atLoading, setAtLoading] = useState(false);
  const recognitionRef = useRef<any>(null);

  const prevApprovalRef = useRef(pendingApproval);
  useEffect(() => {
    const hadApproval =
      prevApprovalRef.current != null && prevApprovalRef.current.detail?.kind === "question";
    prevApprovalRef.current = pendingApproval;
    if (hadApproval && pendingApproval == null) {
      setTimeout(() => taRef.current?.focus(), 80);
    }
  }, [pendingApproval]);

  /** v40 回填消费：composerBackfill 按 key（home/new/sessionId）匹配，仅消费一次 */
  const composerBackfill = useChatStore((s) => s.composerBackfill);
  useEffect(() => {
    const bf = useChatStore.getState().composerBackfill;
    if (!bf || bf.key !== draftKey) return;
    useChatStore.setState({ composerBackfill: null });
    setInput(bf.text);
    if (bf.attachments.length > 0) setAttachments((prev) => [...prev, ...bf.attachments]);
  }, [draftKey, composerBackfill]);

  /** plan-546: 草稿防抖同步——输入/附件/配置变化 300ms 后写入草稿 store；
   * 发送成功后置 skipDraftSyncRef 跳过一次，避免空状态写回导致草稿复活 */
  const skipDraftSyncRef = useRef(false);
  useEffect(() => {
    if (draftKey === "new") return;
    if (skipDraftSyncRef.current) {
      skipDraftSyncRef.current = false;
      return;
    }
    const t = setTimeout(() => {
      useDraftsStore.getState().patchDraft(draftKey, {
        text: input,
        attachments,
        reasoningEffort: effort,
        ...(isHome ? { modelId: homeModelId, mode: composerMode, projectId: currentProjectId } : {}),
      });
    }, 300);
    return () => clearTimeout(t);
  }, [draftKey, input, attachments, effort, isHome, homeModelId, composerMode, currentProjectId]);

  /** plan-546: 首页挂载时若草稿记录了工作目录，恢复全局 currentProjectId（侧栏高亮同步） */
  useEffect(() => {
    if (isHome && initialDraft?.projectId != null) {
      const st = useChatStore.getState();
      if (st.currentProjectId !== initialDraft.projectId) {
        useChatStore.setState({ currentProjectId: initialDraft.projectId });
      }
    }
    // 仅挂载时执行一次
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 模型选择：会话内跟随 session.model_id；首页优先草稿模型，其次最近使用模型，最后首个可用模型
  const fallbackModelId = (models.find((m) => m.is_active) ?? models[0])?.id ?? null;
  const sessionModelId = isHome
    ? homeModelId ?? lastModelId ?? fallbackModelId
    : currentSession?.model_id ?? homeModelId ?? lastModelId ?? fallbackModelId;
  const activeModel = models.find((m) => m.id === sessionModelId) ?? null;
  const supportsReasoning = (activeModel?.reasoning_efforts?.length ?? 0) > 0;

  /** 上下文占用百分比（usage.total / context_window） */
  const usagePct = useMemo(() => {
    if (!usage || !usage.context_window || usage.context_window <= 0) return 0;
    return Math.min(100, Math.round((usage.total / usage.context_window) * 100));
  }, [usage]);

  const activeProjects = useMemo(() => projects.filter((p) => !p.archived), [projects]);
  const activeProjectId = currentProjectId;
  const activeProject = activeProjects.find((p) => p.id === activeProjectId);

  const slashCommands = useMemo(
    () => [
      { cmd: "/clear", desc: "清空当前会话历史" },
      { cmd: "/plan", desc: "切换为规划模式" },
      { cmd: "/full", desc: "切换为完全访问模式" },
      { cmd: "/read", desc: "切换为只读模式" },
    ],
    []
  );

  const filteredSlash = useMemo(() => {
    const match = input.match(/(?:^|\s)\/([^\s]*)$/);
    if (!match) return [];
    const query = match[1].toLowerCase();
    return slashCommands.filter((s) => s.cmd.slice(1).toLowerCase().includes(query));
  }, [input, slashCommands]);

  const slashVisible = showSlash && filteredSlash.length > 0;

  const loadAtFiles = useCallback(async (pid: number) => {
    setAtLoading(true);
    try {
      const tree = await api.getProjectTree(pid, 6);
      const paths: string[] = [];
      const walk = (nodes: TreeNode[]) => {
        for (const n of nodes) {
          if (paths.length >= 500) return;
          if (n.type === "file") paths.push(n.path);
          if (n.children) walk(n.children);
        }
      };
      walk(tree.children || []);
      setAtFiles(paths);
    } catch {
      setAtFiles([]);
    } finally {
      setAtLoading(false);
    }
  }, []);

  const filteredAtFiles = useMemo(() => {
    if (!atQuery) return atFiles.slice(0, 15);
    return atFiles.filter((p) => p.toLowerCase().includes(atQuery)).slice(0, 15);
  }, [atFiles, atQuery]);

  const pickAtFile = (filePath: string) => {
    const pos = taRef.current?.selectionStart ?? input.length;
    const prefix = input.slice(0, pos).replace(/@([^\s]*)$/, `@${filePath} `);
    const next = prefix + input.slice(pos);
    setInput(next);
    setShowAt(false);
    setAtQuery("");
    setTimeout(() => {
      if (taRef.current) {
        taRef.current.focus();
        taRef.current.selectionStart = taRef.current.selectionEnd = prefix.length;
        resizeTextarea(taRef.current);
      }
    }, 0);
  };

  const resizeTextarea = (el: HTMLTextAreaElement) => {
    el.style.height = "auto";
    const clamped = Math.max(36, Math.min(el.scrollHeight, 400));
    el.style.height = `${clamped}px`;
  };

  useEffect(() => {
    if (taRef.current) resizeTextarea(taRef.current);
  }, [input]);

  const handleTextareaWheel = (e: WheelEvent<HTMLTextAreaElement>) => {
    const el = e.currentTarget;
    const { scrollTop, scrollHeight, clientHeight } = el;
    const atTop = scrollTop === 0;
    const atBottom = Math.ceil(scrollTop + clientHeight) >= scrollHeight;
    if ((e.deltaY < 0 && atTop) || (e.deltaY > 0 && atBottom)) {
      return;
    }
    e.stopPropagation();
  };

  const handleTextareaMouseDown = (e: ReactMouseEvent<HTMLTextAreaElement>) => {
    const el = e.currentTarget;
    const isOverflowing = el.scrollHeight > el.clientHeight;
    if (!isOverflowing) return;
    const rect = el.getBoundingClientRect();
    const isNearScrollbar = e.clientX >= rect.right - 14;
    if (isNearScrollbar) {
      e.stopPropagation();
    }
  };

  const addFiles = useCallback(
    async (fileList: FileList | File[]) => {
      const arr = Array.from(fileList);
      if (arr.length === 0) return;
      setUploading(true);
      try {
        const uploaded: AttachmentInfo[] = [];
        for (const f of arr) {
          try {
            const res = await api.uploadFile(f);
            uploaded.push(res);
          } catch {
            /* ignore upload err */
          }
        }
        if (uploaded.length > 0) {
          setAttachments([...attachments, ...uploaded]);
        }
      } finally {
        setUploading(false);
      }
    },
    [attachments, setAttachments]
  );

  const handlePaste = useCallback(
    (e: React.ClipboardEvent) => {
      const items = e.clipboardData?.items;
      if (!items) return;
      const files: File[] = [];
      for (let i = 0; i < items.length; i++) {
        if (items[i].kind === "file") {
          const file = items[i].getAsFile();
          if (file) files.push(file);
        }
      }
      if (files.length > 0) {
        e.preventDefault();
        void addFiles(files);
      }
    },
    [addFiles]
  );

  const handleDrop = useCallback(
    (e: DragEvent<HTMLDivElement>) => {
      e.preventDefault();
      setDragOver(false);
      if (e.dataTransfer.files?.length) {
        void addFiles(e.dataTransfer.files);
      }
    },
    [addFiles]
  );

  const pickSlash = (cmd: string) => {
    if (cmd === "/clear") {
      // 清空当前会话视图（与命令中心「新任务」一致的本地重置语义）
      useChatStore.setState({
        messages: [], turns: [], tasks: [], runningTurnId: null, isRunning: false,
        interruptedTurnId: null, streamingBuffers: {}, thinkingBuffers: {}, usage: null,
        pendingApproval: null, pendingPlan: null, reviewedFiles: {}, injectMarks: [],
      });
      setInput("");
      setShowSlash(false);
      return;
    }
    if (cmd === "/plan") {
      setMode("plan");
      setInput("");
      setShowSlash(false);
      return;
    }
    if (cmd === "/full") {
      setMode("default");
      setInput("");
      setShowSlash(false);
      return;
    }
    if (cmd === "/read") {
      setMode("readonly");
      setInput("");
      setShowSlash(false);
      return;
    }
    setInput(`${cmd} `);
    setShowSlash(false);
    taRef.current?.focus();
  };

  const toggleVoice = () => {
    const SpeechRecognition =
      (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition;
    if (!SpeechRecognition) {
      alert("当前环境不支持语音识别 API");
      return;
    }
    if (listening) {
      recognitionRef.current?.stop();
      setListening(false);
      return;
    }
    const rec = new SpeechRecognition();
    rec.lang = "zh-CN";
    rec.continuous = true;
    rec.interimResults = true;
    rec.onresult = (e: any) => {
      let text = "";
      for (let i = 0; i < e.results.length; i++) {
        text += e.results[i][0].transcript;
      }
      setInput(text);
    };
    rec.onerror = () => setListening(false);
    rec.onend = () => setListening(false);
    rec.start();
    recognitionRef.current = rec;
    setListening(true);
  };

  const handleAddDirectory = async () => {
    setAddingDir(true);
    try {
      const dir = await window.chatcoderAPI?.selectDirectory?.();
      if (dir) {
        const created = await useChatStore.getState().createProject(dir);
        if (created?.id) {
          useChatStore.setState({ currentProjectId: created.id });
        }
      }
    } finally {
      setAddingDir(false);
      setShowProjectMenu(false);
    }
  };

  const changeModel = (modelId: number) => {
    if (!isHome && currentSessionId != null) {
      // plan-166-767: 切换模型等待后端落库成功后再更新本地 state；失败回滚提示。
      // 配合发送请求携带 model_id（权威值），消除「前端已切、后端未变」竞态。
      setShowModels(false);
      void api.updateSession(currentSessionId, { model_id: modelId })
        .then(() => {
          useChatStore.setState((s) => ({
            sessions: s.sessions.map((x) => (x.id === currentSessionId ? { ...x, model_id: modelId } : x)),
            lastModelId: modelId,
          }));
        })
        .catch(() => {
          useChatStore.setState({ error: "切换模型失败，请重试" });
        });
    } else {
      setHomeModelId(modelId);
      useChatStore.setState({ lastModelId: modelId });
      setShowModels(false);
    }
  };

  const changeReasoning = (e: string | null) => {
    // plan-546: 深度双写——本 key 草稿隔离 + 全局最近值（新会话/新首页默认承接）
    setEffort(e);
    useChatStore.setState({ lastReasoningEffort: e });
    setShowReasoning(false);
  };

  const setMode = (mode: "default" | "plan" | "readonly") => {
    setComposerMode(mode);
    setShowModeMenu(false);
    if (currentSessionId != null) {
      void api.updateSession(currentSessionId, { permission_mode: mode });
      useChatStore.setState((s) => ({
        sessions: s.sessions.map((x) => (x.id === currentSessionId ? { ...x, permission_mode: mode } : x)),
      }));
    }
  };

  const shortPathName = (fullPath: string) => {
    const segs = fullPath.replace(/\\/g, "/").split("/").filter(Boolean);
    return segs[segs.length - 1] || fullPath;
  };

  const canSend = Boolean(input.trim() || attachments.length > 0 || composerBrowserRefs.length > 0);

  const handleCancel = () => {
    void cancelTurn();
  };

  const [sending, setSending] = useState(false);
  const handleSend = async () => {
    if (!canSend || sending) return;
    setSending(true);
    const content = input.trim();
    const attachmentPayload = attachments.map((a) => ({ ...a }));
    const mode = composerMode;

    if (composerBrowserRefs.length > 0) {
      clearComposerBrowserRefs();
    }

    const sendEffort = activeEffort ?? undefined;
    const usedModelId = sessionModelId;

    if (isHome) {
      try {
        const pId = activeProjectId ?? activeProjects[0]?.id ?? null;
        if (pId == null) return;
        // plan-546/547: 模型与模式随创建一次落准；深度写入新会话草稿；全局最近值同步
        // plan-676: 首页目标同样随创建一次落准（goal_status=active）
        const sessionId = await useChatStore.getState().createSession(pId, content.slice(0, 30) || "新对话", {
          model_id: usedModelId,
          permission_mode: mode,
          goal_text: homeGoalText,
        });
        if (sessionId == null) return;
        useDraftsStore.getState().patchDraft(`s${sessionId}`, { reasoningEffort: sendEffort ?? null });
        if (usedModelId != null) useChatStore.setState({ lastModelId: usedModelId });
        const sendMode = (mode === "plan" || mode === "readonly") ? mode : null;
        await sendTurn(content, attachmentPayload, sendEffort, sendMode, usedModelId);
        skipDraftSyncRef.current = true;
        setInput("");
        setAttachments([]);
        setEffort(null);
        setShowSlash(false);
        setShowAt(false);
        // 发送后清空首页草稿：再点新建任务得到全新空态首页（模式默认、模型/深度承接最近值）
        useDraftsStore.getState().clearDraft("home");
        setHomeGoalText(null);
        onStarted?.();
      } catch {
        /* ignore */
      } finally {
        setSending(false);
      }
      return;
    }
    // 运行中发送 → sendTurn 内部入队，turn 完成后自动续发
    const sendMode = (mode === "plan" || mode === "readonly") ? mode : null;
        await sendTurn(content, attachmentPayload, sendEffort, sendMode, sessionModelId);
    skipDraftSyncRef.current = true;
    setInput("");
    setAttachments([]);
    setShowSlash(false);
    setShowAt(false);
    if (draftKey !== "new") useDraftsStore.getState().clearDraft(draftKey);
    setSending(false);
  };

  const modeLabel =
    composerMode === "default"
      ? "完全访问"
      : composerMode === "plan"
      ? "计划模式"
      : composerMode === "accept_edits"
      ? "计划执行"
      : "只读模式";

  // 是否处于 AI 结构化提问阶段（直接替换输入框主体）
  const isQuestionMode = !isHome && pendingApproval?.detail?.kind === "question";

  return (
    <div
      className={`composer${dragOver ? " drag-over" : ""}${isHome ? " composer-home" : ""}`}
      onDragOver={(e) => {
        e.preventDefault();
        setDragOver(true);
      }}
      onDragLeave={(e) => {
        e.preventDefault();
        setDragOver(false);
      }}
      onDrop={handleDrop}
    >
      {isHome && (
        <div className="es-card-project">
          <button
            className="es-project-trigger"
            onClick={() => setShowProjectMenu(!showProjectMenu)}
            title={activeProject?.path ?? "选择项目"}
          >
            <IconFolder size={13} />
            <span className="es-project-name">{activeProject ? shortPathName(activeProject.path) : "选择项目…"}</span>
            <IconChevronDown size={11} />
          </button>
          {showProjectMenu && (
            <div className="context-menu es-project-menu" onClick={() => setShowProjectMenu(false)}>
              <div
                className="context-menu-item"
                onClick={() => {
                  void handleAddDirectory();
                }}
              >
                <IconFolder size={12} /> <span>{addingDir ? "添加中…" : "选择本地目录…"}</span>
              </div>
              {activeProjects.length > 0 && <div className="context-menu-divider" />}
              {activeProjects.map((p) => (
                <div
                  key={p.id}
                  className={`context-menu-item${p.id === activeProjectId ? " active" : ""}`}
                  onClick={() => {
                    useChatStore.setState({ currentProjectId: p.id });
                    setShowProjectMenu(false);
                  }}
                  title={p.path}
                >
                  <span>{shortPathName(p.path)}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {!isHome && pendingPlan && (
        <div className="composer-plan-bar">
          <div className="composer-plan-bar-left">
            <IconClipboard size={13} />
            <span className="composer-plan-bar-title">计划已就绪：{pendingPlan.task || "任务执行计划"}</span>
            <span className="composer-plan-bar-hint">（点击【确认执行】或直接在下方输入修改意见）</span>
          </div>
          <div className="composer-plan-bar-actions">
            <button type="button" className="btn-ghost" onClick={() => void dismissPlan()}>
              取消
            </button>
            <button type="button" className="plan-inline-confirm" onClick={() => void confirmPlanTurn(true)}>
              确认执行
            </button>
          </div>
        </div>
      )}

      {/* 核心重构：AI 提问时直接将输入框主体替换为 QuestionWizardBox 卡片（对齐参考图 paste-20260829121505.png） */}
      {isQuestionMode ? (
        <QuestionWizardBox
          detail={pendingApproval.detail}
          onCancel={() => respondApproval(pendingApproval.approvalId, false)}
          onSubmit={(answers) => respondApproval(pendingApproval.approvalId, true, false, answers)}
        />
      ) : (
        <div className="composer-main">
          {/* 紧凑内嵌浏览器标注胶囊块：小巧精致不占空间，点击弹出详情预览 Modal（对齐图 2） */}
          {composerBrowserRefs.length > 0 && (
            <div className="composer-browser-refs">
              {composerBrowserRefs.map((ref) => (
                <div
                  key={ref.id}
                  className={`composer-browser-ref-card kind-${ref.kind}`}
                  onClick={() => setBrowserRefPreview(ref)}
                  title="点击查看标注详情预览"
                >
                  {ref.thumbUrl ? (
                    <img
                      src={resolveFileUrl(ref.thumbUrl)}
                      alt="缩略图"
                      className="composer-ref-chip-thumb"
                    />
                  ) : (
                    <div className="composer-ref-chip-icon">
                      {ref.kind === "element" && <IconTarget size={13} />}
                      {ref.kind === "screenshot" && <IconImage size={13} />}
                      {ref.kind === "dom" && <IconCode size={13} />}
                      {ref.kind === "console" && <IconTerminal size={13} />}
                    </div>
                  )}
                  <div className="composer-ref-card-info">
                    <span className="ref-chip-kind-label">
                      {ref.kind === "element"
                        ? "元素标注"
                        : ref.kind === "screenshot"
                        ? "网页截图"
                        : ref.kind === "dom"
                        ? "DOM快照"
                        : "控制台"}
                    </span>
                    <span className="composer-ref-card-page">{ref.pageTitle}</span>
                    {ref.selector && <span className="composer-ref-card-selector">{ref.selector}</span>}
                    {ref.note && <span className="composer-ref-card-note-brief">· {ref.note}</span>}
                  </div>
                  <button
                    className="composer-ref-card-remove"
                    onClick={(e) => {
                      e.stopPropagation();
                      removeComposerBrowserRef(ref.id);
                    }}
                    title="移除此标注"
                  >
                    <IconX size={11} />
                  </button>
                </div>
              ))}
            </div>
          )}

          {attachments.length > 0 && (
            <div className="composer-attachments">
              {attachments.map((att, idx) => (
                <div
                  key={att.file_id ?? idx}
                  className={`composer-attach-chip${att.type === "image" ? " image" : ""}`}
                  title={att.filename}
                >
                  {att.type === "image" ? (
                    <img
                      src={resolveFileUrl(att.url)}
                      alt={att.filename}
                      className="attach-chip-thumb"
                      onClick={() => setPreview(att)}
                    />
                  ) : (
                    <IconPaperclip size={11} />
                  )}
                  <span className="attach-chip-name" onClick={() => setPreview(att)}>
                    {att.filename}
                  </span>
                  <button
                    className="remove"
                    onClick={() => setAttachments((prev) => prev.filter((_, i) => i !== idx))}
                  >
                    <IconX size={10} />
                  </button>
                </div>
              ))}
              {uploading && <span className="composer-attach-uploading">上传中…</span>}
            </div>
          )}
          {/* plan-671: 目标胶囊——输入框内顶部（排队胶囊上方），复用胶囊语言（对齐 zcode 低打扰）。
              plan-676: 首页变体仅 label+文本+移除（无轮次计数/完成打勾——会话未创建无续跑语义） */}
          {goalPillVisible && (
            <div
              className={`goal-pill${isRunning ? " running" : ""}${goalStatus === "completed" ? " done" : ""}`}
              title={goalText ?? ""}
            >
              <span className="goal-pill-label">
                {goalStatus === "completed" ? "已完成" : isRunning ? "推进中" : "目标"}
              </span>
              <span className="goal-pill-text">{goalText}</span>
              {goalStatus === "active" && !isHome && (
                <span className="goal-pill-count">{goalTurnsUsed}/{goalMaxTurns}</span>
              )}
              {goalStatus === "active" && !isHome && (
                <button
                  className="goal-pill-btn"
                  onClick={completeGoal}
                  title="确认目标已达成（停止自动续跑）"
                  type="button"
                >
                  <IconCheck size={11} />
                </button>
              )}
              {goalStatus === "active" && (
                <button
                  className="goal-pill-btn"
                  onClick={cancelGoal}
                  title={isHome ? "移除目标" : "取消目标"}
                  type="button"
                >
                  <IconX size={11} />
                </button>
              )}
            </div>
          )}
          {/* plan-547: 排队胶囊——内嵌输入框内（textarea 上方）紧凑展示，不再与任务进度/变更贴条叠压 */}
          {!isHome && queuedInputs.length > 0 && (
            <div className="composer-queue-pills">
              {queuedInputs.map((q) => (
                <div key={q.id} className={`composer-queue-pill${q.flushing ? " flushing" : ""}`} title={q.content}>
                  <span className="cq-pill-label">{q.flushing ? "发送中" : "排队"}</span>
                  <span className="cq-pill-text">
                    {q.content || (q.attachments?.length ? `附件 × ${q.attachments.length}` : "")}
                  </span>
                  {q.mode === "plan" && <span className="cq-pill-tag">规划</span>}
                  {q.mode === "readonly" && <span className="cq-pill-tag">只读</span>}
                  <button
                    className="cq-pill-send"
                    onClick={() => void flushQueuedInput(q.id)}
                    title="立即发送（下一次 AI 调用前传达）"
                    type="button"
                  >
                    <IconArrowUp size={10} />
                  </button>
                  <button
                    className="cq-pill-remove"
                    onClick={() => updateQueuedInput(q.id, null)}
                    title="移出队列"
                    type="button"
                  >
                    <IconX size={10} />
                  </button>
                </div>
              ))}
            </div>
          )}
          <textarea
            ref={taRef}
            className="composer-input"
            placeholder={
              isHome
                ? "向 ChatCoder 提问，使用 @ 添加上下文，使用 / 选择命令或能力"
                : pendingPlan
                ? "输入文字提出修改意见，回车继续迭代方案…"
                : "提出后续修改要求"
            }
            value={input}
            rows={1}
            onPaste={handlePaste}
            onWheel={handleTextareaWheel}
            onMouseDown={handleTextareaMouseDown}
            onInput={(e) => resizeTextarea(e.currentTarget)}
            onChange={(e) => {
              const v = e.target.value;
              setInput(v);
              const pos = e.target.selectionStart;
              const isSlash = /(?:^|\s)\/[^\s]*$/.test(v.slice(0, pos));
              setShowSlash(isSlash);
              if (isSlash) setSlashIndex(0);
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
                if (e.key === "ArrowDown") {
                  e.preventDefault();
                  setSlashIndex((i) => (i + 1) % filteredSlash.length);
                  return;
                }
                if (e.key === "ArrowUp") {
                  e.preventDefault();
                  setSlashIndex((i) => (i - 1 + filteredSlash.length) % filteredSlash.length);
                  return;
                }
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  pickSlash(filteredSlash[Math.min(slashIndex, filteredSlash.length - 1)].cmd);
                  return;
                }
                if (e.key === "Escape") {
                  e.preventDefault();
                  setShowSlash(false);
                  return;
                }
              }
              if (showAt && filteredAtFiles.length > 0) {
                if (e.key === "ArrowDown") {
                  e.preventDefault();
                  setAtIndex((i) => (i + 1) % filteredAtFiles.length);
                  return;
                }
                if (e.key === "ArrowUp") {
                  e.preventDefault();
                  setAtIndex((i) => (i - 1 + filteredAtFiles.length) % filteredAtFiles.length);
                  return;
                }
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  pickAtFile(filteredAtFiles[Math.min(atIndex, filteredAtFiles.length - 1)]);
                  return;
                }
                if (e.key === "Escape") {
                  e.preventDefault();
                  setShowAt(false);
                  return;
                }
              }
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                void handleSend();
              }
              if (e.key === "Escape") {
                setShowSlash(false);
                setShowAt(false);
              }
            }}
          />
          <div className="composer-toolbar">
            <div className="composer-tools-left">
              <button className="composer-attach" title="添加附件" onClick={() => fileRef.current?.click()}>
                <IconPlus size={16} />
              </button>
              <div className="composer-mode-wrap">
                <button
                  className={`composer-mode-btn mode-${composerMode}`}
                  onClick={() => {
                    setShowModeMenu((v) => !v);
                    setShowModels(false);
                    setShowReasoning(false);
                  }}
                  title="权限模式"
                >
                  <IconShield size={13} />
                  {modeLabel}
                  <IconChevronDown size={11} />
                </button>
                {showModeMenu && (
                  <div className="composer-menu composer-mode-menu">
                    <div className="composer-menu-title">权限模式</div>
                    <button
                      className={composerMode === "default" ? "active" : ""}
                      onClick={() => setMode("default")}
                    >
                      完全访问
                    </button>
                    <button
                      className={composerMode === "plan" ? "active" : ""}
                      onClick={() => setMode("plan")}
                    >
                      计划模式
                    </button>
                    <button
                      className={composerMode === "readonly" ? "active" : ""}
                      onClick={() => setMode("readonly")}
                    >
                      只读模式
                    </button>
                    {/* plan-671/676: 目标模式入口（会话内与空态首页均可用） */}
                    {(currentSessionId != null || isHome) && (
                      <>
                        <div className="composer-menu-divider" />
                        <button
                          onClick={() => {
                            setGoalInput(goalStatus === "active" ? (goalText ?? "") : "");
                            setShowGoalModal(true);
                            setShowModeMenu(false);
                          }}
                        >
                          <IconTarget size={12} />
                          {goalStatus === "active" ? "修改目标…" : "设定目标…"}
                        </button>
                        {goalStatus === "active" && (
                          <button onClick={cancelGoal}>
                            <IconX size={12} />
                            {isHome ? "移除目标" : "取消目标"}
                          </button>
                        )}
                      </>
                    )}
                  </div>
                )}
              </div>
              <input
                ref={fileRef}
                type="file"
                multiple
                style={{ display: "none" }}
                onChange={(e) => {
                  if (e.target.files) void addFiles(e.target.files);
                  e.target.value = "";
                }}
              />
            </div>
            <div className="composer-right">
              <ModelPicker
                models={models}
                value={sessionModelId}
                onChange={changeModel}
                open={showModels}
                onToggle={() => {
                  setShowModels((v) => !v);
                  setShowReasoning(false);
                }}
              />
              {supportsReasoning && (
                <div className="composer-reasoning">
                  <button
                    className="composer-reasoning-btn"
                    onClick={() => {
                      setShowReasoning((v) => !v);
                      setShowModels(false);
                    }}
                    title="思考深度"
                  >
                    <IconBrain size={11} />
                    {activeEffort || "默认"}
                  </button>
                  {showReasoning && (
                    <div className="composer-menu composer-reasoning-menu">
                      <div className="composer-menu-title">思考深度</div>
                      {activeModel!.reasoning_efforts.map((effort) => (
                        <button
                          key={effort}
                          className={effort === activeEffort ? "active" : ""}
                          onClick={() => changeReasoning(effort)}
                        >
                          {effort}
                        </button>
                      ))}
                      <button
                        className={activeEffort == null ? "active" : ""}
                        onClick={() => changeReasoning(null)}
                      >
                        默认
                      </button>
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
              <button
                className={`composer-ctx${listening ? " listening" : ""}`}
                onClick={toggleVoice}
                title={listening ? "停止录音" : "语音输入"}
              >
                <IconMic size={15} />
              </button>
              {/* plan-547: 运行中按钮互斥——空输入只显示 Stop；输入内容后切换为排队发送（点击入队，入队后变回 Stop） */}
              {!isHome && isRunning && !canSend && (
                <button
                  className="composer-send stop"
                  onClick={handleCancel}
                  title="停止当前任务"
                >
                  <IconStop size={14} />
                </button>
              )}
              {(!isRunning || (!isHome && canSend)) && (
                <button
                  className={`composer-send${isRunning ? " queue" : ""}`}
                  disabled={!canSend}
                  onClick={() => void handleSend()}
                  title={isRunning ? "发送（排队，任务完成后自动发送）" : "发送"}
                >
                  <IconArrowUp size={16} />
                </button>
              )}
            </div>
          </div>
        </div>
      )}

      {dragOver && (
        <div className="composer-drag-overlay">
          <IconPaperclip size={24} />
          <span>松开以添加附件</span>
        </div>
      )}
      <Modal
        open={preview !== null}
        onClose={() => setPreview(null)}
        title={preview?.filename ?? "图片预览"}
        width={900}
      >
        {preview?.type === "image" && (
          <img
            className="composer-image-preview"
            src={resolveFileUrl(preview.url)}
            alt={preview.filename}
          />
        )}
      </Modal>

      {/* plan-671: 设定/修改目标弹层（复用 Modal 与现有输入样式） */}
      <Modal
        open={showGoalModal}
        onClose={() => setShowGoalModal(false)}
        title={goalStatus === "active" ? "修改目标" : "设定目标"}
        subtitle="目标激活后，每轮结束若未标记完成将自动续跑推进，直至达成或达轮次上限"
        width={520}
        actions={
          <>
            <button className="btn" onClick={() => setShowGoalModal(false)}>取消</button>
            <button className="btn primary" onClick={submitGoal} disabled={!goalInput.trim()}>
              {goalStatus === "active" ? "更新目标" : "设定目标"}
            </button>
          </>
        }
      >
        <div className="goal-modal-body">
          <input
            className="sb-rename-input"
            value={goalInput}
            onChange={(e) => setGoalInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && goalInput.trim()) {
                e.preventDefault();
                submitGoal();
              }
            }}
            placeholder="用一句话描述要达成的目标，例如：修复登录页在深色主题下的样式问题"
            autoFocus
          />
        </div>
      </Modal>

      {/* 浏览器标注详情预览弹窗（对齐图 2） */}
      <Modal
        open={browserRefPreview !== null}
        onClose={() => setBrowserRefPreview(null)}
        title={
          browserRefPreview
            ? `${
                browserRefPreview.kind === "element"
                  ? "元素标注"
                  : browserRefPreview.kind === "screenshot"
                  ? "网页截图"
                  : browserRefPreview.kind === "dom"
                  ? "DOM 快照"
                  : "控制台求值"
              } · ${browserRefPreview.pageTitle}`
            : "标注预览"
        }
        width={720}
      >
        {browserRefPreview && (
          <div className="browser-ref-preview-modal">
            {browserRefPreview.thumbUrl && (
              <div className="browser-ref-preview-shot-wrap">
                <img
                  src={resolveFileUrl(browserRefPreview.thumbUrl)}
                  alt="标注截图"
                  className="browser-ref-preview-shot"
                />
              </div>
            )}
            <div className="browser-ref-preview-details">
              <div className="browser-ref-row">
                <span className="label">目标页面：</span>
                <span className="val">{browserRefPreview.pageTitle}</span>
                <a
                  className="url"
                  href={browserRefPreview.pageUrl}
                  target="_blank"
                  rel="noreferrer"
                >
                  {browserRefPreview.pageUrl}
                </a>
              </div>
              {browserRefPreview.selector && (
                <div className="browser-ref-row">
                  <span className="label">CSS 选择器：</span>
                  <code>{browserRefPreview.selector}</code>
                </div>
              )}
              {browserRefPreview.elementText && (
                <div className="browser-ref-row">
                  <span className="label">元素文本：</span>
                  <span className="val">{browserRefPreview.elementText}</span>
                </div>
              )}
              {browserRefPreview.note && (
                <div className="browser-ref-row">
                  <span className="label">用户补充批注：</span>
                  <span className="val note">{browserRefPreview.note}</span>
                </div>
              )}
            </div>
          </div>
        )}
      </Modal>

      {slashVisible && (
        <div className="composer-menu composer-slash">
          <div className="composer-menu-title">快捷命令</div>
          {filteredSlash.map((s, idx) => (
            <button
              key={s.cmd}
              className={idx === slashIndex ? "active" : ""}
              onMouseEnter={() => setSlashIndex(idx)}
              onClick={() => pickSlash(s.cmd)}
            >
              <strong>{s.cmd}</strong>
              <span>{s.desc}</span>
            </button>
          ))}
        </div>
      )}

      {showAt && (
        <div className="composer-menu composer-at">
          <div className="composer-menu-title">引用文件上下文（@）</div>
          {atLoading && <div className="composer-menu-empty">加载文件树…</div>}
          {!atLoading && filteredAtFiles.length === 0 && (
            <div className="composer-menu-empty">{atQuery ? "未找到匹配文件" : "继续输入文件名以补全…"}</div>
          )}
          {!atLoading &&
            filteredAtFiles.map((f, idx) => (
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

      {/* 普通工具审批弹窗（终端命令/危险写入等工具调用审批） */}
      {!isHome && pendingApproval && pendingApproval.detail.kind !== "question" && (
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
              <div className="approval-agent">{String(pendingApproval.detail.agent_name)} 申请执行此工具</div>
            )}
            {pendingApproval.detail.args != null && (
              <pre className="approval-args">{formatApprovalArgs(pendingApproval.detail.args)}</pre>
            )}
            {typeof pendingApproval.detail.summary === "string" && (
              <div className="approval-summary">{pendingApproval.detail.summary}</div>
            )}
            <div className="approval-actions">
              <button
                className="btn-ghost"
                onClick={() => respondApproval(pendingApproval.approvalId, false)}
              >
                取消
              </button>
              <button
                className="btn-ghost"
                onClick={() => respondApproval(pendingApproval.approvalId, true)}
              >
                仅本次执行
              </button>
              <button
                className="btn-ghost"
                onClick={() => respondApproval(pendingApproval.approvalId, true, true, undefined, "session")}
                title="自动生成会话级执行策略规则，本会话内同类操作不再询问"
              >
                当前会话允许
              </button>
              <button
                className="btn-ghost"
                onClick={() => respondApproval(pendingApproval.approvalId, true, true, undefined, "global")}
                title="自动生成全局执行策略规则，所有会话同类操作不再询问"
              >
                始终允许
              </button>
            </div>
          </div>
        </div>
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

function UsageRing({
  pct,
  usage,
  compacting,
}: {
  pct: number;
  usage: UsageDetail | null;
  compacting?: boolean;
}) {
  const [showDetail, setShowDetail] = useState(false);
  const ringRef = useRef<HTMLButtonElement>(null);
  const radius = 10;
  const circ = 2 * Math.PI * radius;
  const offset = circ - (pct / 100) * circ;
  const color = compacting
    ? "var(--warning)"
    : pct > 80
    ? "var(--error)"
    : pct > 50
    ? "var(--warning)"
    : "var(--success)";

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
        style={{
          position: "relative",
          width: 26,
          height: 26,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          borderRadius: "50%",
          border: "none",
          background: "transparent",
          cursor: "pointer",
        }}
      >
        <svg width="26" height="26" viewBox="0 0 26 26">
          <circle cx="13" cy="13" r={radius} fill="none" stroke="var(--border)" strokeWidth="2.5" />
          <circle
            cx="13"
            cy="13"
            r={radius}
            fill="none"
            stroke={color}
            strokeWidth="2.5"
            strokeDasharray={circ}
            strokeDashoffset={offset}
            strokeLinecap="round"
            transform="rotate(-90 13 13)"
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
          <div>
            合计：{formatK(usage.total)}k / 窗口：{formatK(usage.context_window)}k
          </div>
          <div>
            模型：{usage.agent_name || "默认"} · 来源：{usage.source || "未知"}
          </div>
          {usage.breakdown &&
            Object.entries(usage.breakdown).map(([key, value]) => (
              <div key={key}>
                {key}：{formatK(value)}k
              </div>
            ))}
        </div>
      )}
    </div>
  );
}

/** 直接替换输入框的向导式提问卡片组件（对齐参考图 paste-20260829121505.png） */
function QuestionWizardBox({
  detail,
  onCancel,
  onSubmit,
}: {
  detail: Record<string, unknown>;
  onCancel: () => void;
  onSubmit: (answers: Record<string, unknown>) => void;
}) {
  const [stepIndex, setStepIndex] = useState(0);
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [customText, setCustomText] = useState("");
  const customInputRef = useRef<HTMLInputElement>(null);

  const questions = (Array.isArray(detail.questions) ? detail.questions : []) as Array<{
    question?: unknown;
    options?: unknown;
    allow_custom?: unknown;
  }>;
  const total = questions.length;
  const currentQ = questions[stepIndex] ?? {};
  const currentOptions = Array.isArray(currentQ.options) ? (currentQ.options as string[]) : [];
  const allowCustom = currentQ.allow_custom !== false;
  const currentAnswer = answers[String(stepIndex)] ?? "";

  useEffect(() => {
    const prevAns = answers[String(stepIndex)] ?? "";
    if (prevAns && !currentOptions.includes(prevAns)) {
      setCustomText(prevAns);
    } else {
      setCustomText("");
    }
  }, [stepIndex, answers, currentOptions]);

  const handlePickOption = (opt: string) => {
    const updated = { ...answers, [String(stepIndex)]: opt };
    setAnswers(updated);
    setCustomText("");
    if (stepIndex < total - 1) {
      setTimeout(() => setStepIndex((s) => s + 1), 160);
    } else {
      setTimeout(() => onSubmit(updated), 180);
    }
  };

  const handleNextCustom = () => {
    const text = customText.trim() || currentAnswer;
    if (!text) return;
    const updated = { ...answers, [String(stepIndex)]: text };
    setAnswers(updated);
    setCustomText("");
    if (stepIndex < total - 1) {
      setStepIndex((s) => s + 1);
    } else {
      onSubmit(updated);
    }
  };

  const taskTag = String(detail.task_tag || detail.agent_name || "问答偏好");

  return (
    <div className="composer-question-wizard">
      <div className="question-wizard-header">
        <div className="question-wizard-title-group">
          <span className="question-wizard-tag">{taskTag}</span>
          <span className="question-wizard-title-text" title={String(currentQ.question ?? "")}>
            {String(currentQ.question ?? "请确认执行偏好")}
          </span>
        </div>
        <div className="question-wizard-pager">
          <button
            type="button"
            className="question-wizard-pager-btn"
            disabled={stepIndex === 0}
            onClick={() => setStepIndex((s) => Math.max(0, s - 1))}
            title="上一题"
          >
            <IconChevronLeft size={13} />
          </button>
          <span>
            {stepIndex + 1}/{total}
          </span>
          <button
            type="button"
            className="question-wizard-pager-btn"
            disabled={stepIndex >= total - 1}
            onClick={() => setStepIndex((s) => Math.min(total - 1, s + 1))}
            title="下一题"
          >
            <IconChevronRight size={13} />
          </button>
        </div>
      </div>

      {currentOptions.length > 0 && (
        <div className="question-wizard-options">
          {currentOptions.map((opt, optIdx) => {
            const isSelected = currentAnswer === opt;
            return (
              <button
                key={opt}
                type="button"
                className={`question-wizard-opt-btn${isSelected ? " selected" : ""}`}
                onClick={() => handlePickOption(opt)}
              >
                <span className="question-wizard-opt-num">{optIdx + 1}.</span>
                <span className="question-wizard-opt-text">{opt}</span>
              </button>
            );
          })}

          {allowCustom && (
            <div className="question-wizard-custom-row">
              <span className="question-wizard-opt-num">{currentOptions.length + 1}.</span>
              <input
                ref={customInputRef}
                className="question-wizard-custom-input"
                placeholder="输入自定义回答，按回车确认…"
                value={customText}
                onChange={(e) => setCustomText(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    handleNextCustom();
                  }
                }}
              />
            </div>
          )}
        </div>
      )}

      <div className="question-wizard-footer">
        <div className="question-wizard-hint">
          <span>Tab / 上下键切换 · 回车确认</span>
        </div>
        <div className="question-wizard-actions">
          <button
            type="button"
            className="btn-ghost"
            style={{ fontSize: "11.5px", padding: "4px 10px" }}
            onClick={onCancel}
          >
            忽略
          </button>
          <button
            type="button"
            className="plan-inline-confirm"
            onClick={handleNextCustom}
            disabled={!customText.trim() && !currentAnswer}
          >
            {stepIndex < total - 1 ? "继续" : "提交回答"}
          </button>
        </div>
      </div>
    </div>
  );
}
