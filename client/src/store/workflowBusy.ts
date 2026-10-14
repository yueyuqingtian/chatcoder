/** 会话的「工作流后台活动」口径（纯函数，无 store / 网络依赖，便于单测）。
 *
 *  工作流跑在后台：主代理发起后即收工，会话里可能既没有运行中的 turn、也没有在等的子代理，
 *  但整轮并没有结束。侧栏 / 输入框 / 摘要 / 消息流的「执行中」都以本模块的口径为准。 */
import type { WorkflowRunOut } from "../api/client";
import { parseUtc } from "../utils/time";

/** 会话是否有正在后台执行的工作流（程序 running）。它与轮次运行态并列：工作流在跑时，
 *  本会话可能已经没有运行中的 turn（如「开始执行」只启动了程序），侧栏 / 输入框 / 摘要的「执行中」都要算进来。 */
export function sessionWorkflowRunning(runs: WorkflowRunOut[] | undefined): boolean {
  return !!runs && runs.some((r) => r.status === "running");
}

/** 会话是否仍有工作流相关的后台活动：有运行中的程序，或服务端正要唤醒主代理送达工作流通知（待唤醒）。
 *
 *  plan-20261014-actions-wait：程序进入终态后，服务端要稍等再唤醒主代理（合并同一时段的通知、待会话空闲），
 *  这段空档里没有运行中的程序也没有运行中的 turn——只看前两者，各处「执行中」会回到空闲、唤醒后又恢复。
 *  workflow.wakeup 事件把这段空档标为「待唤醒」，这里并入忙碌口径。 */
export function sessionWorkflowBusy(runs: WorkflowRunOut[] | undefined, wakeupPending: boolean | undefined): boolean {
  return !!wakeupPending || sessionWorkflowRunning(runs);
}

/** zustand selector 用：按会话 id 取忙碌态（会话为空视为不忙碌）。 */
export function selectSessionWorkflowBusy(
  s: { bySession: Record<number, WorkflowRunOut[]>; wakeupPending: Record<number, boolean> },
  sessionId: number | null | undefined,
): boolean {
  return sessionId != null && sessionWorkflowBusy(s.bySession[sessionId], s.wakeupPending[sessionId]);
}

/** 会话内已启动且不在运行的程序里，最晚「停止活动」的时刻（毫秒；没有则 0）。草稿计划（没有 started_at）不算。
 *
 *  取值顺序 paused_at → finished_at → updated_at：续跑会清掉 paused_at，所以 paused_at 仍在就说明程序最后一次是
 *  停在暂停——此后即使又被取消（finished_at 更晚），暂停期间也没有在工作，终点以暂停时刻为准；
 *  完成 / 失败 / 运行中被取消取 finished_at；等待决定等都没有的回落到 updated_at。
 *
 *  用途：整轮的「已工作」用时终点——工作流在后台跑，主代理那一轮早已结束；若用户中途停止了工作流、
 *  之后也没有唤醒轮，终点只取主代理那一轮的完成时刻，用时会从走动中的数字倒退回去。 */
export function latestWorkflowEndMs(runs: WorkflowRunOut[] | undefined): number {
  let best = 0;
  for (const r of runs ?? []) {
    if (r.status === "running" || !r.started_at) continue;
    const ms = parseUtc(r.paused_at ?? r.finished_at ?? r.updated_at);
    if (ms > best) best = ms;
  }
  return best;
}
