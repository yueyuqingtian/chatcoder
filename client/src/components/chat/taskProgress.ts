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
 * 判据是**这一轮任务本身还活着吗**（phase），而不是"还有没有未完成项"：AI 漏标最后一项
 * （很常见）时，DB 里会留一个 running 步骤，旧口径重启后照样把它当进行中渲染。
 *
 *   - running（该轮正在执行）→ 展示进度，"下一项"可按运行态兜底为进行中；
 *   - interrupted（用户主动停止）→ 保留未完成项的**警示展示**（不显示转圈），
 *     让用户知道还剩什么、可以继续（用户明确要求的续跑语义）；
 *   - concluded（已完成 / 失败 / 取消 / 待确认）→ plan-20261025-progress-keep 起**保留整份清单**：
 *     一轮结束后进程不再收起，直到之后的轮次制定了新清单才刷新；残留的「进行中」收口为待办、不转圈。
 *     已回滚的轮次随回滚作废，不参与取数。
 *
 * 派生本身是纯函数（progressDerive.ts），这里只负责从 store 取数与记忆化。
 * 这一条同时覆盖胶囊、工具卡与右面板（同源），因此口径不会只在某处分叉。
 */
import { useMemo, useRef } from "react";
import { useChatStore } from "../../store/chat";
import { resolveOwnerRows, resolveSubsessionRows, type OwnerRunState } from "./progressOwner";
import { deriveProgress } from "./progressDerive";

export type ProgressStatus = "pending" | "running" | "done" | "interrupted";

export interface ProgressRow {
  key: string;
  title: string;
  note: string;
  status: ProgressStatus;
  /** plan-282-1441：来源子代理 id（仅引擎步骤有）——右侧面板用它做"定位到执行消息"。 */
  agentId?: number | null;
  /** plan-549-2281：执行该步骤的子代理 id；null/缺省 = 主代理自己执行。
   *  升级前的历史步骤这里存的是子会话 id（前端无法凭数字区分），ownerKind 用来标明类型；
   *  进程行据此显示归属 chip，只有在确实能跳转时才提供点击。 */
  owner?: number | null;
  /** plan-549-2281：owner 的类型——subagent（新数据）/ subsession（升级前的历史数据）。
   *  缺省时按运行态快照推导，两都推不出来就不加 chip。 */
  ownerKind?: "subagent" | "subsession" | null;
}

/** 轮次活跃态——进度"是否还活着"的唯一判据（plan-282-1492）。 */
export type TurnPhase = "running" | "interrupted" | "concluded";

export interface ProgressResult {
  rows: ProgressRow[];
  /** 数据来自 AI 清单（todo.updated 事件），否则来自引擎步骤 */
  useTodo: boolean;
  done: number;
  /** 未完成项数量（含 running / pending / interrupted） */
  unfinished: number;
  /** 该清单所属轮次的活跃态（plan-282-1492）。concluded 时清单照常保留（plan-20261025-progress-keep）。 */
  phase: TurnPhase;
  /** 续接清单：上一轮未完成、本轮模型尚未更新（界面统一标「待续接」，不再靠逐行备注识别） */
  carried: boolean;
  /** 清单所属轮次（无清单为 null） */
  turnId: number | null;
  /** 清单来自比正在运行的轮次更早、已结束的轮次：本轮还没制定新清单，暂时沿用之前的（续接清单不算） */
  fromPrevious: boolean;
}

/** 执行者运行态修正（plan-20261020-progress）：实现放在无依赖的纯模块里，
 *  由本模块统一对外导出——胶囊 / 右面板 / 工具卡仍只需认 `taskProgress`。
 *  plan-549-2281：owner 语义从子会话改为子代理，旧名保留导出（`client/tests` 引用旧名）。 */
export { resolveOwnerRows, resolveSubsessionRows };
export type { OwnerRunState, SubsessionRunState } from "./progressOwner";

/** 读取当前会话的任务进度行（胶囊、工具卡与右面板唯一入口）。
 *
 * plan-282-1441（#10）：步骤来源**只认"最近一个含步骤的 turn"**。
 * plan-20261025-progress-keep：该轮结束后清单保留，之后的轮次制定新清单才替换；具体口径见 progressDerive.ts。
 */
