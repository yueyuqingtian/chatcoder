/** 工具卡数据聚合（plan-156-619 S7b；plan-20261019-toolcard-v2 重做）。
 *
 * 五个栏目的数据来源各不相同，集中在这里取一次，避免每个栏目组件各自订阅、各自发请求。
 *
 * 口径：
 *  · 进程 / 计划 / 子代理 / 工作流：本地派生（会话切片），切会话即随切片切换；
 *  · Git：**项目级**——分支、领先落后、未提交改动的文件数与增删行数，一次请求取齐
 *    （`/git/diff-stat`）。不再与「本会话改过的文件」取交集，也不再列文件：
 *    工具卡只给「更改 +N −M / 分支 / 提交或推送」三个入口，文件清单在右侧 Git 面板；
 *  · 计划标题：读计划文档首个一级标题（模块级缓存，切会话不重复请求）；
 *  · 空栏目 → 返回 `null`，由容器决定不渲染该栏目（而不是显示「暂无」）。
 *
 * Git 摘要的刷新时机：挂载 / 切项目、本会话写盘事件（节流）、运行期间每 5 秒、一轮结束、窗口重新获得焦点、
 * 以及卡片内做完切分支 / 新建分支后（`actions.refreshGit(true)` 强制刷新）。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError, type GitCardRepoOut, type GitDiffStatOut, type GitStatusOut } from "../../../api/client";
import { useChatStore, TERMINAL_SUBAGENT_STATUS } from "../../../store/chat";
import { usePanelStore, type PanelTabId } from "../../../store/panel";
import { useWorkflowStore } from "../../../store/workflow";
import { useProgressRows, type ProgressRow } from "../taskProgress";
import { foldProcessRows, type ProcessEntry } from "./processFold";
import type { PlanCardInfo } from "../../../store/chat";
import type { TurnOut } from "../../../api/client";

/** Git 摘要缓存有效期（ms）：同一项目内切会话不重复请求；略短于 5s 轮询，保证轮询总能拿到新数据。 */
const GIT_CACHE_MS = 4000;
/** 运行期间轮询 Git 摘要的间隔（ms）。 */
const GIT_POLL_MS = 5000;
/** 写盘事件触发刷新的节流（ms）：一轮里连续写很多文件时不会逐个请求。 */
const GIT_EVENT_THROTTLE_MS = 1500;
/** 计划标题兜底文案（文档没有一级标题 / 读取失败时）。 */
const PLAN_TITLE_FALLBACK = "任务执行计划";
/** 计划文档里取标题的匹配：首个一级（或二级）标题。 */
const PLAN_HEADING_RE = /^#{1,2}\s+(.+?)\s*#*\s*$/m;

const WORKFLOW_OPEN_STATUS = new Set(["draft", "running", "waiting_human", "paused"]);
/** turn 仍「活着」的状态：已确认的计划只在执行轮活着时展示。 */
const TURN_ALIVE_STATUS = new Set(["running", "interrupted", "awaiting_confirmation"]);

/** 与右侧 Git 面板共用的「当前仓库」记忆键（多仓库项目：卡片与面板默认作用于同一个仓库）。 */
const gitRepoKey = (projectId: number) => `chatcoder.git.repo.${projectId}`;

export interface GitSectionData {
  projectId: number;
  /** 项目内全部仓库的摘要（根仓库在前；单仓库项目只有一个） */
  repos: GitCardRepoOut[];
  /** 当前作用的仓库（分支行显示它的分支，分支弹窗对它操作） */
  repo: GitCardRepoOut;
  /** 全部仓库合计：未提交改动的文件数 / 新增行 / 删除行 */
  files: number;
  additions: number;
  deletions: number;
  /** 是否拿到了增删行数：后端版本较旧（没有 /git/diff-stat）时为 false，界面只显示文件数 */
  hasLineStats: boolean;
}

export interface PlanSectionData {
  turnId: number;
  title: string;
  planDocPath: string;
  awaiting: boolean;
}

export interface ProcessSectionData {
  rows: ProgressRow[];
  /** 按限高动态折叠后的展示条目（见 processFold.ts） */
  entries: ProcessEntry[];
  done: number;
  total: number;
  /** 胶囊上显示的那一行（优先 running，其次第一个未完成） */
  currentTitle: string;
  hasRunning: boolean;
}

