/** 工作流视图模型：状态文案与色调、进度与当前动作的推导。
 *
 * 聊天卡片、右侧面板与设置页共用这里的口径，保证同一状态在各处的文字一致。
 */
import type { BadgeTone } from "../ui";
import type { StatusKind } from "../iconkit/StatusIcon";
import type { WorkflowNodeOut, WorkflowProgramOut, WorkflowRunOut } from "../../api/client";
import { parseServerTime } from "../../utils/time";

export interface RunStatusView {
  label: string;
  tone: BadgeTone;
  variant: "soft" | "outline" | "solid";
}

const RUN_STATUS: Record<string, RunStatusView> = {
  draft: { label: "待确认", tone: "neutral", variant: "outline" },
  running: { label: "运行中", tone: "info", variant: "soft" },
  waiting_human: { label: "待你决定", tone: "warning", variant: "soft" },
  paused: { label: "已暂停", tone: "warning", variant: "solid" },
  succeeded: { label: "已完成", tone: "success", variant: "soft" },
  failed: { label: "失败", tone: "danger", variant: "soft" },
  cancelled: { label: "已取消", tone: "neutral", variant: "soft" },
};

const NODE_STATUS: Record<string, { label: string; icon: StatusKind }> = {
  pending: { label: "未开始", icon: "pending" },
  queued: { label: "排队中", icon: "queued" },
  running: { label: "进行中", icon: "running" },
  recovering: { label: "网络重试中", icon: "running" },
  waiting_approval: { label: "等待工具审批", icon: "paused" },
  waiting_human: { label: "等待你的决定", icon: "paused" },
  awaiting_merge: { label: "等待合并步骤完成", icon: "queued" },
  done: { label: "已完成", icon: "done" },
  failed: { label: "失败", icon: "failed" },
  interrupted: { label: "已中断，可继续", icon: "interrupted" },
  cancelled: { label: "已取消", icon: "cancelled" },
  skipped: { label: "已跳过", icon: "cancelled" },
};

export const KIND_LABEL: Record<string, string> = {
  explore: "探索", execute: "实现", review: "评估", check: "检查", gate: "决定", merge: "合并",
  integrate: "并入主工作区",
};

/** 步骤轨道里的短标签：轨道只做总览，完整步骤名放在悬停提示与步骤明细中。 */
export const KIND_SHORT_LABEL: Record<string, string> = {
  ...KIND_LABEL,
  integrate: "并入",
};

const PAUSE_LABEL: Record<string, string> = {
  network: "网络中断",
  restart: "应用退出后中断",
  user: "已手动暂停",
  turn_stop: "已停止当前轮",
  budget: "token 预算已用完",
  error: "运行出错",
};

export function runStatusView(status: string, closedReason?: string | null): RunStatusView {
  // 草稿被“调整”取代：与聊天里的计划卡一致，显示为中性的“已调整”
  if (status === "cancelled" && closedReason === "adjusted") {
    return { label: "已调整", tone: "neutral", variant: "outline" };
  }
  return RUN_STATUS[status] ?? { label: status, tone: "neutral", variant: "soft" };
}

export function nodeStatusView(status: string): { label: string; icon: StatusKind } {
  return NODE_STATUS[status] ?? { label: status, icon: "pending" };
}

export function allNodes(snap: WorkflowProgramOut): WorkflowNodeOut[] {
  return snap.flows.flatMap((f) => f.nodes);
}

export interface Progress {
  done: number;
  total: number;
}

/** 已完成（含跳过）的步骤数 / 总步骤数 */
export function progressOf(snap: WorkflowProgramOut): Progress {
  const nodes = allNodes(snap);
  const done = nodes.filter((n) => n.status === "done" || n.status === "skipped").length;
  return { done, total: nodes.length };
}

/** 等待用户决定的闸门（至多一个：子工作流内串行，闸门会暂停该子工作流） */
export function gateOf(snap: WorkflowProgramOut): { flowKey: string; node: WorkflowNodeOut } | null {
  for (const flow of snap.flows) {
    const node = flow.nodes.find((n) => n.status === "waiting_human");
    if (node) return { flowKey: flow.run.package_key ?? "", node };
  }
  return null;
}

/** 子工作流依赖的其它子工作流标题（来自计划图），无依赖返回空串 */
export function dependencyLabel(snap: WorkflowProgramOut, flowKey: string | null): string {
  const flows = (snap.run.graph?.flows ?? []) as Array<{ key?: string; title?: string; depends_on?: string[] }>;
  const me = flows.find((f) => f.key === flowKey);
  const deps = me?.depends_on ?? [];
  if (deps.length === 0) return "";
  const titles = deps.map((k) => flows.find((f) => f.key === k)?.title || k);
  return `等待：${titles.join("、")}`;
}

