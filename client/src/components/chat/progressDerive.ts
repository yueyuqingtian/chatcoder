/** 进程行派生（纯函数）——胶囊 / 工具卡「进程」/ 右面板「进度」的唯一口径。
 *
 * 单独成文件是为了能脱离 React 与 store 做单测（client/tests/progress-derive.test.ts）；
 * 对外仍由 `taskProgress.ts` 的 `useProgressRows` 提供，调用方不直接用它。
 *
 * ── 取哪一份清单 ──
 *  ① AI 清单（todo.updated，运行中实时）优先；② 否则取落库步骤里**最近一个含步骤的轮次**。
 *  已回滚的轮次随回滚作废，跳过它继续往前找。
 *
 * ── 这份清单处于什么阶段（phase，按清单所属轮次判定，plan-282-1492）──
 *  - running：该轮正在执行 → 实时进度，「下一项」可按运行态兜底为进行中；
 *  - interrupted：用户停止 → 未完成项呈现为中断（警示色，不转圈）；
 *  - concluded：该轮已结束 → **保留整份清单**（plan-20261025-progress-keep：用户要求一轮结束后
 *    不再收起进程，直到之后的轮次制定了新清单才刷新）。清单里残留的「进行中」先收口为待办，
 *    不再转圈——重启后不会出现「最后一步还在执行」；若该轮是正常完成、且前面每一项都已完成、
 *    只剩最后一格没打勾，则补标为完成（plan-568-2333：模型收尾常漏最后一次 todo_write，
 *    此前卡片会永远停在「N-1/N」）。
 *
 * ── 续接清单（carried）──
 *  上一轮未完成、本轮开始时由服务端广播的清单：本轮运行中如实呈现为「待续接」；
 *  若本轮结束仍未更新，服务端下一轮只看紧邻上一轮的清单、不会再续接它，此时不再标「待续接」，
 *  改按落库步骤展示（与重启后看到的一致）。
 */
import type { TaskOut, TurnOut } from "../../api/client";
import type { TodoItem } from "../../store/chat";
import { resolveOwnerRows, type OwnerRunState } from "./progressOwner";
import type { ProgressResult, ProgressRow, ProgressStatus, TurnPhase } from "./taskProgress";

export interface ProgressSource {
  /** AI 清单；调用方已按会话归属过滤（不属于当前会话时传 null） */
  todos: TodoItem[] | null;
  todosTurnId: number | null;
  todosCarried: boolean;
  tasks: TaskOut[];
  turns: Array<Pick<TurnOut, "id" | "status">>;
  runningTurnId: number | null;
  isRunning: boolean;
  /** 执行者（子代理 / 历史子会话）运行态快照 */
  owners: OwnerRunState[];
}

/** 是否为步骤任务（引擎的 group 不是步骤；request 是用户消息，也不计入进度）。 */
function isStepTask(t: TaskOut): boolean {
  return t.kind === "step" || (t.parent_task_id != null && t.kind !== "group");
}

/** 原始状态 → 展示状态。
 *  cancelled/failed 是"停止/异常后留下的未完成项"（task_service.cancel_turn_tasks 写 cancelled），
 *  语义是"还没做完、可以继续"，因此归为 interrupted（警示色），而不是 done。 */
export function normalizeStatus(status: string): ProgressStatus {
  if (status === "in_progress") return "running";
  if (status === "cancelled" || status === "failed") return "interrupted";
  if (status === "done" || status === "completed") return "done";
  if (status === "running") return "running";
  return "pending";
}

/** 展示态兜底：把"接下来要执行的那一项"标为进行中。
 *  必须由实际运行态门控（plan-282-1441）——否则"上次退出前其实已跑完、只是没来得及写终态"的清单，
 *  重启后仍显示"最后一步还在执行"。 */
