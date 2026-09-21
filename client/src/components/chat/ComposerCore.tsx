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
  IconBox,
  IconPackage,
  IconPlug,
  IconFileText,
  IconRingProgress,
  IconSpinner,
} from "../icons";
import { Modal } from "../Modal";
import { createPortal } from "react-dom";
import { ModelPicker } from "./ModelPicker";
import { useChatStore, persistLastReasoning, type UsageDetail } from "../../store/chat";
import { useDraftsStore } from "../../store/drafts";
import { useI18n } from "../../store/i18n";
import { api, resolveFileUrl, type AttachmentInfo, type McpServerOut, type PermissionProfileOut, type PluginMarketItem, type SkillOut, type TreeNode } from "../../api/client";
import { openGallery } from "../../store/gallery";
import { useClickOutside } from "../../hooks/useClickOutside";

/** v7: 思考深度档位高低序——与 ModelsPanel REASONING_OPTS 对齐，用于取模型最高档兜底 */
const EFFORT_RANK: Record<string, number> = {
  none: 0, minimal: 1, low: 2, medium: 3, high: 4, xhigh: 5, max: 6,
};

/** v7: 取模型支持的最高思考档位（无支持返回 null）——新建任务冷启动默认 */
function maxEffortOf(reasoningEfforts?: string[] | null): string | null {
  if (!reasoningEfforts || reasoningEfforts.length === 0) return null;
  return reasoningEfforts.reduce((best, cur) =>
    (EFFORT_RANK[cur] ?? -1) > (EFFORT_RANK[best] ?? -1) ? cur : best,
  );
}

export interface ComposerCoreProps {
  variant?: "default" | "chat" | "home" | "compact";
  projectId?: number | null;
  sessionId?: number | null;
  /** 首页变体：会话创建并发出首条消息后回调（宿主用于离开空态页） */
  onStarted?: () => void;
}

/** plan-238-1210 (A2): 引用 chips（@文件 / $技能）——不再内嵌进输入文本。 */
export interface ComposerRef {
  /** plan-282-1441（#9）：新增 "mcp"（连接器）与 "plugin"（插件）引用 */
  kind: "file" | "skill" | "mcp" | "plugin";
  /** 原始值：文件为工作区相对路径，技能为技能名，连接器/插件为各自名称 */
  value: string;
  /** 展示名 */
  label: string;
}

/** 引用 chips → 随消息文本追加的可读引用行（模型可见；后端仍为纯文本透传）。 */
export function buildRefsSuffix(refs: ComposerRef[]): string {
  if (refs.length === 0) return "";
  const files = refs.filter((r) => r.kind === "file").map((r) => `@${r.value}`);
  const skills = refs.filter((r) => r.kind === "skill").map((r) => `$${r.value}`);
  // plan-282-1441（#9）：连接器引用——写明"使用连接器"，引导模型走对应 MCP 工具
  const connectors = refs.filter((r) => r.kind === "mcp").map((r) => r.value);
  const pluginRefs = refs.filter((r) => r.kind === "plugin").map((r) => r.value);
  const lines: string[] = [];
  if (files.length > 0) lines.push(`引用文件：${files.join("、")}`);
  if (skills.length > 0) lines.push(`使用技能：${skills.join("、")}`);
  if (connectors.length > 0) lines.push(`使用连接器：${connectors.join("、")}`);
  if (pluginRefs.length > 0) lines.push(`使用插件：${pluginRefs.join("、")}`);
  return lines.join("\n");
}

