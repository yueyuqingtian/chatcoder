/** taskProgress —— 任务进度行的**唯一**数据源（plan-282-1441 · #3）。
 *
 * 背景：此前「输入框上方胶囊」与「右侧任务摘要面板」各有一套取数口径——
 *  - 胶囊：AI 清单优先，其次按 turn 倒序找未完成清单；
 *  - 右面板：只看 `max(turn_id)`，且无步骤时还会**回退到 `request` 任务**
 *    （而 request 的标题就是用户消息文本）。
 * 结果两处经常显示不同的步骤，用户看到"同一个任务两个说法"。
 *
 * 现在抽成本模块，两处都只用它，保证内容与状态完全一致。
 *
 * 进度只由 AI 维护（todo_write 清单 → 落库为 group+step，见
 * server/app/orchestration/tools/todo.py），**不回退到用户消息**。
 *
 * ── plan-282-1492（口径修正：结束的任务不再显示"还在跑"）──
 * 旧口径的判据是"**还有没有未完成项**"，于是只要 AI 漏标最后一项（很常见）或某次
 * todo_write 落库失败，DB 里就会留一个 running/pending 步骤；退出软件重新进入后，
 * 胶囊照样把它当"进行中进度"渲染——用户看到的是"任务明明跑完了，重启后最后一步
 * 还在执行"。现在判据改成**这一轮任务本身还活着吗**（phase）：
 *
 *   - running（该轮正在执行）→ 展示进度，"下一项"可按运行态兜底为进行中；
 *   - interrupted（用户主动停止）→ 保留未完成项的**警示展示**（不显示转圈），
 *     让用户知道还剩什么、可以继续（用户明确要求的续跑语义）；
 *   - concluded（已完成 / 失败 / 取消 / 回滚 / 待确认）→ **不展示进度**：
 *     任务已收口，残留的 running/pending 步骤只是账没对齐，不再当作进度。
 *
 * 这一条同时覆盖胶囊与右面板（两处同源），因此"重启后残留进度"不会只在某处复发。
 */
import { useMemo } from "react";
import { useChatStore } from "../../store/chat";
import type { TaskOut } from "../../api/client";

export type ProgressStatus = "pending" | "running" | "done" | "interrupted";

export interface ProgressRow {
  key: string;
  title: string;
  note: string;
  status: ProgressStatus;
  /** plan-282-1441：来源子代理 id（仅引擎步骤有）——右侧面板用它做"定位到执行消息"，
   *  输入框胶囊不使用。 */
  agentId?: number | null;
}

/** 轮次活跃态——进度"是否还活着"的唯一判据（plan-282-1492）。 */
export type TurnPhase = "running" | "interrupted" | "concluded";

/** 是否为步骤任务（引擎的 group 不是步骤；request 是用户消息，也不计入进度）。 */
function isStepTask(t: TaskOut): boolean {
  return t.kind === "step" || (t.parent_task_id != null && t.kind !== "group");
}

/** 原始状态 → 展示状态。
 *  cancelled/failed 是"停止/异常后留下的未完成项"（task_service.cancel_turn_tasks 写 cancelled），
 *  语义是"还没做完、可以继续"，因此归为 interrupted（警示色），而不是 done。 */
function normalizeStatus(status: string): ProgressStatus {
  if (status === "in_progress") return "running";
  if (status === "cancelled" || status === "failed") return "interrupted";
  if (status === "done" || status === "completed") return "done";
  if (status === "running") return "running";
  return "pending";
}

export interface ProgressResult {
  rows: ProgressRow[];
  /** 数据来自 AI 清单（todo.updated 事件），否则来自引擎步骤 */
  useTodo: boolean;
  done: number;
  /** 未完成项数量（含 running / pending / interrupted） */
  unfinished: number;
  /** 该进度归属轮次的活跃态（plan-282-1492）：concluded 时 rows 必然为空。 */
  phase: TurnPhase;
}

/** 展示态兜底：把"接下来要执行的那一项"标为进行中。
 *
 * plan-282-1441（修复）：**必须由实际运行态门控**——否则"上次退出前其实已跑完、
 * 只是没来得及写终态"的清单，在重启进入软件后仍显示"最后一步还在执行"。
 * plan-282-1492：门控参数收敛为 `phase === "running"`（内部使用；调用方不再各自传参，
 * 避免两处口径再次分叉）。
 */
