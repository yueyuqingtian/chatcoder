/** 工具卡数据聚合（plan-156-619 S7b；plan-156-620 修订）。
 *
 * 五个栏目的数据来源各不相同，集中在这里取一次，避免每个栏目组件各自订阅、各自发请求。
 *
 * 口径：
 *  · 进程 / 计划 / 子代理 / 工作流：本地派生（会话切片），切会话即随切片切换；
 *  · Git：项目级 `git status` 与**本会话改动过的文件**取交集——卡片按会话隔离，
 *    别的会话改的文件不出现在这里（完整列表仍在右面板 Git 标签）；
 *  · 计划标题：读计划文档首个一级标题（模块级缓存，切会话不重复请求）；
 *  · 空栏目 → 返回 `null`，由容器决定不渲染该栏目（而不是显示「暂无」）。
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { api, type GitStatusOut } from "../../../api/client";
import { useChatStore, TERMINAL_SUBAGENT_STATUS } from "../../../store/chat";
import { usePanelStore, type PanelTabId } from "../../../store/panel";
import { useWorkflowStore } from "../../../store/workflow";
import { useProgressRows, type ProgressRow } from "../taskProgress";
import type { PlanCardInfo } from "../../../store/chat";
import type { TurnOut } from "../../../api/client";

/** Git 栏目在展开态最多列出的文件数（其余走「查看全部」跳右面板）。 */
export const GIT_LIST_LIMIT = 5;
/** 已结束子代理的折叠阈值（用户指定：超过 4 个才折叠）。 */
export const SUBAGENT_COLLAPSE_AT = 4;
/** Git 状态缓存有效期（ms）：同一项目内切会话不重复请求。 */
const GIT_CACHE_MS = 5000;
/** 计划标题兜底文案（文档没有一级标题 / 读取失败时）。 */
const PLAN_TITLE_FALLBACK = "任务执行计划";
/** 计划文档里取标题的匹配：首个一级（或二级）标题。 */
const PLAN_HEADING_RE = /^#{1,2}\s+(.+?)\s*#*\s*$/m;

const WORKFLOW_OPEN_STATUS = new Set(["draft", "running", "waiting_human", "paused"]);
/** turn 仍「活着」的状态：已确认的计划只在执行轮活着时展示。 */
const TURN_ALIVE_STATUS = new Set(["running", "interrupted", "awaiting_confirmation"]);

export interface GitItem {
  path: string;
  code: string;
  staged: boolean;
}

export interface GitSectionData {
  branch: string | null;
  items: GitItem[];
  fileCount: number;
}

export interface PlanSectionData {
  turnId: number;
  title: string;
  planDocPath: string;
  awaiting: boolean;
}

export interface ProcessSectionData {
  rows: ProgressRow[];
  done: number;
  total: number;
  /** 胶囊上显示的那一行（优先 running，其次第一个未完成） */
  currentTitle: string;
  hasRunning: boolean;
}

export interface SubagentSectionData {
  running: { id: number; name: string }[];
  /** 已结束条目（当前轮次的排在最前；超过阈值才折叠展示） */
  finished: { id: number; name: string }[];
}

export interface WorkflowSectionData {
  id: number;
  title: string;
  status: string;
  done: number;
  total: number;
}

export interface ToolCardData {
  git: GitSectionData | null;
  plan: PlanSectionData | null;
  process: ProcessSectionData | null;
  subagent: SubagentSectionData | null;
  workflow: WorkflowSectionData | null;
}

/* ── 模块级缓存：跨会话复用（切会话不重复请求，直接复用同项目的结果） ── */

const gitCache = new Map<number, { at: number; status: GitStatusOut }>();
const gitInflight = new Map<number, Promise<GitStatusOut>>();
const planTitleCache = new Map<string, string>();