/** 第一个失败的节点：失败状态下“从断点继续 / 从头重做该步骤”针对的对象 */
export function failedNodeOf(snap: WorkflowProgramOut): { flowKey: string; node: WorkflowNodeOut } | null {
  for (const flow of snap.flows) {
    const node = flow.nodes.find((n) => n.status === "failed");
    if (node) return { flowKey: flow.run.package_key ?? "", node };
  }
  return null;
}

/** 当前一句话进展：卡片与面板顶部共用，回答“现在在做什么、有没有丢进度、怎么办” */
export function currentNote(snap: WorkflowProgramOut): string {
  const run = snap.run;
  if (run.status === "draft") return "计划已生成，确认后开始执行";
  if (run.status === "succeeded") return "全部步骤已完成";
  if (run.status === "cancelled") {
    return run.closed_reason === "adjusted"
      ? "已按新的要求调整，由新计划替代"
      : "已取消，已完成的改动保留在工作区";
  }
  const gate = gateOf(snap);
  if (gate) return `等待你的决定：${gate.node.reason || gate.node.question || gate.node.title}`;
  const nodes = allNodes(snap);
  const active = nodes.find((n) => n.status === "running" || n.status === "recovering");
  if (active?.status === "recovering") return `网络波动，正在重试：${active.title}`;
  if (active?.kind === "execute" && active.rework_round > 0) {
    return `评估未通过，正在返工：${flowTitleOf(snap, active)} · ${active.title}（第 ${active.rework_round} 次）`;
  }
  if (active) {
    const attempt = active.attempt_no > 1 ? `（第 ${active.attempt_no} 次尝试）` : "";
    return `正在${KIND_LABEL[active.kind] ?? "执行"}：${active.title}${attempt}`;
  }
  const { done } = progressOf(snap);
  if (run.status === "paused") {
    const reason = PAUSE_LABEL[run.pause_kind ?? ""] ?? "已暂停";
    return `${reason}，已保留 ${done} 步进度，可继续`;
  }
  if (run.status === "failed") {
    const failed = nodes.find((n) => n.status === "failed");
    if (failed) return `${failed.title}：${failed.reason || "执行失败"}`;
    return "运行失败，可从断点继续";
  }
  return "等待调度";
}

/** 列表筛选：进行中 = 待确认 / 运行中 / 待决定；已暂停单列 */
export function isOpenRun(run: WorkflowRunOut): boolean {
  return run.status === "draft" || run.status === "running" || run.status === "waiting_human";
}

/** 改动文件清单：最多列出 5 个，其余以数量概括 */
export function changedFilesSummary(files: string[]): { shown: string[]; more: number } {
  return { shown: files.slice(0, 5), more: Math.max(0, files.length - 5) };
}

/** 续跑前的工作区漂移提示 */
export function driftMessage(files: string[]): string {
  const { shown, more } = changedFilesSummary(files);
  const tail = more > 0 ? ` 等 ${files.length} 个文件` : "";
  return `工作区在暂停期间有改动：${shown.join("、")}${tail}。继续可能覆盖这些改动，确定仍要继续吗？`;
}

