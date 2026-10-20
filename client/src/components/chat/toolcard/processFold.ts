/** 进程栏的动态折叠（plan-486-2114）。
 *
 * 【为什么要改】旧算法（plan-20261019-toolcard-v2）按「展示优先级」挑出一组**散点**可见项，
 * 再把折叠行插在第一个被折叠项的位置。子会话并行执行时，清单会出现「已完成 / 进行中」交错
 * （子会话步骤按计划插在中间，且 progressOwner 会按子会话真实运行态把未完成项改成 running），
 * 于是「已完成 N 项」会夹在两个进行中步骤之间，任务先后顺序被切断。
 *
 * 【新口径】折叠只作用于顺序上连续的一段，且只允许两个边界段：
 *   · 前缀已完成段：从第 1 行起连续 done —— 从最早的开始折，保留最近完成的，折叠行在最顶部；
 *   · 后缀未开始段：到最后一行止连续 pending —— 从最晚的开始折，保留最早待办的，折叠行在最底部。
 * 中段永不折叠（被 running / interrupted 隔开的 done 或 pending 段、以 pending 开头或
 * 以 done 结尾的顺序异常段），因此折叠行绝不会夹在两个未折叠步骤之间。
 *
 * 【折叠量】枚举「前缀折叠数 × 后缀折叠数」，在「未折叠项 ≥ 5 且总行数 ≤ 7」的可行组合里
 * 按「少折待办 → 少折已完成」取一组：待办最后才牺牲，已完成的可见性最低（原口径「先折已完成」）。
 * 若中段步骤本身就超限、首尾折尽仍放不下，则取折叠量最大的一组并允许超出 7 行——
 * 顺序正确优先于限高，由卡片体滚动兜底。
 *
 * 纯函数，无 DOM / store 依赖，便于推理与单测（client/tests/process-fold.test.ts）。
 */
import type { ProgressRow } from "../taskProgress";

/** 未折叠项的最低保证数（用户口径：未折叠项目保证展示 5 个的高度）。 */
export const PROCESS_MIN_VISIBLE = 5;
/** 折叠行最多两个（已完成 / 待处理各一个），不计入上面的保证数。 */
export const PROCESS_MAX_FOLD_ROWS = 2;
/** 进程列表的限高（行数）：5 个未折叠项 + 最多 2 个折叠行。 */
export const PROCESS_MAX_ROWS = PROCESS_MIN_VISIBLE + PROCESS_MAX_FOLD_ROWS;

export type FoldKind = "done" | "todo";

export type ProcessEntry =
  | { kind: "row"; row: ProgressRow }
  | {
    kind: "fold";
    fold: FoldKind;
    rows: ProgressRow[];
    /** 被折叠项在原列表中的下标区间（0-based，闭区间）——折叠行浮窗据此显示「第 X–Y 步」。 */
    range: [number, number];
  };

/** 折叠行文案：「已完成 4 项」/「待处理 2 项」。 */
export function foldLabel(fold: FoldKind, count: number): string {
  return `${fold === "done" ? "已完成" : "待处理"} ${count} 项`;
}

/** 折叠方案：折掉前缀 countH 项已完成、后缀 countT 项待处理。 */
interface FoldPlan {
  countH: number;
  countT: number;
}

/** 从第 1 行起连续 done 的项数。 */
function headDoneCount(rows: ProgressRow[]): number {
  let i = 0;
  while (i < rows.length && rows[i].status === "done") i += 1;
  return i;
}

/** 从末尾向前连续 pending 的项数（不与前缀已完成段重叠）。 */
function tailPendingCount(rows: ProgressRow[], headDone: number): number {
  const n = rows.length;
  let i = 0;
  while (i < n - headDone && rows[n - 1 - i].status === "pending") i += 1;
  return i;
}