/** 取项目 git 状态：5s 内复用；并发请求合并为一个。 */
async function fetchGitStatus(projectId: number, force: boolean): Promise<GitStatusOut> {
  const hit = gitCache.get(projectId);
  if (!force && hit && Date.now() - hit.at < GIT_CACHE_MS) return hit.status;
  const flying = gitInflight.get(projectId);
  if (flying) return flying;
  const p = api.gitStatus(projectId)
    .then((status) => { gitCache.set(projectId, { at: Date.now(), status }); return status; })
    .finally(() => { gitInflight.delete(projectId); });
  gitInflight.set(projectId, p);
  return p;
}

const normPath = (p: string): string => p.replace(/\\/g, "/").replace(/^\.\//, "");

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

  // ── Git：项目状态（跨会话缓存），轮次结束时强制刷新一次 ──
  const [gitStatus, setGitStatus] = useState<GitStatusOut | null>(
    () => (projectId != null ? gitCache.get(projectId)?.status ?? null : null),
  );
  /** 上一次的运行态：只有「运行 → 结束」的跳变才强制刷新（切会话不算） */
  const wasRunningRef = useRef(isRunning);
  useEffect(() => {
    const justFinished = wasRunningRef.current && !isRunning;
    wasRunningRef.current = isRunning;
    if (projectId == null) { setGitStatus(null); return; }
    let cancelled = false;
    setGitStatus(gitCache.get(projectId)?.status ?? null);
    // 运行刚结束时文件已落盘，强制刷新；其余情况（含切会话）5s 内复用缓存
    fetchGitStatus(projectId, justFinished)
      .then((st) => { if (!cancelled) setGitStatus(st); })
      .catch(() => { if (!cancelled) setGitStatus(null); });
    return () => { cancelled = true; };
  }, [projectId, isRunning]);

  /** 本会话改动过的文件（turnChanges 已按会话切片隔离）。 */
  const sessionPaths = useMemo(() => {
    const set = new Set<string>();
    for (const list of Object.values(turnChanges || {})) {
      for (const c of list || []) set.add(normPath(c.path));
    }
    return set;
  }, [turnChanges]);

  const git = useMemo<GitSectionData | null>(() => {
    if (!gitStatus || gitStatus.clean || sessionPaths.size === 0) return null;
    const all: GitItem[] = [
      ...gitStatus.conflicts.map((f) => ({ path: f.path, code: f.code, staged: false })),
      ...gitStatus.staged.map((f) => ({ path: f.path, code: f.code, staged: true })),
      ...gitStatus.unstaged.map((f) => ({ path: f.path, code: f.code, staged: false })),
      ...gitStatus.untracked.map((f) => ({ path: f.path, code: f.code, staged: false })),
    ];
    const seen = new Set<string>();
    const items = all.filter((f) => {
      const p = normPath(f.path);
      if (!sessionPaths.has(p) || seen.has(p)) return false;
      seen.add(p);
      return true;
    });
    if (items.length === 0) return null;
    return { branch: gitStatus.branch, items, fileCount: items.length };
  }, [gitStatus, sessionPaths]);

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
      const isCurrentTurn = m.turnId != null && m.turnId === runningTurnId;
      if (isRunningAgent) running.push({ id, name });
      else if (isCurrentTurn) currentTurnFinished.push({ id, name });
      else finished.push({ id, name });
    }
    return { running, finished: [...currentTurnFinished, ...finished] };
  }, [subagentMeta, runningTurnId]);

  const workflowData = useMemo<WorkflowSectionData | null>(() => {
    const open = (runs || []).filter((r) => WORKFLOW_OPEN_STATUS.has(String(r.status)));
    if (open.length === 0) return null;
    // 取最近更新的一个作为主展示
    const top = open[open.length - 1];
    return {
      id: top.id,
      title: top.title || "工作流",
      status: String(top.status),
      done: top.progress?.done ?? 0,
      total: top.progress?.total ?? 0,
    };
  }, [runs]);

  return { git, plan, process: processData, subagent: subagentData, workflow: workflowData };
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

/** Git 栏目点击分支行：打开右面板 Git 标签（提交 / 推送 / 切分支）。 */
export function openGitPanel(): void {
  revealPanelTab("git");
}