function withRunningFallback(rows: ProgressRow[], activelyRunning: boolean): ProgressRow[] {
  if (rows.length === 0 || !activelyRunning) return rows;
  if (rows.some((r) => r.status === "running")) return rows;
  const idx = rows.findIndex((r) => r.status === "pending");
  if (idx < 0) return rows;
  const out = [...rows];
  out[idx] = { ...out[idx], status: "running" };
  return out;
}

/** 收口「模型漏标的最后一项」（plan-568-2333）。
 *
 *  现象：一轮已经正常做完、回复也给了，卡片却永远停在「7/8」——最后一项还是空心圆。原因是
 *  模型收尾时常常不再发一次 todo_write 把最后一步标成 completed，只留下 in_progress；
 *  而 settle 在轮次结束后把所有残留「进行中」统一收口为待办（避免伪造"还在执行"），
 *  于是这一项被永久显示成未完成。
 *
 *  判定严格限定为「前面每一格都已完成、只剩最后一格没打勾」——这才是纯粹的账没对齐；
 *  只要更早还有待办 / 中断项，就说明活确实没干完，一律不动。调用方还须保证轮次是**正常完成**
 *  （中断 / 失败 / 用户停止都不补，停止后没做完的项必须如实留着）。
 *  归属子代理的步骤也不补：那一项是否真的完成由执行者的运行态决定（见 resolveOwnerRows）。 */
function closeTrailingUnmarked(rows: ProgressRow[]): ProgressRow[] {
  if (rows.length === 0) return rows;
  const last = rows[rows.length - 1];
  // 只补「还停在执行中」的末项——模型是在做这一步的过程中收尾的，属于漏打勾。
  // 已取消 / 失败（interrupted）与从未开始的待办一律不动：前者是真的没做完，后者可能本就没打算做。
  if (last.status !== "running" || last.owner != null) return rows;
  for (let i = 0; i < rows.length - 1; i += 1) {
    if (rows[i].status !== "done") return rows;
  }
  const out = [...rows];
  out[out.length - 1] = { ...last, status: "done" };
  return out;
}

const EMPTY: ProgressResult = {
  rows: [], useTodo: false, done: 0, unfinished: 0, phase: "concluded", carried: false, turnId: null, fromPrevious: false,
};