export function ComposerCore({ variant = "default", onStarted }: ComposerCoreProps) {
  const { t } = useI18n();
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
  /** plan-238-1210 (A2): 引用 chips（@文件 / $技能）。
   *  不再内嵌进输入文本（那是双层高亮层错位的根源），改为输入框上方可单独删除的 chips；
   *  发送时拼装成可读引用行追加到消息文本，保证模型仍能看到引用。 */
  const [refs, setRefs] = useState<ComposerRef[]>(() => initialDraft?.refs ?? []);
  /** 本会话/首页独立的思考深度（null=跟随全局最近值） */
  const [effort, setEffort] = useState<string | null>(() => initialDraft?.reasoningEffort ?? null);
  /** 展示与发送用的档位：本 key 草稿 → 全局最近 → 冷启动模型最高档（activeModel 就绪后计算，见下方） */
  // activeEffort 在 activeModel 定义之后计算（需依赖 sessionModelId 所选模型的档位）

  /** 首页变体在会话创建前暂存的模型选择（发送时写入新会话） */
  const [homeModelId, setHomeModelId] = useState<number | null>(() => initialDraft?.modelId ?? null);

  // plan-230-1144 M2: 模式列表外置（permission_profiles API），composerMode 不再限死字面量联合
  const [permProfiles, setPermProfiles] = useState<PermissionProfileOut[]>([]);
  useEffect(() => {
    let cancelled = false;
    api.listPermissionProfiles()
      .then((ps) => { if (!cancelled) setPermProfiles(ps); })
      .catch(() => { /* 失败回退静态三项菜单 */ });
    return () => { cancelled = true; };
  }, []);

  const [composerMode, setComposerMode] = useState<string>(
    isHome
      ? initialDraft?.mode ?? "default"
      : (currentSession?.permission_mode as string) || "default"
  );

  useEffect(() => {
    if (currentSession?.permission_mode) {
      setComposerMode(currentSession.permission_mode as string);
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

  // plan-219: 空态首页快捷 chips 预填输入框（detail.text 非空时写入并聚焦）
  useEffect(() => {
    const onPrefillEvt = (e: Event) => {
      const text = (e as CustomEvent<{ text?: string }>).detail?.text;
      if (!text) return;
      setInput(text);
      taRef.current?.focus();
    };
    window.addEventListener("chatcoder:composer-prefill", onPrefillEvt);
    return () => window.removeEventListener("chatcoder:composer-prefill", onPrefillEvt);
  }, []);

  // 会话 229: 拉取可用技能（失败静默——技能菜单/分区整体不渲染）
  useEffect(() => {
    let cancelled = false;
    api.listSkills()
      .then((items) => { if (!cancelled) setSkills(items.filter((s) => s.is_active)); })
      .catch(() => { /* ignore */ });
    return () => { cancelled = true; };
  }, []);

  /** plan-282-1441（#9）：/ 菜单的「连接器」分区——已启用的 MCP（含内置与插件贡献） */
  useEffect(() => {
    let cancelled = false;
    api.listMcpServers()
      .then((items) => { if (!cancelled) setMcpServers(items.filter((m) => m.is_active)); })
      .catch(() => { /* ignore */ });
    return () => { cancelled = true; };
  }, []);

  /** plan-282-1441（#9）：/ 菜单的「插件」分区——已**启用**的插件（未启用不参与补全） */
  useEffect(() => {
    let cancelled = false;
    api.pluginMarketplace()
      .then((res) => {
        if (cancelled) return;
        setPlugins((res.items ?? []).filter((p) => p.installed && p.enabled));
      })
      .catch(() => { /* 未安装插件也不影响菜单其他分区 */ });
    return () => { cancelled = true; };
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
  // 会话 229: 技能列表（/ 菜单技能区 + $ 补全菜单共用）
  const [skills, setSkills] = useState<SkillOut[]>([]);
  /** plan-282-1441（#9）：/ 菜单「连接器」分区数据源（已启用的 MCP） */
  const [mcpServers, setMcpServers] = useState<McpServerOut[]>([]);
  /** plan-282-1441（#9）：/ 菜单「插件」分区数据源（已安装且已启用的插件） */
  const [plugins, setPlugins] = useState<PluginMarketItem[]>([]);
  const [showSkills, setShowSkills] = useState(false);
  const [skillIndex, setSkillIndex] = useState(0);
  const [skillQuery, setSkillQuery] = useState("");
  const recognitionRef = useRef<any>(null);
  const projectMenuRef = useRef<HTMLDivElement>(null);
  const modeMenuRef = useRef<HTMLDivElement>(null);
  const reasoningMenuRef = useRef<HTMLDivElement>(null);
  useClickOutside(projectMenuRef, showProjectMenu, () => setShowProjectMenu(false));
  useClickOutside(modeMenuRef, showModeMenu, () => setShowModeMenu(false));
  useClickOutside(reasoningMenuRef, showReasoning, () => setShowReasoning(false));
  /** / @ $ 弹层容器：用于"键盘上下键切换时把选中项滚入可视区"。
   *  弹层有 max-height + overflow-y:auto，此前只改 index 不滚动内容区，
   *  选中项一旦超出可视区就看不到（用户看不到自己选到哪一项）。 */
  const slashMenuRef = useRef<HTMLDivElement>(null);
  const skillMenuRef = useRef<HTMLDivElement>(null);
  const atMenuRef = useRef<HTMLDivElement>(null);

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
        refs,
        reasoningEffort: effort,
        ...(isHome ? { modelId: homeModelId, mode: composerMode, projectId: currentProjectId } : {}),
      });
    }, 300);
    return () => clearTimeout(t);
  }, [draftKey, input, attachments, refs, effort, isHome, homeModelId, composerMode, currentProjectId]);

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
  // 可用性口径与 ModelPicker 过滤一致：模型启用 + 供应商启用 + trae 需目录可用
  const isUsableModel = (m: (typeof models)[number] | undefined | null): m is (typeof models)[number] =>
    !!m && m.is_active && m.provider_active !== false && !(m.api_format === "trae" && !m.trae_available);
  const fallbackModelId = (models.find(isUsableModel) ?? models[0])?.id ?? null;
  const preferredModelId = isHome
    ? homeModelId ?? lastModelId
    : currentSession?.model_id ?? homeModelId ?? lastModelId;
  // 所选模型被停用/删除（或供应商被禁用）后自动回落到首个可用模型，
  // 避免选择器继续展示一个已不可用的模型（需切换一次才消失的问题）；
  // 模型列表尚未加载完成（空数组）时保持原值，避免启动期闪烁。
  const sessionModelId = (() => {
    if (preferredModelId == null) return fallbackModelId;
    if (models.length === 0) return preferredModelId;
    return isUsableModel(models.find((m) => m.id === preferredModelId)) ? preferredModelId : fallbackModelId;
  })();
  const activeModel = models.find((m) => m.id === sessionModelId) ?? null;
  const supportsReasoning = (activeModel?.reasoning_efforts?.length ?? 0) > 0;
  /** v7: 展示/发送档位——本 key 草稿 → 全局最近 → 冷启动无历史时取所选模型最高档兜底 */
  const activeEffort = effort ?? lastReasoningEffort
    ?? (supportsReasoning ? maxEffortOf(activeModel?.reasoning_efforts) : null);

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

  /** 会话 229: / 菜单技能区（与快捷命令共用 "/" 前缀过滤词） */
  const filteredSlashSkills = useMemo(() => {
    const match = input.match(/(?:^|\s)\/([^\s]*)$/);
    if (!match) return [];
    const q = match[1].toLowerCase();
    if (!q) return skills;
    return skills.filter(
      (s) => s.name.toLowerCase().includes(q) || (s.display_name ?? "").toLowerCase().includes(q)
    );
  }, [input, skills]);

  /** plan-282-1441（#9）：/ 菜单「连接器」分区（已启用的 MCP，共用 "/" 过滤词） */
  const filteredSlashMcp = useMemo(() => {
    const match = input.match(/(?:^|\s)\/([^\s]*)$/);
    if (!match) return [];
    const q = match[1].toLowerCase();
    if (!q) return mcpServers;
    return mcpServers.filter(
      (m) => m.name.toLowerCase().includes(q) || (m.display_name ?? "").toLowerCase().includes(q)
    );
  }, [input, mcpServers]);

  /** 会话 229: $ 补全菜单技能过滤（仅技能） */
  const filteredSkills = useMemo(() => {
    const q = skillQuery;
    if (!q) return skills;
    return skills.filter(
      (s) => s.name.toLowerCase().includes(q) || (s.display_name ?? "").toLowerCase().includes(q)
    );
  }, [skills, skillQuery]);

  /** plan-282-1441（#9）：/ 菜单「插件」分区（已启用的插件，共用 "/" 过滤词） */
  const filteredSlashPlugins = useMemo(() => {
    const match = input.match(/(?:^|\s)\/([^\s]*)$/);
    if (!match) return [];
    const q = match[1].toLowerCase();
    if (!q) return plugins;
    return plugins.filter(
      (p) => p.name.toLowerCase().includes(q)
        || (p.displayName ?? "").toLowerCase().includes(q)
        || (p.descriptionZh ?? "").toLowerCase().includes(q),
    );
  }, [input, plugins]);

  /** 会话 229: / 菜单扁平项（命令 + 技能 + 连接器 + 插件，键盘导航共用同一索引） */
  const slashItems = useMemo(
    () => [
      ...filteredSlash.map((s) => ({ kind: "cmd" as const, key: s.cmd })),
      ...filteredSlashSkills.map((s) => ({ kind: "skill" as const, key: s.name })),
      ...filteredSlashMcp.map((m) => ({ kind: "mcp" as const, key: m.name })),
      ...filteredSlashPlugins.map((p) => ({ kind: "plugin" as const, key: p.name })),
    ],
    [filteredSlash, filteredSlashSkills, filteredSlashMcp, filteredSlashPlugins]
  );

  const slashVisible = showSlash && slashItems.length > 0;

  /** 问题13: @ 文件搜索——按查询词走全量文件搜索接口（无深度/数量限制），防抖 250ms */
  const atDebounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const searchAtFiles = useCallback(async (pid: number, q: string) => {
    setAtLoading(true);
    try {
      // 空查询：回退到项目树初始建议（仅展示用）；非空：全量路径子串搜索
      const paths = q.trim()
        ? await api.projectFileSearch(pid, q)
        : await api.getProjectTree(pid, 6).then((tree) => {
            const out: string[] = [];
            const walk = (nodes: TreeNode[]) => {
              for (const n of nodes) {
                if (out.length >= 15) return;
                if (n.type === "file") out.push(n.path.replace(/\\/g, "/"));
                if (n.children) walk(n.children);
              }
            };
            walk(tree.children || []);
            return out;
          });
      setAtFiles(paths);
    } catch {
      setAtFiles([]);
    } finally {
      setAtLoading(false);
    }
  }, []);

  const queueAtSearch = useCallback((pid: number, q: string) => {
    if (atDebounceRef.current) clearTimeout(atDebounceRef.current);
    atDebounceRef.current = setTimeout(() => void searchAtFiles(pid, q), 250);
  }, [searchAtFiles]);

  useEffect(() => () => { if (atDebounceRef.current) clearTimeout(atDebounceRef.current); }, []);

  const filteredAtFiles = useMemo(() => {
    if (!atQuery) return atFiles.slice(0, 15);
    return atFiles.filter((p) => p.toLowerCase().includes(atQuery)).slice(0, 15);
  }, [atFiles, atQuery]);

  /** 把弹层内当前选中项（.active）滚入可视区。
   *  不用 scrollIntoView：它会连带滚动祖先容器（消息流会跟着动），
   *  这里只改弹层自己的 scrollTop，副作用最小。 */
  const scrollActiveIntoView = (menu: HTMLDivElement | null) => {
    if (!menu) return;
    const el = menu.querySelector<HTMLElement>("button.active");
    if (!el) return;
    // 弹层自身 position:absolute，是子项的 offsetParent，故 offsetTop 可直接用
    const top = el.offsetTop;
    const bottom = top + el.offsetHeight;
    if (top < menu.scrollTop) menu.scrollTop = top;
    else if (bottom > menu.scrollTop + menu.clientHeight) {
      menu.scrollTop = bottom - menu.clientHeight;
    }
  };

  // 键盘切换（以及列表内容变化）后，选中项必须在可视区内
  useEffect(() => { scrollActiveIntoView(slashMenuRef.current); },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [slashIndex, slashItems.length, slashVisible]);
  useEffect(() => { scrollActiveIntoView(skillMenuRef.current); },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [skillIndex, filteredSkills.length, showSkills]);
  useEffect(() => { scrollActiveIntoView(atMenuRef.current); },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [atIndex, filteredAtFiles.length, showAt]);

  /** plan-238-1210 (A2): 选择文件 → 追加引用 chip（不再把 @路径 写回输入文本）。
   *  同时清掉用户已敲的 `@查询词`，避免残留半截文本。 */
  const pickAtFile = (filePath: string) => {
    const pos = taRef.current?.selectionStart ?? input.length;
    // 去掉触发用的 `@查询词`（仅当光标前确实在输入 @ 时才替换）
    const before = input.slice(0, pos);
    const cleaned = before.replace(/@([^\s]*)$/, "");
    setInput(cleaned + input.slice(pos));
    const label = filePath.replace(/\\/g, "/").split("/").pop() || filePath;
    setRefs((prev) => (prev.some((r) => r.kind === "file" && r.value === filePath)
      ? prev
      : [...prev, { kind: "file", value: filePath, label }]));
    setShowAt(false);
    setAtQuery("");
    setTimeout(() => {
      if (taRef.current) {
        taRef.current.focus();
        taRef.current.selectionStart = taRef.current.selectionEnd = cleaned.length;
        resizeTextarea(taRef.current);
      }
    }, 0);
  };

  /** plan-238-1210 (A2): 输入框回归原生渲染——不再有"透明 textarea + 标签高亮层"的
   *  双层结构（两层排版引擎必须逐像素一致，边界无穷：宽度口径/折行/字距/缩放…）。
   *  现在 textarea 直接显示完整文本，滚动由 textarea 自身承担，高度自适应到 400px 上限。 */
  const resizeTextarea = (el: HTMLTextAreaElement, retry = 0) => {
    if (el.getClientRects().length === 0) {
      // 隐藏/未挂载态 scrollHeight 不可靠，等可见后再测（rAF 有限重试）
      if (retry < 30) requestAnimationFrame(() => resizeTextarea(el, retry + 1));
      return;
    }
    el.style.height = "auto";
    const clamped = Math.max(36, Math.min(el.scrollHeight, 400));
    el.style.height = `${clamped}px`;
    // 高度收缩后清掉残留滚动，避免可视区停在中部（"原本有字的地方空白"）
    if (el.scrollHeight <= el.clientHeight + 1 && el.scrollTop !== 0) el.scrollTop = 0;
  };

  useEffect(() => {
    if (taRef.current) resizeTextarea(taRef.current);
    // 切换会话后草稿回填/面板重新可见，必须重测高度
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [input, currentSessionId]);

  // 窗口/面板宽度变化会改变换行高度，同步重测
  useEffect(() => {
    let raf = 0;
    const onResize = () => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => {
        if (taRef.current) resizeTextarea(taRef.current);
      });
    };
    window.addEventListener("resize", onResize);
    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("resize", onResize);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /** plan-238-1210 (A2): 滚动回到了 textarea 自身——到顶/到底时把滚轮让给外层
   *  消息流，其余情况留在输入框内滚动（不再有 wrap 滚动容器）。 */
  const handleComposerWheel = (e: WheelEvent<HTMLTextAreaElement>) => {
    const el = e.currentTarget;
    const { scrollTop, scrollHeight, clientHeight } = el;
    const atTop = scrollTop <= 0;
    const atBottom = Math.ceil(scrollTop + clientHeight) >= scrollHeight;
    if ((e.deltaY < 0 && atTop) || (e.deltaY > 0 && atBottom)) {
      return;
    }
    e.stopPropagation();
  };

  /** 会话 229: 打开输入框图片附件的全局查看器（多图可左右切换；非图片新窗口打开） */
  const openAttachmentPreview = (att: AttachmentInfo) => {
    const imgs = attachments.filter((a) => a.type === "image" || a.mime_type.startsWith("image/"));
    if (imgs.length === 0) return;
    const list = imgs.map((a) => ({ url: resolveFileUrl(a.url), name: a.filename, size: a.size }));
    const idx = imgs.indexOf(att);
    openGallery(list, idx < 0 ? 0 : idx);
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

  /** 会话 229: 清掉触发菜单的前缀片段（保留前导空白），光标落回原位置。
   *  plan-238-1210 (A2): 技能引用改走 chips，只清触发词、不注入 `$name`。 */
  const clearTriggerPrefix = (pattern: RegExp) => {
    const pos = taRef.current?.selectionStart ?? input.length;
    const before = input.slice(0, pos).replace(pattern, (_m: string, ws?: string) => ws ?? "");
    setInput(before + input.slice(pos));
    setTimeout(() => {
      if (taRef.current) {
        taRef.current.focus();
        taRef.current.selectionStart = taRef.current.selectionEnd = before.length;
        resizeTextarea(taRef.current);
      }
    }, 0);
  };

  /** 会话 229: 选择技能（/ 菜单或 $ 菜单）→ plan-238-1210 (A2): 追加引用 chip。 */
  const insertSkill = (name: string, fromSlash: boolean) => {
    clearTriggerPrefix(fromSlash ? /(^|\s)\/[^\s]*$/ : /(^|\s)\$[^\s]*$/);
    // plan-282-1441（#9）：chip 展示可读名，value 仍是技能标识（$name 语义依赖它）
    const label = skills.find((s) => s.name === name)?.display_name || name;
    setRefs((prev) => (prev.some((r) => r.kind === "skill" && r.value === name)
      ? prev
      : [...prev, { kind: "skill", value: name, label }]));
    setShowSlash(false);
    setShowSkills(false);
  };

  /** plan-282-1441（#9）：选择连接器（/ 菜单）→ 追加 MCP 引用 chip。
   *  发送时会在消息里写明"使用连接器"，让模型明确走该 MCP；是否真正注入工具仍由
   *  MCP 的启用状态决定（服务端 get_agent_mcp_servers）。 */
  const insertConnector = (name: string, label: string) => {
    clearTriggerPrefix(/(^|\s)\/[^\s]*$/);
    setRefs((prev) => (prev.some((r) => r.kind === "mcp" && r.value === name)
      ? prev
      : [...prev, { kind: "mcp", value: name, label }]));
    setShowSlash(false);
  };

  /** plan-282-1441（#9）：选择插件（/ 菜单）→ 追加插件引用 chip。
   *  插件本身不是工具，它通过贡献技能/连接器生效；这里写明"使用插件"，
   *  让模型知道该按该插件的工作流（其技能）来做事。 */
  const insertPlugin = (name: string, label: string) => {
    clearTriggerPrefix(/(^|\s)\/[^\s]*$/);
    setRefs((prev) => (prev.some((r) => r.kind === "plugin" && r.value === name)
      ? prev
      : [...prev, { kind: "plugin", value: name, label }]));
    setShowSlash(false);
  };

  /** 会话 229: / 菜单项选择（命令走原逻辑，技能/连接器/插件走插入） */
  const pickSlashItem = (item: { kind: "cmd" | "skill" | "mcp" | "plugin"; key: string }) => {
    if (item.kind === "cmd") pickSlash(item.key);
    else if (item.kind === "mcp") {
      const m = mcpServers.find((x) => x.name === item.key);
      insertConnector(item.key, m?.display_name || item.key);
    } else if (item.kind === "plugin") {
      const pl = plugins.find((x) => x.name === item.key);
      insertPlugin(item.key, pl?.displayName || item.key);
    } else insertSkill(item.key, true);
  };

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
    // v7: 全局最近值同时落 localStorage，重启后可恢复上次选择（新建任务默认跟随全局）
    setEffort(e);
    useChatStore.setState({ lastReasoningEffort: e });
    persistLastReasoning(e);
    setShowReasoning(false);
  };

  const setMode = (mode: string) => {
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
    // 浏览器标注/截图引用 → 转换为消息附件 + 结构化文本，随消息一并发送
    // （此前仅 clearComposerBrowserRefs() 丢弃引用，导致标注既不在消息里、AI 也收不到）
    const refAttachments: Record<string, unknown>[] = [];
    const refTextParts: string[] = [];
    for (const ref of composerBrowserRefs) {
      if (ref.attachment) refAttachments.push({ ...ref.attachment });
      const lines: string[] = [];
      lines.push(`[浏览器标注 · ${ref.pageTitle}] 页面: ${ref.url}`);
      if (ref.kind === "element") {
        if (ref.selector) lines.push(`元素: ${ref.selector}`);
        if (ref.bbox) lines.push(`位置: (${ref.bbox.x}, ${ref.bbox.y}) 尺寸: ${ref.bbox.width}×${ref.bbox.height}px`);
        if (ref.styleDigest) lines.push(`样式: ${ref.styleDigest}`);
        if (ref.text) lines.push(`内容: ${ref.text}`);
        if (ref.note) lines.push(`说明: ${ref.note}`);
        if (ref.thumbUrl) lines.push(`截图: ${ref.thumbUrl}`);
      } else if (ref.kind === "dom" && ref.text) {
        lines.push(`快照: ${ref.text}`);
      } else if (ref.kind === "console" && ref.text) {
        lines.push(`求值: ${ref.text}`);
      }
      refTextParts.push(lines.join("\n"));
    }
    const refsSuffix = buildRefsSuffix(refs);
    // plan-308-1542 需求2：引用芯片的**结构化**副本——随消息落库，消息流据此渲染
    // 与输入框一致的带图标芯片（纯文本后缀继续保留，模型上下文不变）。
    const refPayload = refs.map((r) => ({ kind: r.kind, value: r.value, label: r.label }));
    const hasPriorText = Boolean(input.trim()) || refTextParts.length > 0;
    const content = (input.trim() ? input.trim() : "") +
      (refTextParts.length ? `${input.trim() ? "\n\n" : ""}${refTextParts.join("\n\n")}` : "") +
      (refsSuffix ? `${hasPriorText ? "\n\n" : ""}${refsSuffix}` : "");
    const attachmentPayload = [...attachments.map((a) => ({ ...a })), ...refAttachments];
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
        const sessionId = await useChatStore.getState().createSession(pId, input.trim().slice(0, 30) || "新对话", {
          model_id: usedModelId,
          permission_mode: mode,
          goal_text: homeGoalText,
        });
        if (sessionId == null) return;
        useDraftsStore.getState().patchDraft(`s${sessionId}`, { reasoningEffort: sendEffort ?? null });
        if (usedModelId != null) useChatStore.setState({ lastModelId: usedModelId });
        // plan-230-1144 M2: 除 default/accept_edits（无命令模式语义）外一律透传——
    // 自定义模式名也要传给引擎，否则后端按 "default" 解析出全量工具集，
    // 出现"选了受限模式但模型仍看到全量工具"的错位。
    const sendMode = (mode === "default" || mode === "accept_edits") ? null : mode;
        await sendTurn(content, attachmentPayload, sendEffort, sendMode, usedModelId, refPayload);
        skipDraftSyncRef.current = true;
        setInput("");
        setAttachments([]);
        setRefs([]);
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
    // plan-230-1144 M2: 除 default/accept_edits（无命令模式语义）外一律透传——
    // 自定义模式名也要传给引擎，否则后端按 "default" 解析出全量工具集，
    // 出现"选了受限模式但模型仍看到全量工具"的错位。
    const sendMode = (mode === "default" || mode === "accept_edits") ? null : mode;
        await sendTurn(content, attachmentPayload, sendEffort, sendMode, sessionModelId, refPayload);
    skipDraftSyncRef.current = true;
    setInput("");
    setAttachments([]);
    setRefs([]);
    setShowSlash(false);
    setShowAt(false);
    // v7: 发送后仅清文字/附件，保留思考深度等输入框配置——深度按会话持久化（重进/重启不丢）
    if (draftKey !== "new") useDraftsStore.getState().clearDraftText(draftKey);
    setSending(false);
  };

  // plan-230-1144 M2: 模式标签——内置模式走 i18n，自定义模式用配置里的 display_name
  const BUILTIN_MODE_KEYS: Record<string, string> = {
    default: "composer.mode_full",
    plan: "composer.mode_plan",
    readonly: "composer.mode_readonly",
    accept_edits: "composer.mode_plan_exec",
  };
  const modeLabel = (() => {
    const key = BUILTIN_MODE_KEYS[composerMode];
    if (key) return t(key);
    const p = permProfiles.find((x) => x.name === composerMode);
    return p ? p.display_name : composerMode;
  })();

  // 是否处于 AI 结构化提问阶段（直接替换输入框主体）
  const isQuestionMode = !isHome && pendingApproval?.detail?.kind === "question";

  /** 会话 229: 输入框 placeholder（textarea 文字透明后由标签层渲染，保持视觉一致） */
  const placeholderText = isHome
    ? t("composer.placeholder_home")
    : pendingPlan
    ? t("composer.placeholder_plan")
    : t("composer.placeholder_followup");

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
        <div className="es-card-project" ref={projectMenuRef}>
          <button
            className="es-project-trigger"
            onClick={() => setShowProjectMenu(!showProjectMenu)}
            title={activeProject?.path ?? t("composer.select_project")}
          >
            <IconFolder size={13} />
            <span className="es-project-name">{activeProject ? shortPathName(activeProject.path) : t("composer.select_project")}</span>
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
                <IconFolder size={12} /> <span>{addingDir ? t("composer.adding_dir") : t("composer.choose_local_dir")}</span>
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

      {/* plan-282-1434（B1）：原「计划已就绪」横幅（.composer-plan-bar）已删除。
          确认/取消动作移入计划卡内部下方居中（见 PlanCard 的 footer）——
          此前确认入口在输入框上方、计划内容在消息流里，两者分离，视线要来回跳；
          且该横幅与卡片"待确认"徽标语义重复。 */}

      {/* 核心重构：AI 提问时直接将输入框主体替换为 QuestionWizardBox 卡片（对齐参考图 paste-20260829121505.png） */}
      {isQuestionMode ? (
        <QuestionWizardBox
          approvalId={pendingApproval.approvalId}
          detail={pendingApproval.detail}
          onCancel={() => respondApproval(pendingApproval.approvalId, false)}
          onSubmit={(answers) => respondApproval(pendingApproval.approvalId, true, false, answers)}
        />
      ) : (
        <div className={`composer-main${dragOver ? " is-drag-over" : ""}`}>
          {/* plan-282-1421（图4）：拖拽提示内联在输入卡内（不再外挂虚线遮罩）——
              外挂遮罩挂在 .composer 上（含 32px 横向内距）会与真实输入卡错位一圈，
              且与卡片圆角/贴条态圆角对不上。现在边框由 .composer-main 自己承载。 */}
          {dragOver && (
            <div className="composer-drop-hint">
              <IconPaperclip size={16} />
              <span>松开以添加附件</span>
            </div>
          )}
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
                      onClick={() => openAttachmentPreview(att)}
                    />
                  ) : (
                    <IconPaperclip size={11} />
                  )}
                  <span
                    className="attach-chip-name"
                    onClick={() =>
                      att.type === "image"
                        ? openAttachmentPreview(att)
                        : window.open(resolveFileUrl(att.url), "_blank", "noopener")
                    }
                  >
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
                  title={isHome ? t("composer.remove_goal") : t("composer.cancel_goal")}
                  type="button"
                >
                  <IconX size={11} />
                </button>
              )}
            </div>
          )}
          {/* plan-547 → plan-282-1421（第5项）：排队消息展示重做。
              信息层级：序号 → 模式标签 → 内容摘要 → 附件数 → 立即发送 / 移除。
              交互：整条可 hover 高亮；发送中显示旋转指示；入队/出队有动效（见 CSS）。 */}
          {!isHome && queuedInputs.length > 0 && (
            <div className="composer-queue">
              <div className="composer-queue-head">
                <span className="cq-head-label">{t("composer.queue_label")}</span>
                <span className="cq-head-count">{queuedInputs.length}</span>
              </div>
              <div className="composer-queue-pills">
                {queuedInputs.map((q, i) => (
                  <div
                    key={q.id}
                    className={`composer-queue-pill${q.flushing ? " flushing" : ""}`}
                    title={q.content}
                  >
                    <span className="cq-pill-index">{i + 1}</span>
                    {q.mode === "plan" && <span className="cq-pill-tag">规划</span>}
                    {q.mode === "readonly" && <span className="cq-pill-tag">只读</span>}
                    <span className="cq-pill-text">
                      {q.content || (q.attachments?.length ? "（仅附件）" : "")}
                    </span>
                    {(q.attachments?.length ?? 0) > 0 && (
                      <span className="cq-pill-attach" title={`${q.attachments?.length ?? 0} 个附件`}>
                        <IconPaperclip size={10} />
                        {q.attachments?.length ?? 0}
                      </span>
                    )}
                    {q.flushing ? (
                      <span className="cq-pill-sending" title={t("composer.queue_sending")}>
                        <IconSpinner size={11} />
                      </span>
                    ) : (
                      <button
                        className="cq-pill-btn send"
                        onClick={() => void flushQueuedInput(q.id)}
                        title={t("composer.queue_flush_now")}
                        aria-label={t("composer.queue_flush_now")}
                        type="button"
                      >
                        <IconArrowUp size={11} />
                      </button>
                    )}
                    <button
                      className="cq-pill-btn remove"
                      onClick={() => updateQueuedInput(q.id, null)}
                      title={t("composer.queue_remove")}
                      aria-label={t("composer.queue_remove")}
                      type="button"
                    >
                      <IconX size={11} />
                    </button>
                  </div>
                ))}
              </div>
            </div>
          )}
          {/* plan-238-1210 (A2): 引用 chips 行（@文件 / $技能）——不再内嵌进输入文本，
              从结构上消除"透明 textarea + 标签高亮层"的排版错位问题。 */}
          {refs.length > 0 && (
            <div className="composer-refs">
              {refs.map((r, i) => (
                <span
                  key={`${r.kind}-${r.value}-${i}`}
                  className={`composer-ref-chip composer-ref-${r.kind}`}
                  title={r.value}
                >
                  <span className="composer-ref-icon">
                    {/* plan-282-1441（#9）：四类引用用不同图标区分（文件/技能/连接器/插件） */}
                    {r.kind === "file" ? <IconFileText size={11} />
                      : r.kind === "mcp" ? <IconPlug size={11} />
                      : r.kind === "plugin" ? <IconPackage size={11} />
                      : <IconBox size={11} />}
                  </span>
                  <span className="composer-ref-name">{r.label}</span>
                  <button
                    type="button"
                    className="composer-ref-remove"
                    title={r.kind === "file" ? "移除文件引用"
                      : r.kind === "mcp" ? "移除连接器引用"
                      : r.kind === "plugin" ? "移除插件引用"
                      : "移除技能引用"}
                    onClick={() => setRefs((prev) => prev.filter((_, idx) => idx !== i))}
                  >
                    <IconX size={10} />
                  </button>
                </span>
              ))}
            </div>
          )}
          <div className="composer-input-wrap">
            <textarea
              ref={taRef}
              className="composer-input"
              placeholder={placeholderText}
              spellCheck={false}
              value={input}
              rows={1}
              onPaste={handlePaste}
              onWheel={handleComposerWheel}
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
                // 问题13: 触发全量文件搜索（携带查询词，防抖）
                const pid = isHome ? activeProjectId : currentProjectId;
                if (pid != null) void queueAtSearch(pid, atMatch[1]);
              } else {
                setShowAt(false);
                setAtQuery("");
              }
              // 会话 229: $ 技能补全菜单（与 /、@ 菜单互斥）
              const skillMatch = v.slice(0, pos).match(/\$([^\s]*)$/);
              if (skillMatch) {
                setShowSkills(true);
                setSkillQuery(skillMatch[1].toLowerCase());
                setSkillIndex(0);
                setShowSlash(false);
                setShowAt(false);
              } else {
                setShowSkills(false);
                setSkillQuery("");
              }
            }}
            onKeyDown={(e) => {
              // plan-238-1210 (A2): 文本中不再有 @/$ 引用片段（已改为 chips），
              // 原"整词删除拦截"随之移除——Backspace/Delete 回归原生行为。
              if (slashVisible) {
                if (e.key === "ArrowDown") {
                  e.preventDefault();
                  setSlashIndex((i) => (i + 1) % slashItems.length);
                  return;
                }
                if (e.key === "ArrowUp") {
                  e.preventDefault();
                  setSlashIndex((i) => (i - 1 + slashItems.length) % slashItems.length);
                  return;
                }
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  pickSlashItem(slashItems[Math.min(slashIndex, slashItems.length - 1)]);
                  return;
                }
                if (e.key === "Escape") {
                  e.preventDefault();
                  setShowSlash(false);
                  return;
                }
              }
              // 会话 229: $ 技能补全菜单键盘导航
              if (showSkills) {
                const n = filteredSkills.length;
                if (e.key === "ArrowDown") {
                  e.preventDefault();
                  setSkillIndex((i) => (i + 1) % Math.max(1, n));
                  return;
                }
                if (e.key === "ArrowUp") {
                  e.preventDefault();
                  setSkillIndex((i) => (i - 1 + Math.max(1, n)) % Math.max(1, n));
                  return;
                }
                if (e.key === "Enter" && !e.shiftKey && n > 0) {
                  e.preventDefault();
                  insertSkill(filteredSkills[Math.min(skillIndex, n - 1)].name, false);
                  return;
                }
                if (e.key === "Escape") {
                  e.preventDefault();
                  setShowSkills(false);
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
                setShowSkills(false);
              }
            }}
            />
          </div>
          <div className="composer-toolbar">
            <div className="composer-tools-left">
              <button className="composer-attach" title={t("composer.attach_tip")} onClick={() => fileRef.current?.click()}>
                <IconPlus size={16} />
              </button>
              <div className="composer-mode-wrap" ref={modeMenuRef}>
                <button
                  className={`composer-mode-btn mode-${composerMode}`}
                  onClick={() => {
                    setShowModeMenu((v) => !v);
                    setShowModels(false);
                    setShowReasoning(false);
                  }}
                  title={t("composer.mode_title")}
                >
                  <IconShield size={13} />
                  {modeLabel}
                  <IconChevronDown size={11} />
                </button>
                {showModeMenu && (
                  <div className="composer-menu composer-mode-menu">
                    <div className="composer-menu-title">{t("composer.mode_title")}</div>
                    {/* plan-230-1144 M2: 菜单项由 /permission-profiles 下发（内置+自定义）；
                        API 失败时回退静态三项。accept_edits 现在也可手动选择/切回。 */}
                    {(permProfiles.length > 0
                      ? permProfiles
                      : [
                          { name: "default", display_name: t("composer.mode_full") } as PermissionProfileOut,
                          { name: "plan", display_name: t("composer.mode_plan") } as PermissionProfileOut,
                          { name: "readonly", display_name: t("composer.mode_readonly") } as PermissionProfileOut,
                        ]
                    ).map((p) => (
                      <button
                        key={p.name}
                        className={composerMode === p.name ? "active" : ""}
                        title={p.description || undefined}
                        onClick={() => setMode(p.name)}
                      >
                        {BUILTIN_MODE_KEYS[p.name] ? t(BUILTIN_MODE_KEYS[p.name]) : p.display_name}
                      </button>
                    ))}
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
                          {goalStatus === "active" ? t("composer.edit_goal") : t("composer.set_goal")}
                        </button>
                        {goalStatus === "active" && (
                          <button onClick={cancelGoal}>
                            <IconX size={12} />
                            {isHome ? t("composer.remove_goal") : t("composer.cancel_goal")}
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
                <div className="composer-reasoning" ref={reasoningMenuRef}>
                  <button
                    className="composer-reasoning-btn"
                    onClick={() => {
                      setShowReasoning((v) => !v);
                      setShowModels(false);
                    }}
                    title={t("composer.reasoning_depth")}
                  >
                    <IconBrain size={11} />
                    {activeEffort || t("composer.reasoning_default")}
                  </button>
                  {showReasoning && (
                    <div className="composer-menu composer-reasoning-menu">
                      <div className="composer-menu-title">{t("composer.reasoning_depth")}</div>
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
                        {t("composer.reasoning_default")}
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

      {/* plan-282-1421（图4）：原 .composer-drag-overlay 外挂虚线遮罩已移除，
          拖拽提示改由 .composer-main.is-drag-over 的边框 + 卡内 .composer-drop-hint 承载。 */}

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
                  title="点击查看大图"
                  onClick={() => {
                    const u = browserRefPreview.thumbUrl;
                    if (u) {
                      openGallery(
                        [{ url: resolveFileUrl(u), name: `${browserRefPreview.pageTitle || "标注"}-截图.png` }],
                        0,
                      );
                    }
                  }}
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
        <div className="composer-menu composer-slash" ref={slashMenuRef}>
          {filteredSlash.length > 0 && <div className="composer-menu-title">快捷命令</div>}
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
          {filteredSlashSkills.length > 0 && <div className="composer-menu-title">技能（$）</div>}
          {filteredSlashSkills.map((s, j) => {
            const idx = filteredSlash.length + j;
            return (
              <button
                key={`sk-${s.id}`}
                className={idx === slashIndex ? "active" : ""}
                onMouseEnter={() => setSlashIndex(idx)}
                onClick={() => insertSkill(s.name, true)}
              >
                <IconBox size={12} />
                {/* plan-282-1441（#9）：主标题显示可读名（与设置页一致）。
                    引用值仍用 s.name——它是稳定标识（$name 语义与工具调用都依赖它）。 */}
                <strong>{s.display_name || s.name}</strong>
                <span>{s.display_name ? `$${s.name}` : (s.description || "")}</span>
              </button>
            );
          })}
          {/* plan-282-1441（#9）：连接器分区（已启用的 MCP，含内置与插件贡献） */}
          {filteredSlashMcp.length > 0 && <div className="composer-menu-title">连接器</div>}
          {filteredSlashMcp.map((m, j) => {
            const idx = filteredSlash.length + filteredSlashSkills.length + j;
            return (
              <button
                key={`mcp-${m.id}`}
                className={idx === slashIndex ? "active" : ""}
                onMouseEnter={() => setSlashIndex(idx)}
                onClick={() => insertConnector(m.name, m.display_name || m.name)}
              >
                <IconPlug size={12} />
                <strong>{m.display_name || m.name}</strong>
                <span>{m.description || m.name}</span>
              </button>
            );
          })}
          {/* plan-282-1441（#9）：插件分区（已安装且已启用的插件） */}
          {filteredSlashPlugins.length > 0 && <div className="composer-menu-title">插件</div>}
          {filteredSlashPlugins.map((p, j) => {
            const idx = filteredSlash.length + filteredSlashSkills.length + filteredSlashMcp.length + j;
            return (
              <button
                key={`pl-${p.name}`}
                className={idx === slashIndex ? "active" : ""}
                onMouseEnter={() => setSlashIndex(idx)}
                onClick={() => insertPlugin(p.name, p.displayName || p.name)}
              >
                <IconBox size={12} />
                <strong>{p.displayName || p.name}</strong>
                <span>{p.descriptionZh || p.description || p.category || ""}</span>
              </button>
            );
          })}
        </div>
      )}

      {showSkills && filteredSkills.length > 0 && (
        <div className="composer-menu composer-slash" ref={skillMenuRef}>
          <div className="composer-menu-title">技能（$）</div>
          {filteredSkills.map((s, idx) => (
            <button
              key={`s-${s.id}`}
              className={idx === skillIndex ? "active" : ""}
              onMouseEnter={() => setSkillIndex(idx)}
              onClick={() => insertSkill(s.name, false)}
            >
              <IconBox size={12} />
              <strong>${s.name}</strong>
              <span>{s.display_name || s.description || ""}</span>
            </button>
          ))}
        </div>
      )}

      {showAt && (
        <div className="composer-menu composer-at" ref={atMenuRef}>
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

/** token 数量格式化（k 单位，去掉多余的 .0）；圆环浮层与旧调用共用的工具函数。 */
function formatK(value: number): string {
  return (Math.max(0, value) / 1000).toFixed(value >= 10000 ? 0 : 1).replace(/\.0$/, "");
}

/** plan-282-1421（第10项）：上下文占用圆环 + 浮层重做。
 *
 * 相对旧实现的改动（对齐参考图 6）：
 *  - **hover/focus 即显示**（不再需要点击）；点击可"钉住"以便移入浮层查看细节；
 *  - **两档信息**：基础态只给「上下文容量 used/window（pct%）」+ 进度条 + 「平均缓存命中率」；
 *    详细态（拿到供应商真实数据：source=api_last 或带 breakdown）才追加实时缓存率、
 *    输入/输出/思考输出拆分与分项 breakdown；
 *  - **压缩中**不再显示文字徽标，圆环自身变旋转加载弧；
 *  - 圆环 26 → **20px**（与工具栏其他图标同一视觉重量）。
 */
function UsageRing({
  pct,
  usage,
  compacting,
}: {
  pct: number;
  usage: UsageDetail | null;
  compacting?: boolean;
}) {
  /** 鼠标在浮层/圆环上时为 true */
  const [hovering, setHovering] = useState(false);
  /** 点击钉住（允许鼠标移入浮层内部滚动查看） */
  const [pinned, setPinned] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);
  const cacheTotals = useChatStore((s) => s.usageCacheTotals);

  const open = (hovering || pinned) && !!usage;

  const color = compacting
    ? "var(--warning)"
    : pct > 80
    ? "var(--error)"
    : pct > 50
    ? "var(--warning)"
    : "var(--success)";

  /** 平均缓存命中率（本任务内累计口径；样本不足时不展示） */
  const avgCacheRate = cacheTotals.samples > 0 && cacheTotals.inputSum > 0
    ? Math.round((cacheTotals.cachedSum / cacheTotals.inputSum) * 100)
    : null;

  // 点击外部关闭"钉住"
  useEffect(() => {
    if (!pinned) return;
    const handler = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setPinned(false);
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [pinned]);

  // 切换会话/收起时复位钉住态
  useEffect(() => {
    if (!usage) setPinned(false);
  }, [usage]);

  return (
    <div
      className="composer-usage-ring-wrap"
      ref={wrapRef}
      onMouseEnter={() => setHovering(true)}
      onMouseLeave={() => setHovering(false)}
    >
      <button
        className="composer-usage-ring"
        data-compacting={compacting || undefined}
        aria-label={compacting ? "正在整理上下文" : `上下文占用 ${pct}%`}
        aria-expanded={open}
        onClick={() => setPinned((v) => !v)}
        onFocus={() => setHovering(true)}
        onBlur={() => setHovering(false)}
        type="button"
      >
        {/* 压缩中：圆环变旋转加载弧（替代原"压缩中"文字徽标） */}
        <IconRingProgress
          value={pct / 100}
          size={20}
          strokeWidth={2}
          loading={Boolean(compacting)}
          trackColor="var(--border)"
          indicatorColor={color}
        />
      </button>

      {open && usage && (
        <div
          className="usage-pop"
          role="tooltip"
          onMouseEnter={() => setHovering(true)}
          onMouseLeave={() => setHovering(false)}
        >
          {/* ── 基础态：容量 + 百分比 + 进度条 + 平均缓存命中率 ── */}
          <div className="usage-pop-head">
            <span className="usage-pop-title">{compacting ? "正在整理上下文" : "上下文容量"}</span>
            <span className="usage-pop-pct">{pct}%</span>
          </div>
          <div className="usage-pop-bar" aria-hidden>
            <span style={{ width: `${Math.min(100, Math.max(0, pct))}%`, background: color }} />
          </div>
          <div className="usage-pop-amount">
            {formatK(usage.total)}k / {formatK(usage.context_window)}k
          </div>
          {/* plan-282-0: 标注占用口径——压缩后本地估算覆盖显示时，明确告知用户
              该数字为估算（真实占用以下一次 API 响应为准），避免"压缩后占用很低"
              的误判（实测估算 14.4% 而真实 69%）。 */}
          {usage.source === "est_after_compact" && (
            <div className="usage-pop-row">
              <span className="usage-pop-key">数据来源</span>
              <span className="usage-pop-val">压缩后估算（下次请求校准）</span>
            </div>
          )}
          {usage.source === "est" && (
            <div className="usage-pop-row">
              <span className="usage-pop-key">数据来源</span>
              <span className="usage-pop-val">本地估算</span>
            </div>
          )}
          <div className="usage-pop-row">
            <span className="usage-pop-key">平均缓存命中率</span>
            <span className="usage-pop-val">{avgCacheRate == null ? "—" : `${avgCacheRate}%`}</span>
          </div>
          {/* plan-282-1434（B3）：浮层瘦身——删除实时缓存命中率、缓存命中、输出、思考输出、
               模型、数据来源与占用构成明细。这些信息要么与"上下文容量"主题无关，
               要么在任务执行之外并不可得；保留的三项（容量/百分比+进度条/平均缓存命中率）
               已能回答"窗口还剩多少、缓存省了多少"这两个核心问题。 */}
        </div>
      )}
    </div>
  );
}

/** 直接替换输入框的向导式提问卡片组件（对齐参考图 paste-20260829121505.png） */
function QuestionWizardBox({
  approvalId,
  detail,
  onCancel,
  onSubmit,
}: {
  /** plan-238-1188: 作答草稿在 store 中按 approvalId 键控，必须显式传入。 */
  approvalId: string;
  detail: Record<string, unknown>;
  onCancel: () => void;
  onSubmit: (answers: Record<string, unknown>) => void;
}) {
  // plan-238-1188: 作答草稿持久化到 store（按 approvalId 键控）。
  // 此前 answers/stepIndex/customText 全为组件本地 state，切到设置页会卸载
  // ComposerCore，返回后已选答案与当前题号全部丢失。
  const questionDraft = useChatStore((s) => s.questionDraft);
  const setQuestionDraft = useChatStore((s) => s.setQuestionDraft);

  const draft = questionDraft && questionDraft.approvalId === approvalId ? questionDraft : null;
  const stepIndex = draft?.stepIndex ?? 0;
  const answers = draft?.answers ?? {};
  const [customText, setCustomText] = useState("");
  const customInputRef = useRef<HTMLInputElement>(null);
  // plan-278-1391: 选项浮窗——单行截断的长选项在悬停/键盘聚焦时完整展示。
  const [optTip, setOptTip] = useState<
    { text: string; top: number; left: number; maxWidth: number; placement: "top" | "bottom" } | null
  >(null);

  /** 仅当选项文本确实被单行截断时才弹浮窗（短选项不打扰）。 */
  const showOptTip = (
    e: React.MouseEvent<HTMLButtonElement> | React.FocusEvent<HTMLButtonElement>,
    text: string,
  ) => {
    const btn = e.currentTarget;
    const span = btn.querySelector<HTMLElement>(".question-wizard-opt-text");
    const el = span ?? btn;
    if (el.scrollWidth <= el.clientWidth + 1) {
      setOptTip(null);
      return;
    }
    const r = btn.getBoundingClientRect();
    const maxWidth = Math.min(560, Math.max(180, window.innerWidth * 0.9));
    // 粗略估算高度用于决定向上/向下翻转（真实高度由 CSS max-height + 滚动兜底）
    const estH = Math.min(240, Math.ceil(text.length / 42) * 18 + 18);
    const spaceBelow = window.innerHeight - r.bottom - 8;
    const placement: "top" | "bottom" =
      spaceBelow < estH && r.top - 8 - estH > 0 ? "top" : "bottom";
    const left = Math.min(Math.max(8, r.left), Math.max(8, window.innerWidth - maxWidth - 8));
    setOptTip({
      text,
      top: placement === "top" ? r.top - 6 : r.bottom + 6,
      left,
      maxWidth,
      placement,
    });
  };
  const hideOptTip = () => setOptTip(null);

  const writeDraft = (next: { stepIndex: number; answers: Record<string, string> }) => {
    setQuestionDraft({ approvalId, stepIndex: next.stepIndex, answers: next.answers });
  };

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
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stepIndex, currentAnswer, currentOptions.join("\u0000")]);

  const handlePickOption = (opt: string) => {
    const updated = { ...answers, [String(stepIndex)]: opt };
    setCustomText("");
    if (stepIndex < total - 1) {
      writeDraft({ stepIndex: stepIndex + 1, answers: updated });
    } else {
      // 末题：落盘草稿后提交（提交成功路径会清空草稿）
      writeDraft({ stepIndex, answers: updated });
      setTimeout(() => onSubmit(updated), 180);
    }
  };

  const handleNextCustom = () => {
    const text = customText.trim() || currentAnswer;
    if (!text) return;
    const updated = { ...answers, [String(stepIndex)]: text };
    setCustomText("");
    if (stepIndex < total - 1) {
      writeDraft({ stepIndex: stepIndex + 1, answers: updated });
    } else {
      writeDraft({ stepIndex, answers: updated });
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
            onClick={() => writeDraft({ stepIndex: Math.max(0, stepIndex - 1), answers })}
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
            onClick={() => writeDraft({ stepIndex: Math.min(total - 1, stepIndex + 1), answers })}
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
                title={opt}
                onMouseEnter={(e) => showOptTip(e, opt)}
                onMouseLeave={hideOptTip}
                onFocus={(e) => showOptTip(e, opt)}
                onBlur={hideOptTip}
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
      {/* plan-278-1391: 选项全文浮窗（portal 到 body，避免被输入框容器裁剪） */}
      {optTip && createPortal(
        <div
          className="question-wizard-opt-tooltip"
          role="tooltip"
          style={{
            top: optTip.top,
            left: optTip.left,
            maxWidth: optTip.maxWidth,
            transform: optTip.placement === "top" ? "translateY(-100%)" : undefined,
          }}
        >
          {optTip.text}
        </div>,
        document.body,
      )}
    </div>
  );
}