/** 工作流时间文案：服务端时间按 UTC 解析后换算为本机时区（见 utils/time.parseServerTime） */
export function formatTime(iso: string | null | undefined): string {
  const ts = parseServerTime(iso);
  if (Number.isNaN(ts)) return "";
  return new Date(ts).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

/** 所在子工作流的标题（找不到时退回标识） */
export function flowTitleOf(snap: WorkflowProgramOut, node: WorkflowNodeOut): string {
  for (const flow of snap.flows) {
    if (flow.nodes.some((n) => n.id === node.id)) return flow.run.title || flow.run.package_key || "";
  }
  return "";
}

/** 子工作流在计划中的依赖层级：同层的子工作流可以并行，层数决定先后 */
export function flowLevels(snap: WorkflowProgramOut): string[][] {
  const flows = (snap.run.graph?.flows ?? []) as Array<{ key?: string; depends_on?: string[] }>;
  const keys = flows.map((f) => f.key ?? "");
  const level = new Map<string, number>();
  const levelOf = (key: string, guard: number): number => {
    if (level.has(key)) return level.get(key) ?? 0;
    if (guard > keys.length) return 0;
    const deps = flows.find((f) => f.key === key)?.depends_on ?? [];
    const value = deps.length === 0 ? 0 : 1 + Math.max(...deps.map((d) => levelOf(d, guard + 1)));
    level.set(key, value);
    return value;
  };
  keys.forEach((k) => levelOf(k, 0));
  const groups: string[][] = [];
  for (const key of keys) {
    const l = level.get(key) ?? 0;
    (groups[l] ??= []).push(key);
  }
  return groups.filter(Boolean);
}

/** 预计并发：同一层子工作流的最大个数（与会话、全局上限取最紧者由运行时决定） */
export function estimatedConcurrency(snap: WorkflowProgramOut): number {
  return Math.max(1, ...flowLevels(snap).map((g) => g.length));
}

/** 角色链：各步骤的类型标签，如“探索 → 实现 → 评估” */
export function roleChain(nodes: Array<{ kind: string }>): string {
  return nodes.map((n) => KIND_LABEL[n.kind] ?? n.kind).join(" → ");
}

/** 写入范围摘要：最多列出两个路径，其余以数量概括；没有写入范围时说明是只读 */
export function scopeSummary(nodes: Array<{ write_scope: string[] }>): string {
  const paths = Array.from(new Set(nodes.flatMap((n) => n.write_scope ?? [])));
  if (paths.length === 0) return "只读，不改动文件";
  const shown = paths.slice(0, 2).join("、");
  return paths.length > 2 ? `写入 ${shown} 等 ${paths.length} 处` : `写入 ${shown}`;
}

/** 子工作流的整体状态，用于子工作流级的进度轨道 */
export function flowStatusOf(nodes: WorkflowNodeOut[]): string {
  const statuses = nodes.map((n) => n.status);
  if (statuses.length && statuses.every((s) => s === "done" || s === "skipped")) return "done";
  if (statuses.includes("failed")) return "failed";
  if (statuses.includes("waiting_human")) return "waiting_human";
  if (statuses.some((s) => s === "running" || s === "recovering")) return "running";
  if (statuses.includes("interrupted") || statuses.includes("waiting_approval")) return "paused";
  if (statuses.some((s) => s === "queued")) return "queued";
  return "pending";
}

/** 评估给出的问题（文件:行 + 问题 + 修复建议），用于展开查看 */
export interface ReviewIssue {
  severity: string;
  file: string;
  line: number | null;
  problem: string;
  fix_hint: string;
}

export function issuesOf(node: WorkflowNodeOut): ReviewIssue[] {
  const raw = (node.verdict?.issues as unknown[] | undefined) ?? [];
  return raw
    .filter((item): item is Record<string, unknown> => !!item && typeof item === "object")
    .map((item) => ({
      severity: String(item.severity ?? "minor"),
      file: String(item.file ?? ""),
      line: typeof item.line === "number" ? item.line : null,
      problem: String(item.problem ?? ""),
      fix_hint: String(item.fix_hint ?? ""),
    }));
}

/** 网络重试的倒计时文案（绝对时间本地计算）：固定槽位，由调用方预留宽度 */
export function retryCountdown(nextRetryAt: string | null | undefined, now: number): string {
  if (!nextRetryAt) return "";
  const left = Math.ceil((parseServerTime(nextRetryAt) - now) / 1000);
  if (!Number.isFinite(left)) return "";
  return left <= 0 ? "即将重试" : `${left} 秒后`;
}

/** 运行的耗时文案（开始到结束；仍在进行则算到现在）；未开始时返回空串 */
export function durationText(
  startIso: string | null | undefined,
  endIso: string | null | undefined,
  now: number,
): string {
  if (!startIso) return "";
  const start = parseServerTime(startIso);
  const end = endIso ? parseServerTime(endIso) : now;
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return "";
  const seconds = Math.round((end - start) / 1000);
  if (seconds < 60) return `${seconds} 秒`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} 分钟`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `${hours} 小时 ${rest} 分` : `${hours} 小时`;
}

/** 计划中的子工作流个数（列表行里展示） */
export function subflowCount(run: WorkflowRunOut): number {
  const flows = (run.graph?.flows ?? []) as unknown[];
  return flows.length;
}

/** 尝试的模式与结果的中文说明（尝试记录里使用） */
export const ATTEMPT_MODE_LABEL: Record<string, string> = {
  fresh: "新建",
  checkpoint: "续跑",
  rework: "返工",
  repair: "修复格式",
};

export const ATTEMPT_STATUS_LABEL: Record<string, string> = {
  running: "进行中",
  done: "已完成",
  failed: "失败",
  interrupted: "已中断",
  cancelled: "已取消",
  stalled: "长时间无进展",
};
