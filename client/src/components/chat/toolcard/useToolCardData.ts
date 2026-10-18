/** 工具卡数据聚合（plan-156-619 S7b）。
 *
 * 五个栏目的数据来源各不相同，集中在这里取一次，避免每个栏目组件各自订阅、
 * 各自发请求（切会话时容易出现 5 个请求互相打架、状态错位）。
 *
 * 口径：
 *  · 进程 / 计划 / 子代理 / 工作流：全部是本地派生（store 已有），零请求；
 *  · Git：唯一需要请求的栏目，按会话记忆 5s 内复用，带自增 seq 丢弃过期响应；
 *  · 空栏目 → 返回 `null`，由容器决定不渲染该栏目（而不是显示「暂无」）。
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { api, type GitStatusOut } from "../../../api/client";
import { useChatStore } from "../../../store/chat";
import { usePanelStore } from "../../../store/panel";
import { useWorkflowStore } from "../../../store/workflow";
import { useProgressRows, type ProgressRow } from "../taskProgress";
import type { PlanCardInfo } from "../../../store/chat";

/** Git 栏目在展开态最多列出的文件数（其余走「查看全部」跳右面板）。 */
export const GIT_LIST_LIMIT = 5;
/** 已结束子代理的折叠阈值（用户指定：超过 4 个才折叠）。 */
export const SUBAGENT_COLLAPSE_AT = 4;
/** Git 状态缓存有效期（ms）：切会话回来不重复请求。 */
const GIT_CACHE_MS = 5000;

const WORKFLOW_OPEN_STATUS = new Set(["draft", "running", "waiting_human", "paused"]);

export interface GitItem {
  path: string;
  code: string;
  staged: boolean;
}

export interface GitSectionData {
  branch: string | null;
  clean: boolean;
  items: GitItem[];
  /** 增删统计：GitStatusOut 不含行数，用文件数代替，避免为此再加一次 diff 请求 */
  fileCount: number;
}

export interface PlanSectionData {
  turnId: number;
  task: string;
  planDocPath: string;
  awaiting: boolean;
}

export interface ProcessSectionData {
  rows: ProgressRow[];
  done: number;
  total: number;
  /** 折叠态胶囊上显示的那一行（优先 running，其次第一个未完成） */
  currentTitle: string;
  hasRunning: boolean;
}

export interface SubagentSectionData {
  running: { id: number; name: string }[];
  /** 已结束且非当前轮的条目（超过阈值才折叠展示） */
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

/** 取「最新且未完成 / 正在执行」的计划——用户口径：旧计划被新消息取代后不再展示。 */
function pickPlan(plansByTurn: Record<number, PlanCardInfo>): PlanSectionData | null {
  let best: PlanCardInfo | null = null;
  for (const p of Object.values(plansByTurn)) {
    if (p.status !== "awaiting_confirmation" && p.status !== "confirmed") continue;
    if (!best || p.turnId > best.turnId) best = p;
  }
  if (!best) return null;
  return {
    turnId: best.turnId,
    task: best.task,
    planDocPath: best.planDocPath,
    awaiting: best.status === "awaiting_confirmation",
  };
}

export function useToolCardData(sessionId: number | null): ToolCardData {
  const projectId = useChatStore((s) => s.currentProjectId);
  const plansByTurn = useChatStore((s) => s.plansByTurn);
  const subagentMeta = useChatStore((s) => s.subagentMeta);
  const turnIdNow = useChatStore((s) => s.runningTurnId);
  const runs = useWorkflowStore((s) => (sessionId == null ? undefined : s.bySession[sessionId]));
  const progress = useProgressRows();

  // ── Git（唯一需要请求的栏目）──
  const [git, setGit] = useState<GitSectionData | null>(null);
  const gitCacheRef = useRef<{ at: number; projectId: number }>({ at: 0, projectId: 0 });
  const gitSeqRef = useRef(0);

  useEffect(() => {
    if (projectId == null) {
      setGit(null);
      return;
    }
    const now = Date.now();
    const cache = gitCacheRef.current;
    if (cache.projectId === projectId && now - cache.at < GIT_CACHE_MS) return;

    const seq = ++gitSeqRef.current;
    void (async () => {
      try {
        const st: GitStatusOut = await api.gitStatus(projectId);
        if (seq !== gitSeqRef.current) return; // 过期响应丢弃
        gitCacheRef.current = { at: Date.now(), projectId };
        if (st.clean) {
          setGit({ branch: st.branch, clean: true, items: [], fileCount: 0 });
          return;
        }
        const items: GitItem[] = [
          ...st.conflicts.map((f) => ({ path: f.path, code: f.code, staged: false })),
          ...st.staged.map((f) => ({ path: f.path, code: f.code, staged: true })),
          ...st.unstaged.map((f) => ({ path: f.path, code: f.code, staged: false })),
          ...st.untracked.map((f) => ({ path: f.path, code: f.code, staged: false })),
        ];
        setGit({ branch: st.branch, clean: false, items, fileCount: items.length });
      } catch {
        // Git 不可用（非仓库 / 未安装）：不渲染该栏目，而不是显示一条报错占位
        if (seq === gitSeqRef.current) setGit(null);
      }
    })();
  }, [projectId]);

  const plan = useMemo(() => pickPlan(plansByTurn || {}), [plansByTurn]);

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
    for (const [idStr, m] of entries) {
      const id = Number(idStr);
      const name = m.name || `子代理 #${id}`;
      const isRunning = m.status === "running" || m.status === "in_progress";
      // 当前轮次的子代理永不折叠（用户明确要求）
      const isCurrentTurn = m.turnId != null && m.turnId === turnIdNow;
      if (isRunning || isCurrentTurn) running.push({ id, name });
      else finished.push({ id, name });
    }
    if (running.length === 0 && finished.length === 0) return null;
    return { running, finished };
  }, [subagentMeta, turnIdNow]);

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

/** 计划栏目点击：在右面板打开计划文档（与 PlanCard 的跳转口径一致）。 */
export function openPlanDoc(path: string): void {
  const panel = usePanelStore.getState();
  panel.setPreviewPath(path);
  panel.openTab("files");
}

/** Git 栏目点击「查看全部」：打开右面板 Git 标签。 */
export function openGitPanel(): void {
  usePanelStore.getState().openTab("git");
}