/** 枚举合法组合：单项折叠不省高度（折叠行本身占一行），所以折叠数只能是 0 或 ≥ 2。 */
function foldPlans(headDone: number, tailTodo: number): FoldPlan[] {
  const plans: FoldPlan[] = [{ countH: 0, countT: 0 }];
  for (let h = 0; h <= headDone; h += 1) {
    for (let t = 0; t <= tailTodo; t += 1) {
      if (h === 0 && t === 0) continue;
      if (h === 1 || t === 1) continue;
      plans.push({ countH: h, countT: t });
    }
  }
  return plans;
}

/** 折叠后占用的行数：未折叠项 + 各折叠行（每个最多一行）。 */
function heightOf(n: number, plan: FoldPlan): number {
  return n - plan.countH - plan.countT
    + (plan.countH > 0 ? 1 : 0)
    + (plan.countT > 0 ? 1 : 0);
}

/** 折叠的总项数。 */
const foldedOf = (plan: FoldPlan): number => plan.countH + plan.countT;

/** 折叠行数（0 / 1 / 2）。 */
const foldRowsOf = (plan: FoldPlan): number => (plan.countH > 0 ? 1 : 0) + (plan.countT > 0 ? 1 : 0);

/** 兜底方案的偏好：折叠量最大 → 折叠行最少（高度更低）→ 先折已完成 → 待办折得少。 */
function betterFallback(next: FoldPlan, cur: FoldPlan): boolean {
  const dFold = foldedOf(next) - foldedOf(cur);
  if (dFold !== 0) return dFold > 0;
  const dRows = foldRowsOf(next) - foldRowsOf(cur);
  if (dRows !== 0) return dRows < 0;
  if (next.countH !== cur.countH) return next.countH > cur.countH;
  return next.countT < cur.countT;
}

/** 可行方案的偏好：先少折待办（保住「接下来做什么」），再少折已完成（折叠不多不少）。
 *
 * 【为什么不按「可见项最多」排序】两者冲突时（`dddr pppppppp`：折 6 个待办能换回 3 个已完成，
 * 折 3 个已完成 + 4 个待办则可见数少 1），可见数最多的方案恰恰把用户最需要的待办藏了起来。
 * 原口径「先折已完成、仍放不下再折最靠后的待办」的实质就是**待办最后才牺牲**，这里按它收口。
 */
function betterFeasible(next: FoldPlan, cur: FoldPlan): boolean {
  if (next.countT !== cur.countT) return next.countT < cur.countT;
  return next.countH < cur.countH;
}

export function foldProcessRows(rows: ProgressRow[], maxRows: number = PROCESS_MAX_ROWS): ProcessEntry[] {
  const n = rows.length;
  if (n <= maxRows) return rows.map((row) => ({ kind: "row", row }));

  // 限高连「保证可见数」都放不下时不折叠：折叠无法达成限高，只会白藏信息。
  if (maxRows <= PROCESS_MIN_VISIBLE) return rows.map((row) => ({ kind: "row", row }));

  const headDone = headDoneCount(rows);
  const tailTodo = tailPendingCount(rows, headDone);
  const minVisible = Math.min(PROCESS_MIN_VISIBLE, n);

  let feasible: FoldPlan | null = null;
  let fallback: FoldPlan | null = null;
  for (const plan of foldPlans(headDone, tailTodo)) {
    const visible = n - foldedOf(plan);
    if (visible < minVisible) continue;
    if (heightOf(n, plan) <= maxRows) {
      if (!feasible || betterFeasible(plan, feasible)) feasible = plan;
    }
    if (!fallback || betterFallback(plan, fallback)) fallback = plan;
  }

  const chosen = feasible ?? fallback ?? { countH: 0, countT: 0 };
  const { countH, countT } = chosen;
  if (countH === 0 && countT === 0) return rows.map((row) => ({ kind: "row", row }));

  const out: ProcessEntry[] = [];
  if (countH > 0) {
    out.push({
      kind: "fold", fold: "done", rows: rows.slice(0, countH), range: [0, countH - 1],
    });
  }
  for (let i = countH; i < n - countT; i += 1) out.push({ kind: "row", row: rows[i] });
  if (countT > 0) {
    out.push({
      kind: "fold", fold: "todo", rows: rows.slice(n - countT), range: [n - countT, n - 1],
    });
  }
  return out;
}