export function useProgressRows(enabled = true): ProgressResult {
  const tasks = useChatStore((s) => s.tasks);
  const todos = useChatStore((s) => s.todos);
  const todosTurnId = useChatStore((s) => s.todosTurnId);
  const todosCarried = useChatStore((s) => s.todosCarried);
  const turns = useChatStore((s) => s.turns);
  const runningTurnId = useChatStore((s) => s.runningTurnId);
  const isRunning = useChatStore((s) => s.isRunning);
  /** plan-75-334 阶段3：不可见（面板隐藏）期间的冻结结果。 */
  const frozenRef = useRef<ProgressResult | null>(null);

  // ── 本会话执行者（子代理）的运行态（plan-20261020-progress · plan-549-2281）──
  const currentSessionId = useChatStore((s) => s.currentSessionId);
  /** plan-20261023-ownerfix：AI 清单的归属会话（事件到达时记录）。 */
  const todosSessionId = useChatStore((s) => s.todosSessionId);
  /** 冻结结果只对生成它的会话有效：切换会话必须丢弃，否则面板隐藏期间的旧结果
   *  会在新会话里继续展示（用户看到「新开会话还是上一个会话的进程」）。 */
  const frozenSidRef = useRef<number | null>(null);
  if (frozenSidRef.current !== currentSessionId) {
    frozenRef.current = null;
    frozenSidRef.current = currentSessionId;
  }
  const subsessionsByParent = useChatStore((s) => s.subsessionsByParent);
  const subagentMeta = useChatStore((s) => s.subagentMeta);
  /** 「类型:id:运行态」签名：只在执行者新增 / 开始 / 结束时变化，内部的每条 delta 都不变。
   *  用它当进度派生的记忆化键——进度不需要随每条流式消息重算。 */
  const ownerSig = useMemo(() => {
    const subs = currentSessionId != null ? subsessionsByParent[currentSessionId] : null;
    const parts: string[] = [];
    for (const [id, m] of Object.entries(subagentMeta)) {
      parts.push(`a${id}:${m.status === "running" || m.status === "in_progress" ? 1 : 0}`);
    }
    for (const c of subs ?? []) parts.push(`s${c.id}:${c.has_running ? 1 : 0}`);
    return parts.sort().join(",");
  }, [subagentMeta, subsessionsByParent, currentSessionId]);
  /** 运行态快照：签名未变时进度不重算，但 settle 里仍要用**当前**的值。
   *  plan-549-2281：子代理为准，历史子会话作兜——升级前落库的步骤 owner 存的是子会话 id，
   *  这里把两者都喂给纯函数，历史步骤才能继续转圈 / 继续显示归属。
   *  顺序有讲究：子代理放在后面（后写入覆盖），两者 id 数值撞车时以子代理为准。 */
  const ownersRef = useRef<OwnerRunState[]>([]);
  ownersRef.current = [
    ...(currentSessionId != null ? subsessionsByParent[currentSessionId] ?? [] : [])
      .map((c) => ({ id: c.id, running: Boolean(c.has_running), kind: "subsession" as const })),
    ...Object.entries(subagentMeta).map(([id, m]) => ({
      id: Number(id),
      running: m.status === "running" || m.status === "in_progress",
      kind: "subagent" as const,
    })),
  ];

  return useMemo<ProgressResult>(() => {
    // plan-75-334 阶段3：enabled=false 时直接复用上次结果，跳过任务扫描与 turn 查找——
    // 右侧面板隐藏标签不再随主会话每个 delta 重算进度；恢复可见时补算一次。
    if (!enabled && frozenRef.current) return frozenRef.current;
    const result = deriveProgress({
      // plan-20261023-ownerfix：清单必须属于当前会话——切换瞬间到达的旧会话事件会写进
      // 新会话的 todos，此时宁可不用（回退到本会话的落库步骤），也不能把旧进度当成本轮进度。
      todos: Array.isArray(todos) && todosSessionId === currentSessionId ? todos : null,
      todosTurnId,
      todosCarried,
      tasks,
      turns,
      runningTurnId,
      isRunning,
      // 签名（ownerSig）未变时不重算，但派生时要用**当前**的运行态快照
      owners: ownersRef.current,
    });
    // 记录本次结果作为面板隐藏期的冻结值
    frozenRef.current = result;
    return result;
  }, [tasks, todos, todosSessionId, todosTurnId, todosCarried, turns, runningTurnId, isRunning, enabled, ownerSig, currentSessionId]);
}
