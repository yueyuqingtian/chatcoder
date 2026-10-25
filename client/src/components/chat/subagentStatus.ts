/** 子代理的展示口径（plan-20261025-progress-keep）：工具卡「子代理」栏与右面板摘要「子代理」卡共用。
 *
 * 此前两处各判各的：工具卡把失败 / 已停止的子代理一并折进「已完成 N 个」并画成绿勾，
 * 摘要却按真实状态画成失败 / 已停止；工具卡过滤了幽灵条目，摘要没有——同一时刻两边的数量与状态对不上。
 */
import type { SubagentMeta } from "../../store/chat";

export type SubagentState = "running" | "done" | "failed" | "stopped" | "pending";

/** 原始状态 → 展示状态（未知状态按待开始处理，不冒充已完成）。 */
export function subagentState(status: unknown): SubagentState {
  const s = String(status ?? "").toLowerCase();
  if (s === "running" || s === "in_progress") return "running";
  if (s === "done" || s === "completed") return "done";
  if (s === "failed" || s === "error") return "failed";
  // plan-562-2318：后端暂停子代理时下发 paused（orchestration/subagent.py），
  // 归入 stopped 与取消一致——此前落入 pending，暂停的子代理显示成「待开始」。
  if (s === "paused" || s === "cancelled" || s === "canceled" || s === "terminated") return "stopped";
  return "pending";
}

/** 是否在入口里列出（plan-20261019-subagent-ghost）：没有任何归属锚点、也没有任何输出的条目
 *  （典型来源是主代理完成事件曾被误记成「子代理 #N」）点开只是一个空面板，不列出；
 *  运行中的条目照常列出——它可能还没来得及产出消息。 */
export function isListedSubagent(m: SubagentMeta): boolean {
  if (subagentState(m.status) === "running") return true;
  return m.turnId != null || m.taskId != null || m.programId != null
    || m.spawnMessageId != null || (m.messageCount ?? 0) > 0;
}