export interface SubagentSectionData {
  running: { id: number; name: string }[];
  /** 已结束条目（当前轮次的排在最前）；全部折成一行展示 */
  finished: { id: number; name: string }[];
}

export interface WorkflowItem {
  id: number;
  title: string;
  status: string;
  done: number;
  total: number;
}

export interface WorkflowSectionData {
  /** 未结束的运行（草稿 / 运行中 / 待人工 / 暂停）：逐个列出，不折叠 */
  open: WorkflowItem[];
  /** 已结束的运行（成功 / 失败 / 取消）：折成一行，新的在前 */
  finished: WorkflowItem[];
}

export interface ToolCardActions {
  /** 重新读取 Git 摘要；force 绕过缓存（切分支 / 新建分支之后用） */
  refreshGit: (force?: boolean) => void;
  /** 多仓库项目切换卡片作用的仓库（记在 localStorage，与右侧 Git 面板同键） */
  setGitRepo: (rel: string) => void;
}

export interface ToolCardData {
  git: GitSectionData | null;
  plan: PlanSectionData | null;
  process: ProcessSectionData | null;
  subagent: SubagentSectionData | null;
  workflow: WorkflowSectionData | null;
  actions: ToolCardActions;
}

/* ── 模块级缓存：跨会话复用（切会话不重复请求，直接复用同项目的结果） ── */

const gitCache = new Map<number, { at: number; stat: GitDiffStatOut }>();
const gitInflight = new Map<number, Promise<GitDiffStatOut>>();
const planTitleCache = new Map<string, string>();

