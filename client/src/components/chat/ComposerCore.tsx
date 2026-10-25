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
  useLayoutEffect,
  useMemo,
  type DragEvent,
  type WheelEvent,
} from "react";
import { isBusy } from "../../perf/bus";
import { registerReconcileTask, RECONCILE_ORDER } from "../../perf/reconcile";
import {
  IconPaperclip,
  IconChevronLeft,
  IconChevronRight,
  IconX,
  IconTarget,
  IconCheck,
  IconImage,
  IconCode,
  IconTerminal,
  IconBox,
  IconPlug,
  IconFileText,
  IconRingProgress,
  IconPuzzle,
  IconSparkles,
} from "../icons";
// plan-20261009-ui U2：输入框重做——+ 浮窗 / 思考深度滑块 / 权限 / 工作区 / 排队托盘拆为独立组件
import { IconPlusX, IconSendStop } from "../iconkit/MorphIcons";
import { PlusMenu, type ModeOption } from "../composer/PlusMenu";
import type { ExtItem } from "../composer/ExtensionPicker";
import { ModelEffortPicker } from "../composer/ModelEffortPicker";
import { ApprovalPicker } from "../composer/ApprovalPicker";
import { WorkspaceChip } from "../composer/WorkspaceChip";
import { QueueTray } from "../composer/QueueTray";
import { useToolbarFit } from "../composer/useToolbarFit";
import { BUILTIN_MODES, GOAL_MODE, MODE_META, customModeIcon, EFFORT_RANK } from "../composer/modeMeta";
import type { ExtKind } from "../../store/navIntent";
import { Modal } from "../Modal";
import { createPortal } from "react-dom";
// plan-75-332: 审批卡——与 QuestionWizardBox 同构，需要审批时替换输入框主体
import { ApprovalCard } from "./ApprovalCard";
import { useChatStore, persistLastReasoning, patchSession, useCurrentSession, type UsageDetail } from "../../store/chat";
import { useScoped } from "./convoScope";
import { selectSessionWorkflowBusy, useWorkflowStore } from "../../store/workflow";
import { useDraftsStore, normalizeDraftMode, normalizeApprovalMode, type ApprovalMode } from "../../store/drafts";
import { useI18n } from "../../store/i18n";
import { parseRefLines } from "../../utils/refLines";
import { api, resolveFileUrl, type AttachmentInfo, type ComposerRefOut, type McpServerOut, type PermissionProfileOut, type PluginMarketItem, type SkillOut, type TreeNode } from "../../api/client";
import { openGallery } from "../../store/gallery";
// plan-75-332: 权限模式菜单说明文案走统一 Tooltip（遵循“禁止新增原生 title”）
import { Button, Tooltip } from "../ui";

/** plan-20261009-ui U2：+ 浮窗中执行模式的顺序改为「智能体 → 计划 → 只读」——
 *  最常用的默认模式排在最上，自定义模式排在其后（保持后端返回的相对顺序）。 */
const BUILTIN_MODE_ORDER: readonly string[] = BUILTIN_MODES;

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
  /** 是否响应「聚焦输入框」请求（chatcoder:focus-composer）：主输入框响应，侧栏子会话输入框不抢焦点 */
  followFocusRequest?: boolean;
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