function withRunningFallback(rows: ProgressRow[], activelyRunning: boolean): ProgressRow[] {
  const out = rows.map((r) => ({ ...r }));
  if (out.length === 0) return out;
  if (!activelyRunning) return out;
  if (out.some((r) => r.status === "running")) return out;
  const idx = out.findIndex((r) => r.status === "pending");
  if (idx >= 0) out[idx] = { ...out[idx], status: "running" };
  return out;
}

/** 读取当前会话的任务进度行（胶囊与右面板唯一入口）。
 *
 * plan-282-1441（#10）：步骤来源**只认"最近一个含步骤的 turn"**，不再往前回溯。
 * plan-282-1492：在此基础上按该 turn 的活跃态收口——
 *  - 该轮已完成 → 不展示（不再命中上一轮陈旧步骤，也不展示本轮的漏标项）；
 *  - 该轮被停止 → 保留未完成项（警示色），便于用户知道还剩什么、继续干。
 */
export function useProgressRows(): ProgressResult {
  const tasks = useChatStore((s) => s.tasks);
  const todos = useChatStore((s) => s.todos);
  const todosTurnId = useChatStore((s) => s.todosTurnId);
  const turns = useChatStore((s) => s.turns);
  const runningTurnId = useChatStore((s) => s.runningTurnId);
  const isRunning = useChatStore((s) => s.isRunning);

  return useMemo<ProgressResult>(() => {
    const sessionRunning = isRunning || runningTurnId != null;

    /** 判定某轮任务的活跃态。
     *  turn 行缺失（未加载 / 超出 list_turns 上限的老轮次）时：会话正在跑就按 running
     *  （兼容刚发起、turn 行还没进 store 的窗口），否则按已结束处理——老轮次绝不复活。 */
    const phaseOf = (turnId: number | null | undefined): TurnPhase => {
      if (turnId == null) return sessionRunning ? "running" : "concluded";
      if (runningTurnId === turnId) return "running";
      const t = turns.find((x) => x.id === turnId);
      if (t?.status === "running") return "running";
      if (t?.status === "interrupted") return "interrupted";
      if (t) return "concluded";
      return sessionRunning ? "running" : "concluded";
    };

    /** 按活跃态收口 + 计算展示行（所有出口共用）。 */
    const settle = (rows: ProgressRow[], useTodo: boolean, phase: TurnPhase): ProgressResult => {
      let list: ProgressRow[];
      if (phase === "concluded") {
        // 任务已结束：残留的未完成项不再当进度（这正是"重启后最后一步还在跑"的来源）
        list = [];
      } else if (phase === "interrupted") {
        // 停止后收口"进行中"假象：未完成项如实呈现为中断态（警示色、不带转圈）
        list = rows.map((r) => (r.status === "running" ? { ...r, status: "interrupted" as const } : r));
      } else {
        list = rows;
      }
      const display = withRunningFallback(list, phase === "running");
      const done = display.filter((r) => r.status === "done").length;
      return { rows: display, useTodo, done, unfinished: display.length - done, phase };
    };

    // ① AI 清单优先（todo.updated 事件，运行中实时）
    const todoItems = Array.isArray(todos) ? todos.filter((t) => t.content) : [];
    if (todoItems.length > 0) {
      const rows: ProgressRow[] = todoItems.map((t) => ({
        key: t.content,
        title: t.content,
        note: t.activeForm || "",
        status: normalizeStatus(t.status),
      }));
      return settle(rows, true, phaseOf(todosTurnId));
    }

    // ② 引擎步骤：从新到旧扫描，遇到第一个含 step 的 turn 立即定论
    const visible = tasks.filter((t) => !t.is_hidden);
    const turnIds = Array.from(
      new Set(visible.map((t) => t.turn_id).filter((id): id is number => id != null)),
    ).sort((a, b) => b - a);

    for (const turnId of turnIds) {
      const stepList = visible.filter((t) => t.turn_id === turnId && isStepTask(t));
      if (stepList.length === 0) continue; // 该轮没有清单（如刚建立的新 turn）→ 继续找
      const rows: ProgressRow[] = stepList.map((s) => ({
        key: String(s.id),
        title: s.title,
        note: s.note || "",
        status: normalizeStatus(s.status),
        agentId: s.agent_id ?? null,
      }));
      return settle(rows, false, phaseOf(turnId));
    }

    return { rows: [], useTodo: false, done: 0, unfinished: 0, phase: "concluded" };
  }, [tasks, todos, todosTurnId, turns, runningTurnId, isRunning]);
}