const normPath = (p: string): string => p.replace(/\\/g, "/").replace(/^\.\//, "");

/** 后端没有这个接口（版本比前端旧）：404 / 405。 */
const isMissingEndpoint = (e: unknown): boolean => e instanceof ApiError && (e.status === 404 || e.status === 405);

/** 旧后端没有 `/git/diff-stat` 时的降级：退回 `/git/status-all`（再不行退回 `/git/status`），
 *  拿得到分支、领先落后、改动文件数，拿不到增删行数——用 -1 标记「未知」，界面只显示文件数。
 *  前端比后端新是常态（开发时前端热更新、后端是已安装的版本），不能因为少一个接口整栏消失。 */
async function legacyGitStat(projectId: number): Promise<GitDiffStatOut> {
  let st: GitStatusOut;
  try {
    st = await api.gitStatusAll(projectId);
  } catch (e) {
    if (!isMissingEndpoint(e)) throw e;
    st = await api.gitStatus(projectId);
  }
  const single = !st.repos || st.repos.length <= 1;
  const rels = st.repos && st.repos.length > 0 ? st.repos.map((r) => r.rel) : [""];
  /** 路径归属：最长仓库前缀，根仓库（空串）兜底 */
  const ownerOf = (path: string): string => {
    const p = normPath(path);
    let hit = "";
    for (const r of rels) if (r && (p === r || p.startsWith(`${r}/`)) && r.length > hit.length) hit = r;
    return hit;
  };
  const byRepo = new Map<string, Set<string>>();
  const all = new Set<string>();
  for (const f of [...st.conflicts, ...st.staged, ...st.unstaged, ...st.untracked]) {
    const p = normPath(f.path);
    if (p.endsWith("/")) continue; // 内嵌子仓库目录，单独成仓
    all.add(p);
    const owner = ownerOf(p);
    if (!byRepo.has(owner)) byRepo.set(owner, new Set());
    byRepo.get(owner)!.add(p);
  }
  const baseName = (p: string) => p.replace(/[\\/]+$/, "").split(/[\\/]/).pop() || p;
  const repos = rels.map((rel) => {
    const meta = st.repos?.find((r) => r.rel === rel);
    return {
      rel,
      name: rel ? baseName(rel) : baseName(st.root),
      branch: meta?.branch ?? st.branch,
      detached: single ? st.detached : false,
      oid: single ? st.oid : null,
      ahead: single ? st.ahead : 0,
      behind: single ? st.behind : 0,
      files: byRepo.get(rel)?.size ?? 0,
      additions: -1,
      deletions: -1,
    };
  });
  return { additions: -1, deletions: -1, files: all.size, repos };
}

/** 取项目 Git 摘要：缓存期内复用；并发请求合并为一个。 */
async function fetchGitStat(projectId: number, force: boolean): Promise<GitDiffStatOut> {
  const hit = gitCache.get(projectId);
  if (!force && hit && Date.now() - hit.at < GIT_CACHE_MS) return hit.stat;
  const flying = gitInflight.get(projectId);
  if (flying) return flying;
  const p = api.gitDiffStat(projectId)
    .catch((e) => {
      if (!isMissingEndpoint(e)) throw e;
      return legacyGitStat(projectId);
    })
    .then((stat) => { gitCache.set(projectId, { at: Date.now(), stat }); return stat; })
    .finally(() => { gitInflight.delete(projectId); });
  gitInflight.set(projectId, p);
  return p;
}

function readSavedRepo(projectId: number | null): string {
  if (projectId == null) return "";
  try { return localStorage.getItem(gitRepoKey(projectId)) ?? ""; } catch { return ""; }
}

/** 取「最新的一份、仍未完成」的计划——用户口径：
 *  · 只看最新一份；旧计划被新消息取代（发新消息时未确认的计划会被标记取消）后不再展示；
 *  · 待确认 → 展示；已确认 → 仅在其执行轮仍活着（运行 / 中断待续）时展示，执行完即收起。 */
function pickPlan(
  plansByTurn: Record<number, PlanCardInfo>,
  turns: TurnOut[],
  runningTurnId: number | null,
): Omit<PlanSectionData, "title"> | null {
  let latest: PlanCardInfo | null = null;
  for (const p of Object.values(plansByTurn)) {
    if (!latest || p.turnId > latest.turnId) latest = p;
  }
  if (!latest || !latest.planDocPath) return null;
  const awaiting = latest.status === "awaiting_confirmation";
  if (!awaiting) {
    if (latest.status !== "confirmed") return null;
    const turn = turns.find((t) => t.id === latest!.turnId);
    const alive = runningTurnId === latest.turnId
      || (turn != null && TURN_ALIVE_STATUS.has(String(turn.status)) && turn.plan_status !== "done");
    if (!alive) return null;
  }
  return { turnId: latest.turnId, planDocPath: latest.planDocPath, awaiting };
}

/** 计划文档标题（首个标题行；带缓存）。 */
function usePlanTitle(projectId: number | null, path: string | null): string {
  const key = projectId != null && path ? `${projectId}:${path}` : "";
  const [title, setTitle] = useState(() => (key ? planTitleCache.get(key) ?? "" : ""));
  useEffect(() => {
    if (!key || projectId == null || !path) { setTitle(""); return; }
    const cached = planTitleCache.get(key);
    if (cached) { setTitle(cached); return; }
    let cancelled = false;
    setTitle("");
    api.readProjectFile(projectId, path)
      .then((f) => {
        const m = PLAN_HEADING_RE.exec(f.content || "");
        const t = (m?.[1] ?? "").replace(/[*_`]/g, "").trim();
        if (t) planTitleCache.set(key, t);
        if (!cancelled) setTitle(t);
      })
      .catch(() => { /* 读不到用兜底文案 */ });
    return () => { cancelled = true; };
  }, [key, projectId, path]);
  return title || PLAN_TITLE_FALLBACK;
}

export function useToolCardData(sessionId: number | null): ToolCardData {
  const projectId = useChatStore((s) => s.currentProjectId);
  const plansByTurn = useChatStore((s) => s.plansByTurn);
  const turns = useChatStore((s) => s.turns);
  const turnChanges = useChatStore((s) => s.turnChanges);
  const subagentMeta = useChatStore((s) => s.subagentMeta);
  const runningTurnId = useChatStore((s) => s.runningTurnId);
  const isRunning = useChatStore((s) => s.isRunning);
  const runs = useWorkflowStore((s) => (sessionId == null ? undefined : s.bySession[sessionId]));
  const progress = useProgressRows();

  // ── Git：项目级摘要（跨会话缓存）──
  const [stat, setStat] = useState<{ projectId: number; data: GitDiffStatOut } | null>(() => {
    const hit = projectId != null ? gitCache.get(projectId) : undefined;
    return hit && projectId != null ? { projectId, data: hit.stat } : null;
  });
  const [savedRepo, setSavedRepo] = useState(() => readSavedRepo(projectId));
  /** 请求序号：只让最新一次响应落地（切项目 / 并发刷新时旧响应作废） */
  const seqRef = useRef(0);

  const refreshGit = useCallback((force = false) => {
    if (projectId == null) { setStat(null); return; }
    const mine = ++seqRef.current;
    fetchGitStat(projectId, force)
      .then((data) => {
        if (mine !== seqRef.current) return;
        // 内容没变就保留原对象：运行期间每 5 秒轮询一次，不该每次都让整张卡片重渲染
        setStat((prev) => (
          prev && prev.projectId === projectId && JSON.stringify(prev.data) === JSON.stringify(data)
            ? prev
            : { projectId, data }
        ));
      })
      .catch(() => { if (mine === seqRef.current) setStat(null); }); // 非 Git 项目：整栏隐藏
  }, [projectId]);

  const setGitRepo = useCallback((rel: string) => {
    if (projectId == null) return;
    setSavedRepo(rel);
    try {
      if (rel) localStorage.setItem(gitRepoKey(projectId), rel);
      else localStorage.removeItem(gitRepoKey(projectId));
    } catch { /* 记忆失败不影响切换 */ }
  }, [projectId]);

  // 切项目：换上该项目的缓存（没有就先清空），并重新读取
  useEffect(() => {
    setSavedRepo(readSavedRepo(projectId));
    const hit = projectId != null ? gitCache.get(projectId) : undefined;
    setStat(hit && projectId != null ? { projectId, data: hit.stat } : null);
    refreshGit(false);
  }, [projectId, refreshGit]);

  // 运行期间轮询；运行刚结束（文件已落盘）强制刷新一次
  const wasRunningRef = useRef(isRunning);
  useEffect(() => {
    const justFinished = wasRunningRef.current && !isRunning;
    wasRunningRef.current = isRunning;
    if (justFinished) refreshGit(true);
    if (!isRunning) return;
    const timer = window.setInterval(() => refreshGit(true), GIT_POLL_MS);
    return () => window.clearInterval(timer);
  }, [isRunning, refreshGit]);

  // 本会话有新的写盘记录（file.change 事件会更新 turnChanges）：节流刷新，改动数字随写盘实时变化
  const eventTimerRef = useRef<number | null>(null);
  const firstChangesRef = useRef(true);
  useEffect(() => {
    if (firstChangesRef.current) { firstChangesRef.current = false; return; }
    if (eventTimerRef.current != null) return;
    eventTimerRef.current = window.setTimeout(() => {
      eventTimerRef.current = null;
      refreshGit(true);
    }, GIT_EVENT_THROTTLE_MS);
  }, [turnChanges, refreshGit]);
  useEffect(() => () => {
    if (eventTimerRef.current != null) window.clearTimeout(eventTimerRef.current);
  }, []);

  // 窗口重新获得焦点：用户可能在外部改过文件 / 切过分支
  useEffect(() => {
    const onFocus = () => refreshGit(false);
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [refreshGit]);

  const git = useMemo<GitSectionData | null>(() => {
    if (!stat || projectId == null || stat.projectId !== projectId || stat.data.repos.length === 0) return null;
    const repos = stat.data.repos;
    const repo = repos.find((r) => r.rel === savedRepo) ?? repos.find((r) => r.rel === "") ?? repos[0];
    return {
      projectId,
      repos,
      repo,
      files: stat.data.files,
      additions: Math.max(0, stat.data.additions),
      deletions: Math.max(0, stat.data.deletions),
      hasLineStats: stat.data.additions >= 0 && stat.data.deletions >= 0,
    };
  }, [stat, projectId, savedRepo]);

  const planBase = useMemo(
    () => pickPlan(plansByTurn || {}, turns || [], runningTurnId),
    [plansByTurn, turns, runningTurnId],
  );
  const planTitle = usePlanTitle(projectId, planBase?.planDocPath ?? null);
  const plan = useMemo<PlanSectionData | null>(
    () => (planBase ? { ...planBase, title: planTitle } : null),
    [planBase, planTitle],
  );

  const processData = useMemo<ProcessSectionData | null>(() => {
    const rows = progress.rows;
    if (rows.length === 0) return null;
    const done = rows.filter((r) => r.status === "done").length;
    const current = rows.find((r) => r.status === "running")
      ?? rows.find((r) => r.status !== "done");
    return {
      rows,
      entries: foldProcessRows(rows),
      done,
      total: rows.length,
      currentTitle: current?.title ?? "",
      hasRunning: rows.some((r) => r.status === "running"),
    };
  }, [progress.rows]);

  const subagentData = useMemo<SubagentSectionData | null>(() => {
    const entries = Object.entries(subagentMeta || {});
    if (entries.length === 0) return null;
    const running: { id: number; name: string }[] = [];
    const finished: { id: number; name: string }[] = [];
    /** 当前轮次已结束的条目：排在 finished 最前（「刚跑完的这一批先看到」），
     *  但**不**假装它们还在跑（plan-20261018-subagent-status-fix）。 */
    const currentTurnFinished: { id: number; name: string }[] = [];
    for (const [idStr, m] of entries) {
      const id = Number(idStr);
      const name = m.name || `子代理 #${id}`;
      /* 【口径统一】终态必须压过「当前轮次」：
         旧实现只要 `m.turnId === runningTurnId` 就无条件计入 running 数组，于是
         `status === "done"` 的子代理在主代理还在同一轮里继续干活时，工具卡一直显示
         「N 运行中」——而右面板按 status 判定、同一时刻显示「已完成」，两边自相矛盾。 */
      const isTerminal = TERMINAL_SUBAGENT_STATUS.has(String(m.status));
      const isRunningAgent = !isTerminal && (m.status === "running" || m.status === "in_progress");
      /* plan-20261019-subagent-ghost：没有任何归属锚点、也没有任何输出的条目
         （典型来源是主代理完成事件曾被误记成「子代理 #N」）不进入栏目——
         它们点开只是一个空面板。运行中的条目仍保留：它可能还没来得及产出消息。 */
      const hasAnchor = m.turnId != null || m.taskId != null || m.programId != null
        || m.spawnMessageId != null || (m.messageCount ?? 0) > 0;
      if (!hasAnchor && !isRunningAgent) continue;
      const isCurrentTurn = m.turnId != null && m.turnId === runningTurnId;
      if (isRunningAgent) running.push({ id, name });
      else if (isCurrentTurn) currentTurnFinished.push({ id, name });
      else finished.push({ id, name });
    }
    if (running.length === 0 && currentTurnFinished.length === 0 && finished.length === 0) return null;
    return { running, finished: [...currentTurnFinished, ...finished] };
  }, [subagentMeta, runningTurnId]);

  const workflowData = useMemo<WorkflowSectionData | null>(() => {
    const list = runs ?? [];
    if (list.length === 0) return null;
    const open: WorkflowItem[] = [];
    const finished: WorkflowItem[] = [];
    for (const r of list) {
      const item: WorkflowItem = {
        id: r.id,
        title: r.title || "工作流",
        status: String(r.status),
        done: r.progress?.done ?? 0,
        total: r.progress?.total ?? 0,
      };
      (WORKFLOW_OPEN_STATUS.has(item.status) ? open : finished).push(item);
    }
    finished.reverse(); // 新的在前
    return { open, finished };
  }, [runs]);

  const actions = useMemo<ToolCardActions>(() => ({ refreshGit, setGitRepo }), [refreshGit, setGitRepo]);

  return { git, plan, process: processData, subagent: subagentData, workflow: workflowData, actions };
}

/** 打开右面板某标签（面板收起时先展开——旧实现只切标签，面板收起时点了看不到任何变化）。 */
export function revealPanelTab(id: PanelTabId): void {
  const panel = usePanelStore.getState();
  panel.openPanel();
  panel.openTab(id);
}

/** 计划栏目点击：在右面板预览计划文档（与 PlanCard「查看完整计划」口径一致）。 */
export function openPlanDoc(path: string): void {
  usePanelStore.getState().openPlanPreview(path);
  revealPanelTab("files");
}

/** Git 栏目点击「更改」「提交或推送」：打开右面板 Git 标签（查看变更 / 提交 / 推送）。 */
export function openGitPanel(): void {
  revealPanelTab("git");
}