export function ComposerCore({ variant = "default", onStarted, followFocusRequest = true }: ComposerCoreProps) {
  const { t } = useI18n();
  const isHome = variant === "home";
  const taRef = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const isRunning = useScoped("isRunning");
  const currentSessionId = useScoped("currentSessionId");
  const currentProjectId = useChatStore((s) => s.currentProjectId);
  // 会话级「执行中」：主代理轮次在跑、后台工作流在跑（含工作流结束后等待唤醒主代理的空档）、或后台子代理未结束——任一即算。
  // 工作流可能已不占用任何轮次（如「开始执行」只启动了程序），停止按钮要能中断它与子代理，也要能取消待送达的唤醒。
  const workflowBusy = useWorkflowStore((s) => selectSessionWorkflowBusy(s, currentSessionId));
  const pendingSubagents = useScoped("pendingSubagents");
  const busy = isRunning || workflowBusy || pendingSubagents > 0;
  const stopSessionExecution = useScoped("stopSessionExecution");
  const projects = useChatStore((s) => s.projects);
  const models = useChatStore((s) => s.models);
  const lastReasoningEffort = useChatStore((s) => s.lastReasoningEffort);
  const lastModelId = useChatStore((s) => s.lastModelId);
  const queuedInputs = useScoped("queuedInputs");
  const isCompacting = useScoped("isCompacting");
  const usage = useScoped("usage");
  const pendingApproval = useScoped("pendingApproval");
  const approvalQueueLength = useScoped("approvalQueue").length;
  const pendingPlan = useScoped("pendingPlan");

  // plan-156-620：子会话不在主会话列表里——不走 findSession 时模型 / 执行模式 / 权限模式会回落默认值
  const currentSession = useCurrentSession();

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
  // plan-128-518：目标是执行模式菜单的第四项——选中即“待命”，下一条发送的消息就是目标（无二次弹窗）。
  // 待命态只存在于输入框本地；发送后目标写入会话（goal_status=active），由目标胶囊接管展示。
  const [goalArmed, setGoalArmed] = useState(false);
  /** 目标模式是否生效：待命中，或会话已有激活目标 */
  const goalOn = goalArmed || goalStatus === "active";
  // 切换会话 / 回首页时待命态复位（待命只属于当前输入框，不跨会话携带）
  useEffect(() => { setGoalArmed(false); }, [currentSessionId]);
  // 工具条收窄：用 state 持有节点（问答 / 审批态下工具条不渲染，之后再出现时需要重新观察）
  const [toolbarEl, setToolbarEl] = useState<HTMLDivElement | null>(null);
  useToolbarFit(toolbarEl);
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
    useChatStore.setState((s) => patchSession(s, currentSessionId, (x) => ({
      ...x,
      goal_text: out.text ?? null,
      goal_status: (out.status || "none") as typeof x.goal_status,
      goal_turns_used: out.turns_used ?? 0,
    })));
  };

  const cancelGoal = () => {
    setGoalArmed(false);
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

  // plan-75-332: 执行模式——内置 3 档（agent / plan / readonly）。
  // 存量草稿/会话里的旧值（default / accept_edits）经 normalizeDraftMode 归一化为 agent，
  // 否则模式菜单会因为选项里没有旧值而"一个都不高亮"。
  const [composerMode, setComposerMode] = useState<string>(
    isHome
      ? normalizeDraftMode(initialDraft?.mode)
      : normalizeDraftMode(currentSession?.permission_mode)
  );

  // plan-75-332: 权限模式（与执行模式正交）——询问审批 / 自动审批 / 完全访问。
  // 会话级持久化到 session.approval_mode；首页先入草稿，随创建一次落准。
  const [approvalMode, setApprovalMode] = useState<ApprovalMode>(
    isHome
      ? normalizeApprovalMode(initialDraft?.approvalMode)
      : normalizeApprovalMode(currentSession?.approval_mode as string)
  );

  useEffect(() => {
    if (currentSession?.permission_mode) {
      setComposerMode(normalizeDraftMode(currentSession.permission_mode));
    }
  }, [currentSession?.permission_mode, currentSessionId]);

  useEffect(() => {
    if (currentSession?.approval_mode) {
      setApprovalMode(normalizeApprovalMode(currentSession.approval_mode as string));
    }
  }, [currentSession?.approval_mode, currentSessionId]);

  useEffect(() => {
    const onComposerModeEvt = (e: Event) => {
      const mode = (e as CustomEvent<{ mode: string }>).detail?.mode;
      if (mode) setComposerMode(normalizeDraftMode(mode));
    };
    window.addEventListener("chatcoder:composer-mode", onComposerModeEvt);
    return () => window.removeEventListener("chatcoder:composer-mode", onComposerModeEvt);
  }, []);

  // plan-219: 空态首页快捷 chips 预填输入框（detail.text 非空时写入并聚焦）
  useEffect(() => {
    const onPrefillEvt = (e: Event) => {
      const detail = (e as CustomEvent<{ text?: string; ifEmpty?: boolean }>).detail;
      const text = detail?.text;
      if (!text) return;
      // plan-20261009-ui U2：首页示例对话点击填入——输入框已有内容时保持不动，不覆盖用户正在写的文字
      setInput((prev) => (detail?.ifEmpty && prev.trim() ? prev : text));
      taRef.current?.focus();
    };
    window.addEventListener("chatcoder:composer-prefill", onPrefillEvt);
    return () => window.removeEventListener("chatcoder:composer-prefill", onPrefillEvt);
  }, []);

  // 「聚焦输入框」请求（新建任务、切换会话、离开设置页之后派发）：此前全项目没有监听方，派发等于空转
  useEffect(() => {
    if (!followFocusRequest) return;
    const onFocusEvt = () => taRef.current?.focus();
    window.addEventListener("chatcoder:focus-composer", onFocusEvt);
    return () => window.removeEventListener("chatcoder:focus-composer", onFocusEvt);
  }, [followFocusRequest]);

  // 会话 229: 拉取可用技能（失败静默——技能菜单/分区整体不渲染）
  useEffect(() => {
    let cancelled = false;
    api.listSkills()
      .then((items) => { if (!cancelled) setSkills(items.filter((s) => s.is_active)); })
      .catch(() => { /* ignore */ });
    return () => { cancelled = true; };
  }, []);

  /** plan-282-1441（#9）：/ 菜单的「连接器」分区——已启用的 MCP（含内置与插件贡献）
   *  plan-59-286：未启用的不入菜单（避免误选），但记下数量在分区底部给"去启用"的出口。 */
  useEffect(() => {
    let cancelled = false;
    api.listMcpServers()
      .then((items) => {
        if (cancelled) return;
        setMcpServers(items.filter((m) => m.is_active));
        setDisabledMcpCount(items.filter((m) => !m.is_active).length);
      })
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

  const sendTurn = useScoped("sendTurn");
  const cancelTurn = useScoped("cancelTurn");
  const updateQueuedInput = useScoped("updateQueuedInput");
  const flushQueuedInput = useScoped("flushQueuedInput");
  const respondApproval = useScoped("respondApproval");

  const composerBrowserRefs = useScoped("composerBrowserRefs");
  const removeComposerBrowserRef = useScoped("removeComposerBrowserRef");
  const clearComposerBrowserRefs = useScoped("clearComposerBrowserRefs");
  const [browserRefPreview, setBrowserRefPreview] = useState<any | null>(null);

  const [uploading, setUploading] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  /** plan-20261025-composer：模型与思考深度合并为同一个浮层，一个开关 */
  const [showModels, setShowModels] = useState(false);
  const [showSlash, setShowSlash] = useState(false);
  const [slashIndex, setSlashIndex] = useState(0);
  // plan-20261009-ui U2：+ 浮窗（执行模式 / 目标 / 插件·技能·连接器），与其它浮窗互斥
  const [showPlus, setShowPlus] = useState(false);
  const plusBtnRef = useRef<HTMLButtonElement>(null);
  const modeChipRef = useRef<HTMLButtonElement>(null);
  // plan-75-332: 权限模式菜单（输入框左侧）——与其它浮窗互斥展开
  const [showApprovalMenu, setShowApprovalMenu] = useState(false);
  const [showAt, setShowAt] = useState(false);
  const [atIndex, setAtIndex] = useState(0);
  const [atQuery, setAtQuery] = useState("");
  const [atFiles, setAtFiles] = useState<string[]>([]);
  const [atLoading, setAtLoading] = useState(false);
  // 会话 229: 技能列表（/ 菜单技能区 + $ 补全菜单共用）
  const [skills, setSkills] = useState<SkillOut[]>([]);
  /** plan-282-1441（#9）：/ 菜单「连接器」分区数据源（已启用的 MCP） */
  const [mcpServers, setMcpServers] = useState<McpServerOut[]>([]);
  /** plan-59-286：未启用连接器数量——在分区里给一行"去启用"的出口，
   *  否则用户装了连接器却在引用菜单里看不到，也不知道原因。 */
  const [disabledMcpCount, setDisabledMcpCount] = useState(0);
  /** plan-282-1441（#9）：/ 菜单「插件」分区数据源（已安装且已启用的插件） */
  const [plugins, setPlugins] = useState<PluginMarketItem[]>([]);
  const [showSkills, setShowSkills] = useState(false);
  const [skillIndex, setSkillIndex] = useState(0);
  const [skillQuery, setSkillQuery] = useState("");
  /** 同一时刻只开一个输入框浮窗（+ / 权限 / 模型与思考深度） */
  const closeFloats = () => {
    setShowPlus(false);
    setShowApprovalMenu(false);
    setShowModels(false);
  };
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
    // S16（plan-41-197）：撤回回填恢复引用 chips（此前只恢复正文与附件，
    // 发送过的 $技能 / @文件 样式在撤回后丢失）
    if (bf.refs && bf.refs.length > 0) {
      setRefs(bf.refs.map((r) => ({
        kind: r.kind as ComposerRef["kind"],
        value: r.value,
        label: r.label,
      })));
    }
  }, [draftKey, composerBackfill]);

  /** plan-546: 草稿防抖同步——输入/附件/配置变化 300ms 后写入草稿 store；
   * 发送成功后置 skipDraftSyncRef 跳过一次，避免空状态写回导致草稿复活 */
  const skipDraftSyncRef = useRef(false);
  /** plan-568-2333：防抖窗口内「还没落盘的那一笔」。此前 cleanup 只 clearTimeout，
   *  输入后 300ms 内切会话或中途卸载，这段输入与附件就直接丢了（切回来是空的）。
   *  存 flush 闭包而不是 payload，避免在这里复刻 patchDraft 的参数类型。 */
  const pendingDraftRef = useRef<{ draftKey: string; flush: () => void } | null>(null);
  useEffect(() => {
    // 切到另一个 key（含「新建」页）时，上一个会话停在窗口里的那笔先落盘再换桶
    const prev = pendingDraftRef.current;
    if (prev && prev.draftKey !== draftKey) {
      pendingDraftRef.current = null;
      prev.flush();
    }
    if (draftKey === "new") return;
    if (skipDraftSyncRef.current) {
      skipDraftSyncRef.current = false;
      // 已发送成功：窗口里那笔属于刚发出去的内容，作废，否则卸载补发会让草稿复活
      pendingDraftRef.current = null;
      return;
    }
    const flush = () => {
      useDraftsStore.getState().patchDraft(draftKey, {
        text: input,
        attachments,
        refs,
        reasoningEffort: effort,
        ...(isHome
          ? { modelId: homeModelId, mode: composerMode, approvalMode, projectId: currentProjectId }
          : {}),
      });
    };
    pendingDraftRef.current = { draftKey, flush };
    const t = setTimeout(() => {
      pendingDraftRef.current = null;
      flush();
    }, 300);
    return () => clearTimeout(t);
  }, [draftKey, input, attachments, refs, effort, isHome, homeModelId, composerMode, approvalMode, currentProjectId]);
  // plan-568-2333：真正卸载（切到设置页、关闭侧边对话等）时补发窗口内的最后一笔。
  // 单独一个空依赖 effect：主 effect 的 cleanup 只清定时器，不清 pending，两者不抢同一份值。
  useEffect(() => () => {
    const pend = pendingDraftRef.current;
    pendingDraftRef.current = null;
    pend?.flush();
  }, []);

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

  const slashCommands = useMemo(
    () => [
      { cmd: "/clear", desc: "清空当前会话历史" },
      { cmd: "/plan", desc: "切换为规划模式" },
      { cmd: "/workflow", desc: "切换为工作流模式：复杂任务拆成多步协同完成" },
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

  /** plan-404-1892（图2）：标记「选中项变化来自键盘导航」，仅此时才把选中项滚入可视区。
   *  鼠标悬停（onMouseEnter → setXxxIndex）不得触发滚动——此前悬停到部分可见项会把列表拽动。
   *  值为对应菜单 key，滚动后立即复位。 */
  const kbScrollRef = useRef<"slash" | "skill" | "at" | null>(null);

  /** 把弹层内当前选中项（.active）滚入可视区。
   *  不用 scrollIntoView：它会连带滚动祖先容器（消息流会跟着动），
   *  这里只改弹层自己的 scrollTop，副作用最小。
   *  plan-404-1892：改用 rect 相对位移计算，不再依赖「弹层是子项 offsetParent」的假设。 */
  const scrollActiveIntoView = (menu: HTMLDivElement | null) => {
    if (!menu) return;
    const el = menu.querySelector<HTMLElement>("button.active");
    if (!el) return;
    const mr = menu.getBoundingClientRect();
    const er = el.getBoundingClientRect();
    if (er.top < mr.top) menu.scrollTop -= mr.top - er.top;
    else if (er.bottom > mr.bottom) menu.scrollTop += er.bottom - mr.bottom;
  };

  // 键盘 ↑/↓ 切换后选中项必须滚入可视区（仅键盘导航触发，见 kbScrollRef）
  useEffect(() => {
    if (kbScrollRef.current !== "slash") return;
    kbScrollRef.current = null;
    scrollActiveIntoView(slashMenuRef.current);
  }, [slashIndex]);
  useEffect(() => {
    if (kbScrollRef.current !== "skill") return;
    kbScrollRef.current = null;
    scrollActiveIntoView(skillMenuRef.current);
  }, [skillIndex]);
  useEffect(() => {
    if (kbScrollRef.current !== "at") return;
    kbScrollRef.current = null;
    scrollActiveIntoView(atMenuRef.current);
  }, [atIndex]);

  // 列表内容变化（输入过滤 / 菜单开关）时回到顶部：旧滚动位置已不对应同一批项
  useEffect(() => { if (slashMenuRef.current) slashMenuRef.current.scrollTop = 0; },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [showSlash, input]);
  useEffect(() => { if (skillMenuRef.current) skillMenuRef.current.scrollTop = 0; },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [showSkills, skillQuery]);
  useEffect(() => { if (atMenuRef.current) atMenuRef.current.scrollTop = 0; },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [showAt, atQuery]);

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
    // 宽度过窄时不能测：侧边对话首次打开时面板尚未完成布局，textarea 只有二十余像素宽，
    // 占位文字因此被折成多行、scrollHeight 随之变大；写进去的固定高度之后不会自己收回
    // （表现为「首次打开输入框很高，点一下页面才恢复正常」）。等宽度就绪再测。
    if (el.clientWidth < 80) {
      if (retry < 60) requestAnimationFrame(() => resizeTextarea(el, retry + 1));
      return;
    }
    const prev = el.style.height;
    el.style.height = "auto";
    const clamped = Math.max(36, Math.min(el.scrollHeight, 400));
    const next = `${clamped}px`;
    // 高度没变就不重复赋值：拖拽期每次写入都会让输入框重新布局一次，
    // 进而带动整个中间列抖动（消息流滚动位置随之漂移）。
    el.style.height = prev === next ? prev : next;
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
    // 运动中跳过重测（判定统一走 PerfBus：窗口运动 / 拖分隔条 / 面板过渡）
    const inMotion = () => isBusy();
    const remeasure = () => {
      // 运动期跳过：resizeTextarea = height:"auto" 写入 + scrollHeight 读取，
      // 是强制同步布局；拖窗口/拖面板每帧跑一次会直接拖垮帧预算（鼠标失灵来源之一）。
      if (inMotion()) return;
      if (taRef.current) resizeTextarea(taRef.current);
    };
    const onResize = () => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(remeasure);
    };
    window.addEventListener("resize", onResize);
    // 运动结束补测一次（窗口 motion 解除 / 分隔条松手）：换行宽度在拖拽中已变，
    // 输入框高度须按终宽重测。顺带修掉旧缺陷：拖面板不产生 window resize，
    // 输入框高度此前从不重测，折行变化后停留旧值。
    const onMotionEnd = (e: Event) => {
      if ((e as CustomEvent<{ active?: boolean }>).detail?.active === false) remeasure();
    };
    // RFL-6（S6）：注册进唯一收敛序列——order 70 输入框 remeasure。
    const offReconcile = registerReconcileTask("composer-remeasure", RECONCILE_ORDER.composerRemeasure,
      "输入框重测", remeasure);
    const onPointerUp = () => { if (!inMotion()) remeasure(); };
    window.addEventListener("chatcoder:window-motion", onMotionEnd);
    window.addEventListener("pointerup", onPointerUp, true);
    return () => {
      offReconcile();
      cancelAnimationFrame(raf);
      window.removeEventListener("resize", onResize);
      window.removeEventListener("chatcoder:window-motion", onMotionEnd);
      window.removeEventListener("pointerup", onPointerUp, true);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 输入框自身宽度变化时重测高度：侧边对话首次布局完成（宽度从 0 / 二十余像素 → 正常值）、
  // 标签切入动画、拖动分隔条，都属于「不产生 window resize 的宽度变化」，
  // 之前只能等 pointerup 碰巧纠正。只认宽度：写 height 本身也会触发 ResizeObserver，
  // 按宽度过滤可避免自激循环。
  useEffect(() => {
    const el = taRef.current;
    if (!el) return;
    let raf = 0;
    let lastW = -1;
    let pending = false;
    const measure = () => {
      // 运动期（拖分隔条 / 面板展开折叠）必须跳过，与既有 remeasure 同一口径：
      // resizeTextarea 先写 height:auto 再读 scrollHeight（强制同步布局），
      // 每帧执行会让输入框高度反复塌陷又写回 —— 中间列容器高度随之抖动，
      // 消息流的滚动锚点（capture → 等宽度稳定 → 像素校正）就在半程布局上执行，
      // 表现为「展开 / 折叠 / 拖拉左右面板后消息流位置不再保持」。
      if (isBusy()) { pending = true; return; }
      pending = false;
      if (taRef.current) resizeTextarea(taRef.current);
    };
    const ro = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width ?? 0;
      if (Math.abs(w - lastW) < 1) return;
      lastW = w;
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(measure);
    });
    ro.observe(el);
    // 运动结束补测一次：首次打开侧边对话时宽度变化发生在面板动画期间，
    // 当时只记了 pending；不补测就会让高度停在旧值（问题回到原点）。
    const onMotionEnd = (e: Event) => {
      if ((e as CustomEvent<{ active?: boolean }>).detail?.active !== false || !pending) return;
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(measure);
    };
    window.addEventListener("chatcoder:window-motion", onMotionEnd);
    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
      window.removeEventListener("chatcoder:window-motion", onMotionEnd);
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
        pendingApproval: null, approvalQueue: [], pendingPlan: null, reviewedFiles: {},
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
    if (cmd === "/workflow") {
      // plan-workflow-collab：经 pickMode 进入，目标模式激活时一并退出（与菜单选择一致）
      pickMode("workflow");
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

  /** plan-20261009-ui U2：已移除语音输入（功能未实现，按钮误导）与「选择本地目录」直建项目——
   *  新建项目统一走项目弹窗（工作区胶囊 → 新建项目）。 */

  const changeModel = (modelId: number) => {
    if (!isHome && currentSessionId != null) {
      // plan-166-767: 切换模型等待后端落库成功后再更新本地 state；失败回滚提示。
      // 配合发送请求携带 model_id（权威值），消除「前端已切、后端未变」竞态。
      setShowModels(false);
      void api.updateSession(currentSessionId, { model_id: modelId })
        .then(() => {
          useChatStore.setState((s) => ({
            ...patchSession(s, currentSessionId, (x) => ({ ...x, model_id: modelId })),
            lastModelId: modelId,
          }));
          // plan-20261017-ux-overhaul：切换当下不再写分隔线（发送时由后端按需写入），无需再拉消息
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
    // plan-20261009-ui：滑块可以连续拖动，每越过一档就调用一次——这里不关浮窗，
    // 用户拖完点外部 / 按 Esc 自然收起（此前选一档就关，无法连续调整）
    setEffort(e);
    useChatStore.setState({ lastReasoningEffort: e });
    persistLastReasoning(e);
  };

  /** plan-75-332: 切换执行模式（只读 / 计划 / 智能体）。旧值先归一化再落库，避免写入脏值。 */
  const setMode = (mode: string) => {
    const next = normalizeDraftMode(mode);
    const prev = composerMode;
    setComposerMode(next);
    setShowPlus(false);
    if (currentSessionId != null) {
      // plan-75-332 R2: 保存失败必须回滚并提示——否则界面显示与实际落库不一致，
      // 用户以为已切换、重进会话又变回去（权限模式同样处理）。
      void api.updateSession(currentSessionId, { permission_mode: next }).catch((e) => {
        setComposerMode(prev);
        useChatStore.setState((s) => ({
          ...patchSession(s, currentSessionId, (x) => ({ ...x, permission_mode: prev })),
          error: `执行模式保存失败：${String(e)}`,
        }));
      });
      useChatStore.setState((s) => patchSession(s, currentSessionId, (x) => ({ ...x, permission_mode: next })));
    }
  };

  /** plan-128-518：模式菜单的统一入口。
   *  选「目标」= 切到智能体能力边界并“待命”，下一条发送的消息即目标（无二次弹窗）；
   *  选其它模式 = 退出目标（目标激活中则一并结束，避免续跑轮在受限模式下空转）。 */
  const pickMode = (name: string) => {
    if (name === GOAL_MODE) {
      if (composerMode !== "agent") setMode("agent");
      setGoalArmed(true);
      setShowPlus(false);
      window.setTimeout(() => taRef.current?.focus(), 0);
      return;
    }
    if (goalOn) {
      setGoalArmed(false);
      if (goalStatus === "active") cancelGoal();
    }
    setMode(name);
    // 菜单关闭后焦点交还输入框，Shift+Tab 等输入框内的快捷键紧接着就能用
    window.setTimeout(() => taRef.current?.focus(), 0);
  };

  /** plan-75-332: 切换权限模式（询问审批 / 自动审批 / 完全访问）。
   *  会话内立即 PATCH 持久化；首页只改本地状态，随创建会话一次落准。 */
  const changeApprovalMode = (mode: ApprovalMode) => {
    const prev = approvalMode;
    setApprovalMode(mode);
    setShowApprovalMenu(false);
    if (currentSessionId != null) {
      // plan-75-332 R2: 保存失败回滚（见 setMode 注释）——权限模式决定后续操作是否弹审批，
      // 显示与落库不一致会让用户误判拦截行为。
      void api.updateSession(currentSessionId, { approval_mode: mode }).catch((e) => {
        setApprovalMode(prev);
        useChatStore.setState((s) => ({
          ...patchSession(s, currentSessionId, (x) => ({ ...x, approval_mode: prev })),
          error: `权限模式保存失败：${String(e)}`,
        }));
      });
      useChatStore.setState((s) => patchSession(s, currentSessionId, (x) => ({ ...x, approval_mode: mode })));
    }
  };


  const canSend = Boolean(input.trim() || attachments.length > 0 || composerBrowserRefs.length > 0);

  const handleCancel = () => {
    // 停止 = 中断本会话的一切执行：运行中的主代理（连带子代理）、后台子代理、运行中的工作流（暂停，保留进度）
    if (isRunning) void cancelTurn();
    void stopSessionExecution();
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
    // plan-128-518：目标模式待命中——这条消息的正文就是目标；目标不是后端执行模式，能力边界按智能体处理
    const goalText0 = goalArmed ? input.trim() : "";
    const mode = composerMode === GOAL_MODE ? "agent" : composerMode;

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
        // plan-75-332: 权限模式（approval_mode）同样随创建一次落准
        const sessionId = await useChatStore.getState().createSession(pId, input.trim().slice(0, 30) || "新对话", {
          model_id: usedModelId,
          permission_mode: mode,
          approval_mode: approvalMode,
          goal_text: goalText0 || homeGoalText,
        });
        if (sessionId == null) return;
        useDraftsStore.getState().patchDraft(`s${sessionId}`, { reasoningEffort: sendEffort ?? null });
        if (usedModelId != null) useChatStore.setState({ lastModelId: usedModelId });
        // plan-75-332: agent（智能体模式）即原来的 default，透传 null 让引擎走默认全量工具；
        // 其余模式（含自定义模式名）必须透传，否则后端按默认解析出全量工具集，
        // 出现"选了受限模式但模型仍看到全量工具"的错位。
    const sendMode = mode === "agent" ? null : mode;
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
        setGoalArmed(false);
        onStarted?.();
      } catch {
        /* ignore */
      } finally {
        setSending(false);
      }
      return;
    }
    // 运行中发送 → sendTurn 内部入队，turn 完成后自动续发
    // plan-75-332: 同首页口径——agent 透传 null（引擎默认全量工具），其余模式名一律透传。
    const sendMode = mode === "agent" ? null : mode;
    // plan-128-518：目标待命——先把正文设为会话目标（写库 + 广播），再发送，引擎该 turn 起即按目标模式下发工具与续跑。
    if (goalText0 && currentSessionId != null) {
      try {
        applyGoalOut(await api.setSessionGoal(currentSessionId, goalText0));
      } catch {
        useChatStore.setState({ error: "目标设定失败，请重试" });
        setSending(false);
        return;
      }
    }
    await sendTurn(content, attachmentPayload, sendEffort, sendMode, sessionModelId, refPayload);
    if (goalText0) setGoalArmed(false);
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

  /** plan-75-332 R2 / plan-20261009-ui U2：执行模式数据源——内置模式按「智能体 → 计划 → 只读」排序，
   *  自定义模式排后；API 未就绪时用静态三项兜底。 */
  const modeMenuProfiles = useMemo(() => {
    const base: PermissionProfileOut[] = permProfiles.length > 0
      ? permProfiles
      : ([{ name: "agent" }, { name: "workflow" }, { name: "plan" }, { name: "readonly" }] as PermissionProfileOut[]);
    const rank = (n: string) => {
      const i = BUILTIN_MODE_ORDER.indexOf(n);
      return i < 0 ? BUILTIN_MODE_ORDER.length : i;
    };
    return [...base].sort((a, b) => rank(a.name) - rank(b.name));
  }, [permProfiles]);

  /** Shift+Tab 将切到的下一个内置模式（目标锁定时循环被锁住，此时不给提示） */
  const nextBuiltinMode = goalOn
    ? null
    : BUILTIN_MODE_ORDER[(BUILTIN_MODE_ORDER.indexOf(composerMode) + 1) % BUILTIN_MODE_ORDER.length];
  /** + 浮窗的模式项：内置模式取 modeMeta（图标 + 短名 + 一行说明），自定义模式取配置的名称与描述；
   *  ⇧ Tab 提示挂在下一个会被切到的模式上（此前固定挂在「计划」上，与实际切换的目标不一致） */
  const modeOptions: ModeOption[] = useMemo(() => {
    const base = modeMenuProfiles.map((p) => {
      const meta = MODE_META[p.name];
      return meta
        ? { name: p.name, label: t(meta.shortKey), desc: t(meta.descKey), icon: meta.icon(16), kbd: p.name === nextBuiltinMode ? "⇧ Tab" : undefined }
        : { name: p.name, label: p.display_name || p.name, desc: p.description || "自定义执行模式", icon: customModeIcon(16) };
    });
    // plan-128-518：“目标”排在内置三项之后、自定义模式之前
    const gm = MODE_META[GOAL_MODE];
    const goalOpt: ModeOption = { name: GOAL_MODE, label: t(gm.shortKey), desc: t(gm.descKey), icon: gm.icon(16) };
    const at = base.findIndex((o) => !BUILTIN_MODE_ORDER.includes(o.name));
    return at < 0 ? [...base, goalOpt] : [...base.slice(0, at), goalOpt, ...base.slice(at)];
  }, [modeMenuProfiles, t, nextBuiltinMode]);
  /** 菜单中的“当前项”：目标生效时高亮“目标”，否则是真实执行模式 */
  const shownMode = goalOn ? GOAL_MODE : composerMode;
  const modeMeta = MODE_META[shownMode];
  const modeShort = modeMeta
    ? t(modeMeta.shortKey)
    : (permProfiles.find((x) => x.name === shownMode)?.display_name ?? shownMode);
  const modeTone = modeMeta?.tone ?? "neutral";

  /** Shift+Tab：在内置执行模式间循环（智能体 → 工作流 → 计划 → 只读；输入框聚焦时生效，菜单打开时同样生效） */
  const cycleMode = () => {
    // plan-128-518：目标模式下锁定模式循环（续跑中途被切走会让目标空转），需要退出请在菜单里选其它模式
    if (goalOn) return;
    const order = BUILTIN_MODE_ORDER;
    const i = order.indexOf(composerMode);
    setMode(order[(i + 1) % order.length]);
    // 从 + 浮窗触发时焦点停在菜单按钮上：切完交还输入框，连续按 Shift+Tab 不必再点一次
    window.setTimeout(() => taRef.current?.focus(), 0);
  };
  // cycleMode 每次渲染都是新函数：经 ref 取最新闭包，供下方菜单打开期间的监听使用
  const cycleModeRef = useRef(cycleMode);
  cycleModeRef.current = cycleMode;
  // + 浮窗打开时焦点在菜单按钮 / 子浮窗搜索框上，输入框的 onKeyDown 收不到 Shift+Tab；
  // 菜单里标着的「⇧ Tab」提示在这里补实，行为与输入框内一致
  useEffect(() => {
    if (!showPlus) return;
    const onKey = (e: KeyboardEvent) => {
      // 输入框已处理过（preventDefault）的不再重复循环
      if (e.key !== "Tab" || !e.shiftKey || e.defaultPrevented) return;
      e.preventDefault();
      cycleModeRef.current();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [showPlus]);

  /** + 浮窗「插件 / 技能 / 连接器」树的数据（只含已启用项，与 / 菜单同源） */
  const extItems = useMemo<Record<ExtKind, ExtItem[]>>(() => {
    const httpsOnly = (u: unknown) => (typeof u === "string" && u.startsWith("https://") ? u : undefined);
    const marketIcon = (meta?: Record<string, unknown> | null) => httpsOnly((meta || {}).market_icon_url);
    return {
      plugin: plugins.map((p) => ({ key: p.name, name: p.displayName || p.name, desc: p.descriptionZh || p.description || p.category || "", iconUrl: httpsOnly(p.logo) })),
      skill: skills.map((sk) => ({ key: sk.name, name: sk.display_name || sk.name, desc: sk.description || "", iconUrl: marketIcon(sk.meta) })),
      connector: mcpServers.map((m) => ({ key: m.name, name: m.display_name || m.name, desc: m.description || "", iconUrl: marketIcon(m.meta) })),
    };
  }, [plugins, skills, mcpServers]);

  /** 从拓展树插入引用 chip（与 / 菜单插入同一语义：发送时追加「使用插件/技能/连接器」引用行） */
  const pickExt = (kind: ExtKind, item: ExtItem) => {
    const refKind: ComposerRef["kind"] = kind === "connector" ? "mcp" : kind;
    setRefs((prev) => (prev.some((r) => r.kind === refKind && r.value === item.key)
      ? prev
      : [...prev, { kind: refKind, value: item.key, label: item.name }]));
    window.setTimeout(() => taRef.current?.focus(), 0);
  };

  /** 排队项「编辑」：移出队列并回填输入框（已有正文时追加在后面，不覆盖用户正在写的内容） */
  const editQueued = (q: { id: string; content: string; attachments?: Record<string, unknown>[]; refs?: ComposerRefOut[] }) => {
    updateQueuedInput(q.id, null);
    // plan-128-518：排队项的 content 里带着拼接的「使用技能：…」引用行，直接回填会变成原始文字。
    // 剪掉引用行后把结构化 refs（旧队列项无 refs 时从文本解析兜底）恢复成输入框 chips。
    const parsed = parseRefLines(q.content);
    const restored: ComposerRef[] = ((q.refs && q.refs.length > 0 ? q.refs : parsed.refs) as ComposerRef[])
      .map((r) => ({ kind: r.kind, value: r.value, label: r.label }));
    const body = parsed.text;
    setInput((cur) => (cur.trim() ? (body ? `${cur}\n${body}` : cur) : body));
    if (restored.length > 0) {
      setRefs((prev) => [...prev, ...restored.filter((r) => !prev.some((p) => p.kind === r.kind && p.value === r.value))]);
    }
    const atts = (q.attachments ?? []) as unknown as AttachmentInfo[];
    if (atts.length > 0) setAttachments((prev) => [...prev, ...atts]);
    window.setTimeout(() => taRef.current?.focus(), 0);
  };

  /** 当前会话所在项目（会话态工作区胶囊只读展示） */
  const sessionProject = projects.find((p) => p.id === (currentSession?.project_id ?? currentProjectId)) ?? null;

  /** 会话隔离修复：提问 / 审批卡只渲染**当前会话**的队首（pendingApproval 随会话切片存取）。
   *  此前在「当前会话无队首」时回退到跨会话索引里最早的一条，导致 A 会话的提问卡出现在 B 会话，
   *  且作答时回执按提交方会话广播，原会话的索引条目永远不消失、卡片反复回到第 1 题。
   *  其他会话的等待统一由侧栏等待标记提示，用户主动切过去处理。 */
  const effectivePendingApproval = pendingApproval;

  /** 来源子代理：审批卡要根据来源反查子代理标题。
   *  plan-549-2281：改按 agent_id + subagent_title 取，不再查子会话桶——并行执行已统一到子代理。 */
  const subagentMeta = useChatStore((s) => s.subagentMeta);
  /** 审批来源：只有「子代理发起」才需要标注（detail 的 is_subagent 由后端下发）。
   *  标题优先取 detail.subagent_title（后端随审批下发），缺失时回退本会话 subagentMeta 的名称；
   *  两者都拿不到就只留 #编号——猜一个名字比不显示更容易误导用户。 */
  const pendingSource = useMemo(() => {
    const detail = effectivePendingApproval?.detail;
    if (!detail || detail.is_subagent !== true) return null;
    const agentId = typeof detail.agent_id === "number" && detail.agent_id > 0 ? detail.agent_id : 0;
    const title = String(detail.subagent_title ?? "") || (agentId > 0 ? subagentMeta[agentId]?.name ?? "" : "");
    if (agentId === 0 && !title) return null;
    return { kind: "subagent" as const, agentId, title };
  }, [effectivePendingApproval, subagentMeta]);

  // 是否处于 AI 结构化提问阶段（直接替换输入框主体）
  const isQuestionMode = !isHome && effectivePendingApproval?.detail?.kind === "question";

  // plan-75-332: 是否处于工具审批阶段——与提问同构：审批卡直接替换输入框主体，
  // 不再用全屏遮罩弹窗（遮罩会把用户的视线与操作位置从输入区移开）。
  const isApprovalMode = !isHome && effectivePendingApproval != null && !isQuestionMode;

  /** 会话 229: 输入框 placeholder（textarea 文字透明后由标签层渲染，保持视觉一致） */
  const placeholderText = goalArmed
    ? t("composer.goal_placeholder")
    : isHome
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
      {/* plan-20261009-ui U2：运行中继续发送的消息排在输入卡上方的独立托盘（原先挤在卡内顶部） */}
      {!isHome && !isQuestionMode && !isApprovalMode && (
        <QueueTray
          items={queuedInputs}
          onFlush={(id) => void flushQueuedInput(id)}
          onRemove={(id) => updateQueuedInput(id, null)}
          onEdit={editQueued}
        />
      )}

      {/* plan-282-1434（B1）：原「计划已就绪」横幅（.composer-plan-bar）已删除。
          确认/取消动作移入计划卡内部下方居中（见 PlanCard 的 footer）——
          此前确认入口在输入框上方、计划内容在消息流里，两者分离，视线要来回跳；
          且该横幅与卡片"待确认"徽标语义重复。 */}

      {/* plan-75-332: 需要审批时把输入框主体替换为 ApprovalCard（与提问卡同构）——
          旧的全屏遮罩弹窗（.approval-overlay）已删除。 */}
      {isQuestionMode ? (
        <QuestionWizardBox
          approvalId={effectivePendingApproval!.approvalId}
          detail={effectivePendingApproval!.detail}
          onCancel={() => respondApproval(effectivePendingApproval!.approvalId, false)}
          onSubmit={(answers) => respondApproval(effectivePendingApproval!.approvalId, true, false, answers)}
        />
      ) : isApprovalMode ? (
        <ApprovalCard
          approvalId={effectivePendingApproval!.approvalId}
          detail={effectivePendingApproval!.detail}
          onDeny={() => respondApproval(effectivePendingApproval!.approvalId, false)}
          onAllowOnce={() => respondApproval(effectivePendingApproval!.approvalId, true)}
          onAlwaysAllow={(scope) =>
            respondApproval(effectivePendingApproval!.approvalId, true, true, undefined, scope)}
          queuedCount={Math.max(0, approvalQueueLength - 1)}
          source={pendingSource}
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
          {/* plan-128-518：目标待命——已选「目标」模式但还没发送，提示“下一条消息就是目标”，可点 × 退出 */}
          {goalArmed && !goalPillVisible && (
            <div className="goal-pill">
              <span className="goal-pill-label">目标</span>
              <span className="goal-pill-text">下一条消息将作为目标，自动推进直到达成</span>
              <button className="goal-pill-btn" onClick={() => setGoalArmed(false)} title="退出目标模式" type="button">
                <IconX size={11} />
              </button>
            </div>
          )}
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
                    {r.kind === "file" ? <IconFileText size={12} />
                      : r.kind === "mcp" ? <IconPlug size={12} />
                      : r.kind === "plugin" ? <IconPuzzle size={12} />
                      : <IconSparkles size={12} />}
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
              // plan-20261009-ui U2：Shift+Tab 在「智能体 → 计划 → 只读」间循环（+ 浮窗内有提示）
              if (e.key === "Tab" && e.shiftKey && !slashVisible && !showSkills && !showAt) {
                e.preventDefault();
                cycleMode();
                return;
              }
              // plan-238-1210 (A2): 文本中不再有 @/$ 引用片段（已改为 chips），
              // 原"整词删除拦截"随之移除——Backspace/Delete 回归原生行为。
              if (slashVisible) {
                if (e.key === "ArrowDown") {
                  e.preventDefault();
                  setSlashIndex((i) => (i + 1) % slashItems.length);
                  kbScrollRef.current = "slash";
                  return;
                }
                if (e.key === "ArrowUp") {
                  e.preventDefault();
                  setSlashIndex((i) => (i - 1 + slashItems.length) % slashItems.length);
                  kbScrollRef.current = "slash";
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
                  kbScrollRef.current = "skill";
                  return;
                }
                if (e.key === "ArrowUp") {
                  e.preventDefault();
                  setSkillIndex((i) => (i - 1 + Math.max(1, n)) % Math.max(1, n));
                  kbScrollRef.current = "skill";
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
                  kbScrollRef.current = "at";
                  return;
                }
                if (e.key === "ArrowUp") {
                  e.preventDefault();
                  setAtIndex((i) => (i - 1 + filteredAtFiles.length) % filteredAtFiles.length);
                  kbScrollRef.current = "at";
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
          {/* plan-20261009-ui U2：工具条重做——
              左：+（模式 / 目标 / 插件·技能·连接器）· 附件 · 执行模式胶囊 · 权限模式胶囊；
              右：上下文圆环（会话）· 模型与思考深度（plan-20261025-composer 合并为一枚无图标文字触发器）·
              发送/停止/排队（同一按钮形变）。已移除语音按钮（功能未实现）。 */}
          <div className="composer-toolbar cc-toolbar" ref={setToolbarEl}>
            <div className="composer-tools-left cc-tools-left">
              <div className="cc-anchor">
                <Tooltip content="模式、目标与拓展" side="top" disabled={showPlus}>
                  <button
                    ref={plusBtnRef}
                    type="button"
                    className={"cc-plus" + (showPlus ? " is-open" : "")}
                    aria-haspopup="menu"
                    aria-expanded={showPlus}
                    aria-label="模式、目标与拓展"
                    onClick={() => { const next = !showPlus; closeFloats(); setShowPlus(next); }}
                  >
                    <IconPlusX open={showPlus} size={16} />
                  </button>
                </Tooltip>
                <PlusMenu
                  open={showPlus}
                  onClose={() => setShowPlus(false)}
                  anchorRefs={[plusBtnRef, modeChipRef]}
                  modes={modeOptions}
                  mode={shownMode}
                  onModeChange={pickMode}
                  exts={extItems}
                  onPickExt={pickExt}
                />
              </div>
              <Tooltip content={t("composer.attach_tip")} side="top">
                <button type="button" className="cc-icon-btn" aria-label={t("composer.attach_tip")} onClick={() => fileRef.current?.click()}>
                  <IconPaperclip size={16} />
                </button>
              </Tooltip>
              <Tooltip content={t("composer.mode_title") + "：" + modeShort + " · Shift+Tab 切换"} side="top" disabled={showPlus}>
                <button
                  ref={modeChipRef}
                  type="button"
                  className={"cc-chip cc-mode-chip tone-" + modeTone + (showPlus ? " is-open" : "")}
                  aria-label={t("composer.mode_title") + "：" + modeShort}
                  onClick={() => { const next = !showPlus; closeFloats(); setShowPlus(next); }}
                >
                  {modeMeta ? modeMeta.icon(14) : customModeIcon(14)}
                  <span className="cc-chip-label">{modeShort}</span>
                </button>
              </Tooltip>
              <ApprovalPicker
                open={showApprovalMenu}
                onToggle={() => { const next = !showApprovalMenu; closeFloats(); setShowApprovalMenu(next); }}
                onClose={() => setShowApprovalMenu(false)}
                value={approvalMode}
                onChange={changeApprovalMode}
              />
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
            <div className="composer-right cc-tools-right">
              {!isHome && (
                <div className="composer-usage">
                  <UsageRing pct={usagePct} usage={usage} compacting={isCompacting} />
                </div>
              )}
              <ModelEffortPicker
                models={models}
                value={sessionModelId}
                onChange={changeModel}
                open={showModels}
                onToggle={() => { const next = !showModels; closeFloats(); setShowModels(next); }}
                onClose={() => setShowModels(false)}
                effort={activeEffort}
                onEffortChange={changeReasoning}
              />
              {/* plan-547 → plan-20261009-ui U2：发送 / 停止 / 排队为同一个按钮的三种形态（图标弹性形变，位置不跳）——
                  空闲：发送；执行中（含后台工作流 / 子代理）且输入为空：停止；主代理轮次运行中有输入：加入排队；仅后台执行时有输入：直接发送。 */}
              {(() => {
                // 执行中：输入为空 → 停止（中断主代理、子代理与工作流）；主代理轮次运行中有输入 → 排队；仅后台执行时有输入 → 直接发送
                const sendState: "send" | "stop" | "queue" = !isHome && busy ? (canSend ? (isRunning ? "queue" : "send") : "stop") : "send";
                const tip = sendState === "stop" ? "停止：中断主代理、子代理与工作流" : sendState === "queue" ? "加入排队（当前任务结束后发送）" : "发送 (Enter)";
                return (
                  <Tooltip content={tip} side="top">
                    <button
                      type="button"
                      className={"cc-send is-" + sendState}
                      disabled={sendState === "send" && !canSend}
                      aria-label={tip}
                      onClick={() => (sendState === "stop" ? handleCancel() : void handleSend())}
                    >
                      <IconSendStop mode={sendState} size={16} />
                    </button>
                  </Tooltip>
                );
              })()}
            </div>
          </div>
        </div>
      )}

      {/* plan-20261009-ui U2：输入卡左下——当前工作区（首页可选；会话开始后只读展示） */}
      {!isQuestionMode && !isApprovalMode && (
        <div className="cc-under">
          <WorkspaceChip
            projects={activeProjects}
            currentId={isHome ? activeProjectId ?? null : sessionProject?.id ?? null}
            locked={!isHome}
            branch={!isHome && sessionProject?.is_worktree ? sessionProject.worktree_branch ?? null : null}
            onSelect={(id) => useChatStore.setState({ currentProjectId: id })}
          />
        </div>
      )}

      {/* plan-282-1421（图4）：原 .composer-drag-overlay 外挂虚线遮罩已移除，
          拖拽提示改由 .composer-main.is-drag-over 的边框 + 卡内 .composer-drop-hint 承载。 */}

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
              onMouseEnter={() => { kbScrollRef.current = null; setSlashIndex(idx); }}
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
                onMouseEnter={() => { kbScrollRef.current = null; setSlashIndex(idx); }}
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
                onMouseEnter={() => { kbScrollRef.current = null; setSlashIndex(idx); }}
                onClick={() => insertConnector(m.name, m.display_name || m.name)}
              >
                <IconPlug size={12} />
                <strong>{m.display_name || m.name}</strong>
                <span>{m.description || m.name}</span>
              </button>
            );
          })}
          {/* plan-59-286：有未启用连接器时给一行出口——
              否则用户装了连接器却在引用菜单里看不到，也无从知道原因。 */}
          {disabledMcpCount > 0 && (
            <button
              className="composer-menu-hint"
              onMouseEnter={() => { kbScrollRef.current = null; setSlashIndex(-1); }}
              onClick={() => {
                setShowSlash(false);
                window.dispatchEvent(new CustomEvent("chatcoder:open-settings", {
                  detail: { tab: "extensions" },
                }));
              }}
            >
              <IconPlug size={12} />
              <strong>另有 {disabledMcpCount} 个连接器未启用</strong>
              <span>前往扩展管理启用</span>
            </button>
          )}
          {/* plan-282-1441（#9）：插件分区（已安装且已启用的插件） */}
          {filteredSlashPlugins.length > 0 && <div className="composer-menu-title">插件</div>}
          {filteredSlashPlugins.map((p, j) => {
            const idx = filteredSlash.length + filteredSlashSkills.length + filteredSlashMcp.length + j;
            return (
              <button
                key={`pl-${p.name}`}
                className={idx === slashIndex ? "active" : ""}
                onMouseEnter={() => { kbScrollRef.current = null; setSlashIndex(idx); }}
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
              onMouseEnter={() => { kbScrollRef.current = null; setSkillIndex(idx); }}
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
                onMouseEnter={() => { kbScrollRef.current = null; setAtIndex(idx); }}
                onClick={() => pickAtFile(f)}
              >
                <span>{f}</span>
              </button>
            ))}
        </div>
      )}

      {/* plan-75-332: 原「普通工具审批弹窗」（.approval-overlay 遮罩 + 四按钮卡片）已删除——
          审批改为 ApprovalCard 直接替换输入框主体（见上方渲染分支），
          四个动作（拒绝 / 允许一次 / 本会话始终允许 / 全局始终允许）全部保留。 */}
    </div>
  );
}

/* plan-75-332: 原审批参数视图（APPROVAL_FIELD_LABELS / clipApprovalText / safeJsonText /
 * formatApprovalValue / approvalArgRows / ApprovalArgsView）已随旧遮罩弹窗一并移除——
 * 参数渲染逻辑迁至 ApprovalCard.tsx（与卡片本身放在一起，避免跨文件找同一件事）。 */

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
 *  - 尺寸迭代：26 → 20px（与其他图标同视觉重量）→ 10px（用户嫌大，缩半）
 *    → **16px（10px 看不清，回调；描边同步 1.5px）**。
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
          size={16}
          strokeWidth={1.5}
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
/** v36 紧急修复：模型可能把选择题选项写成 [{label, description}] 对象数组（工具 schema 要求 string[]）。
 *  对象被当作 React 子元素渲染会直接抛 React #31「Objects are not valid as a React child」整页崩溃。
 *  这里统一压成展示文本：label — description（缺 label 用 description，其余 JSON 化兜底）。 */
function normalizeQuestionOption(raw: unknown): string {
  if (typeof raw === "string") return raw;
  if (raw && typeof raw === "object" && !Array.isArray(raw)) {
    const rec = raw as Record<string, unknown>;
    const pick = (keys: string[]): string => {
      for (const k of keys) {
        const v = rec[k];
        if (typeof v === "string" && v.trim()) return v.trim();
      }
      return "";
    };
    const label = pick(["label", "text", "title", "value", "name"]);
    const desc = pick(["description", "desc", "detail", "hint"]);
    if (label && desc) return `${label} — ${desc}`;
    if (label || desc) return label || desc;
    try {
      return JSON.stringify(raw);
    } catch {
      return String(raw);
    }
  }
  return String(raw ?? "");
}

/** 原生支持 field-sizing 时，自定义回答的高度完全由内容驱动（见 .question-wizard-custom-input），
 *  输入、切题清空、返回上一题恢复多行答案都由浏览器自动撑高 / 回落，不再依赖 JS 测高的执行时机。
 *  Electron 44（Chromium 142）支持；不支持的环境仍走下面的 JS 兜底。 */
const supportsFieldSizing =
  typeof CSS !== "undefined" && typeof CSS.supports === "function" && CSS.supports("field-sizing", "content");

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
  const questionDraft = useScoped("questionDraft");
  const setQuestionDraft = useScoped("setQuestionDraft");

  const draft = questionDraft && questionDraft.approvalId === approvalId ? questionDraft : null;
  const stepIndex = draft?.stepIndex ?? 0;
  const answers = draft?.answers ?? {};
  const [customText, setCustomText] = useState("");
  const customInputRef = useRef<HTMLTextAreaElement>(null);
  const wizardRef = useRef<HTMLDivElement>(null);
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
  // v36 紧急修复：选项统一归一化为文本——模型曾把 options 写成 [{label, description}] 对象，
  // 直接渲染对象会抛 React #31 把整个应用打成错误页。
  const currentOptions = Array.isArray(currentQ.options)
    ? (currentQ.options as unknown[]).map(normalizeQuestionOption)
    : [];
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

  /** 自定义回答高度随内容自适应（上限见 CSS 的 max-height）。
   *  原生支持 field-sizing（见 supportsFieldSizing）时高度由浏览器按内容算，这里直接跳过；
   *  兜底分支按「当前文本 + 题号」重算：此前内联 height 只在 onChange 写入，
   *  切到下一题文本被清空后高度仍是上一题的多行值（用户反馈「换题不回落为 1 行」）。 */
  useLayoutEffect(() => {
    if (supportsFieldSizing) return;
    const el = customInputRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight}px`;
  }, [customText, stepIndex, allowCustom]);

  /** 键盘高亮项下标：0..选项数-1 为选项，紧随其后的一行为自定义回答（无自定义行时不会落到它上） */
  const customIdx = currentOptions.length;
  const optionCount = currentOptions.length + (allowCustom ? 1 : 0);
  /** 切题时的高亮落点：该题已有答案则落在答案上（回到上一题可直接回车确认原答案），否则第一项 */
  const initialKbIndex = (): number => {
    const i = currentOptions.indexOf(currentAnswer);
    if (i >= 0) return i;
    return currentAnswer && allowCustom ? customIdx : 0;
  };
  /** 高亮状态记下「在哪一题、停在哪一项」：题号一变就回到该题的默认落点。
   *  这里用派生而不是 effect 重置——切题（如点「上一题」）后紧跟的按键，不能读到还没重置的旧下标。 */
  const [kbState, setKbState] = useState<{ step: number; idx: number } | null>(null);
  const kbIndex = kbState && kbState.step === stepIndex ? kbState.idx : initialKbIndex();
  const setKbIndex = (idx: number) => setKbState({ step: stepIndex, idx });

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

  /** 键盘：Tab / Shift+Tab、↑↓ 切换高亮项，回车确认高亮项（与底部「Tab / 上下键切换 · 回车确认」一致）。
   *  只在焦点落在页面本身或卡片内时接管；焦点在侧栏输入框、设置页、弹窗等处时不抢按键。
   *  挂到 window 上：提问卡出现后不必先点一下卡片，键盘即可操作。 */
  const kbHandlerRef = useRef<(e: KeyboardEvent) => void>(() => {});
  kbHandlerRef.current = (e: KeyboardEvent) => {
    const ae = document.activeElement;
    if (ae && ae !== document.body && !wizardRef.current?.contains(ae)) return;
    if (e.isComposing || e.keyCode === 229 || e.altKey || e.ctrlKey || e.metaKey || optionCount === 0) return;
    // 自定义回答已含换行：上下键交给光标在多行文本内移动，不抢占选项导航
    if ((e.key === "ArrowUp" || e.key === "ArrowDown") && ae === customInputRef.current && customText.includes("\n")) return;
    const hl = Math.min(kbIndex, optionCount - 1);
    if (e.key === "Tab" || e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const back = e.key === "ArrowUp" || (e.key === "Tab" && e.shiftKey);
      setKbIndex(back ? (hl - 1 + optionCount) % optionCount : (hl + 1) % optionCount);
      return;
    }
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (e.repeat) return; // 按住回车的自动重复不连续确认多道题
      if (hl < customIdx) {
        handlePickOption(currentOptions[hl]);
      } else if (customText.trim()) {
        handleNextCustom();
      } else {
        // 自定义行还没有输入：回车只把焦点交给输入框，不替用户确认别处的答案
        customInputRef.current?.focus();
      }
    }
  };
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => kbHandlerRef.current(e);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const taskTag = String(detail.task_tag || detail.agent_name || "问答偏好");

  return (
    <div className="composer-question-wizard" ref={wizardRef}>
      <div className="question-wizard-header">
        <div className="question-wizard-title-group">
          <span className="question-wizard-tag">{taskTag}</span>
          <Tooltip content={String(currentQ.question ?? "")}>
            <span className="question-wizard-title-text">
              {String(currentQ.question ?? "请确认执行偏好")}
            </span>
          </Tooltip>
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
              // S16（plan-41-197）：移除原生 title——它与自定义浮窗（showOptTip）同时出现，
              // 聚焦/悬停时会叠两个提示框（用户反馈“两处展示浮窗”）。
              <button
                key={opt}
                type="button"
                className={`question-wizard-opt-btn${isSelected ? " selected" : ""}${kbIndex === optIdx ? " is-kb" : ""}`}
                onClick={() => handlePickOption(opt)}
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
            <div className={`question-wizard-custom-row${kbIndex === customIdx ? " is-kb" : ""}`}>
              <span className="question-wizard-opt-num">{currentOptions.length + 1}.</span>
              {/* 回车确认由卡片级键盘处理统一负责（见 kbHandlerRef），这里只把高亮移到自定义行 */}
              {/* 多行文本：回车确认（卡片级键盘处理），Shift+回车换行 */}
              <textarea
                ref={customInputRef}
                className="question-wizard-custom-input"
                rows={1}
                placeholder="输入自定义回答，回车确认，Shift+回车换行…"
                value={customText}
                onChange={(e) => setCustomText(e.target.value)}
                onFocus={() => setKbIndex(customIdx)}
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
          {/* plan-20261020-pts：原借用计划卡私有按钮类 .plan-inline-confirm（已随计划卡重做删除），改用统一按钮 */}
          <Button
            variant="primary"
            size="sm"
            onClick={handleNextCustom}
            disabled={!customText.trim() && !currentAnswer}
          >
            {stepIndex < total - 1 ? "继续" : "提交回答"}
          </Button>
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