export function deriveProgress(src: ProgressSource): ProgressResult {
  const sessionRunning = src.isRunning || src.runningTurnId != null;
  const statusOf = (turnId: number): string | undefined => src.turns.find((t) => t.id === turnId)?.status;

  /** 某轮清单的阶段。turn 行缺失（未加载 / 超出 list_turns 上限的老轮次）时：
   *  比正在运行的轮次更早的必然已结束（同一会话 turn id 单调递增）；
   *  否则会话正在跑就按 running（兼容刚发起、turn 行还没进 store 的窗口），老轮次绝不复活。 */
  const phaseOf = (turnId: number | null): TurnPhase => {
    if (turnId == null) return sessionRunning ? "running" : "concluded";
    if (src.runningTurnId === turnId) return "running";
    const st = statusOf(turnId);
    if (st === "running") return "running";
    if (st === "interrupted") return "interrupted";
    if (st != null) return "concluded";
    if (src.runningTurnId != null && turnId < src.runningTurnId) return "concluded";
    return sessionRunning ? "running" : "concluded";
  };

  const settle = (rows: ProgressRow[], useTodo: boolean, phase: TurnPhase, turnId: number | null, carried: boolean): ProgressResult => {
    // 已结束：先把清单里残留的「进行中」收口为待办，再按执行者真实运行态修正——
    // 轮次结束后仍在后台跑的子代理，它负责的那一步照常转圈。
    // plan-568-2333：收口**之前**先看这一轮是否正常完成——是的话把「前面全完成、只剩最后一格
    // 还停在执行中」补成完成（closeTrailingUnmarked）。模型收尾时经常不再补一次 todo_write，
    // 此前这一格会被永久收口成待办，卡片于是总停在「7/8」。必须在收口前判定，收口后
    // running 已变成 pending，就分不出「做着做着结束」与「本来就没开始」了。
    // 中断 / 失败 / 用户停止不补，停下来的轮次里没做完的项必须如实留着。
    let base = phase === "concluded" && turnId != null && statusOf(turnId) === "completed"
      ? closeTrailingUnmarked(rows)
      : rows;
    if (phase === "concluded") {
      base = base.map((r) => (r.status === "running" ? { ...r, status: "pending" as const } : r));
    }
    // 执行者修正必须先于兜底（plan-20261020-progress）：顺序反了，兜底会多标一项待办
    const resolved = resolveOwnerRows(base, src.owners);
    const list = phase === "interrupted"
      ? resolved.map((r) => (r.status === "running" ? { ...r, status: "interrupted" as const } : r))
      : resolved;
    const display = withRunningFallback(list, phase === "running");
    const done = display.filter((r) => r.status === "done").length;
    return {
      rows: display,
      useTodo,
      done,
      unfinished: display.length - done,
      phase,
      carried,
      turnId,
      fromPrevious: !carried && turnId != null && src.runningTurnId != null && turnId < src.runningTurnId,
    };
  };

  // ① AI 清单
  const todoItems = (src.todos ?? []).filter((t) => t.content);
  const todosVoid = src.todosTurnId != null && statusOf(src.todosTurnId) === "rolled_back";
  if (todoItems.length > 0 && !todosVoid) {
    const phase = phaseOf(src.todosTurnId);
    if (!src.todosCarried) {
      const rows: ProgressRow[] = todoItems.map((t) => ({
        key: t.content,
        title: t.content,
        note: t.activeForm || "",
        status: normalizeStatus(t.status),
        owner: t.owner ?? null,
        // plan-549-2281：清单里的 owner 由事件下发，旧数据是子会话 id——类型交给运行态快照推导
        ownerKind: null,
      }));
      return settle(rows, true, phase, src.todosTurnId, false);
    }
    if (phase !== "concluded") {
      // 续接清单不是本轮进度：未完成项一律呈现为待续接（警示色、不转圈）
      const rows: ProgressRow[] = todoItems.map((t) => ({
        key: t.content,
        title: t.content,
        note: "",
        status: normalizeStatus(t.status) === "done" ? "done" : "interrupted",
        // plan-156-619 S6：交接清单同样保留归属，否则续接后 chip 会消失
        owner: t.owner ?? null,
        ownerKind: null,
      }));
      return settle(rows, true, "interrupted", src.todosTurnId, true);
    }
    // 续接所在的这一轮已结束仍未更新：落到 ② 按落库步骤展示
  }

  // ② 落库步骤：从新到旧扫描，遇到第一个含步骤、且未被回滚的轮次立即定论
  const visible = src.tasks.filter((t) => !t.is_hidden);
  const turnIds = Array.from(
    new Set(visible.map((t) => t.turn_id).filter((id): id is number => id != null)),
  ).sort((a, b) => b - a);
  for (const turnId of turnIds) {
    if (statusOf(turnId) === "rolled_back") continue;
    const steps = visible.filter((t) => t.turn_id === turnId && isStepTask(t));
    if (steps.length === 0) continue; // 该轮没有清单（如刚建立的新 turn）→ 继续找
    const rows: ProgressRow[] = steps.map((s) => ({
      key: String(s.id),
      title: s.title,
      note: s.note || "",
      status: normalizeStatus(s.status),
      agentId: s.agent_id ?? null,
      // plan-549-2281：owner 优先取子代理 id；升级前落库的步骤只有 subsession_id，作为历史数据兜底
      owner: s.agent_id ?? s.subsession_id ?? null,
      ownerKind: s.agent_id != null ? "subagent" : (s.subsession_id != null ? "subsession" : null),
    }));
    return settle(rows, false, phaseOf(turnId), turnId, false);
  }

  return EMPTY;
}
