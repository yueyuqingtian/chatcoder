/** 进程栏的动态折叠（plan-20261019-toolcard-v2）。
 *
 * 【口径（用户原话）】
 *  · 每项一行、行高固定，所以容量按「行数」计，不需要测量 DOM；
 *  · 没超限高就不折叠；超出后**先折已完成**，仍放不下再折**最靠后**的待办；
 *  · 保证「正在执行 > 即将执行 > 靠后执行 > 已完成」的展示优先级，折叠量不多不少（刚好达到限高）；
 *  · 未折叠项保证展示 5 个的高度——折叠行本身不计入这 5 个，所以限高 = 5 + 最多 2 个折叠行 = 7 行。
 *
 * 【算法】给每一行打优先级（进行中 / 中断 → 待办按离当前近→远 → 已完成按离当前近→远），
 * 取「可见项 + 折叠行 ≤ 限高」时**最大的可见项数** k：可见 = 优先级最高的 k 项，其余按「已完成 / 待处理」
 * 各折成一行（折叠行最多两个，各自占一行高度）。进行中 / 中断项永不折叠（并行步骤再多也都可见，
 * 此时列表容器内部滚动兜底）。最后把「只含一项」的折叠行还原成该项本身——折叠一项并不省高度。
 * 输出保持原有顺序：折叠行出现在它第一个被折叠项的位置（已完成通常在最上，待处理通常在最下）。
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
  | { kind: "fold"; fold: FoldKind; rows: ProgressRow[] };

/** 折叠行文案：「已完成 4 项」/「待处理 2 项」。 */
export function foldLabel(fold: FoldKind, count: number): string {
  return `${fold === "done" ? "已完成" : "待处理"} ${count} 项`;
}

/** 必须始终可见的行：进行中与中断（尚未做完且需要用户注意）。 */
const isPinned = (r: ProgressRow): boolean => r.status === "running" || r.status === "interrupted";

/** 按「展示优先级」把行下标排好序（靠前 = 更该留在屏幕上）。 */
function priorityOrder(rows: ProgressRow[]): number[] {
  const n = rows.length;
  // 当前焦点：第一个进行中；没有则第一个未完成；全部完成则落在末尾之后（最近完成的最重要）
  let focus = rows.findIndex((r) => r.status === "running");
  if (focus < 0) focus = rows.findIndex((r) => r.status !== "done");
  if (focus < 0) focus = n;

  const cls = (r: ProgressRow): number => (isPinned(r) ? 0 : r.status === "done" ? 2 : 1);
  /** 同类内的次序键：待办 = 焦点之后由近及远，再是焦点之前（乱序）由近及远；已完成 = 离焦点由近及远 */
  const key = (r: ProgressRow, i: number): number => {
    const c = cls(r);
    if (c === 0) return i;
    if (c === 1) return i >= focus ? i - focus : n + (focus - i);
    return Math.abs(i - focus);
  };
  return rows
    .map((r, i) => ({ i, c: cls(r), k: key(r, i) }))
    .sort((a, b) => a.c - b.c || a.k - b.k || a.i - b.i)
    .map((x) => x.i);
}

/** 选前 k 项可见时的占用行数：可见项 + 已完成折叠行（若有）+ 待处理折叠行（若有）。 */
function costOf(rows: ProgressRow[], order: number[], k: number): number {
  let hasDone = false;
  let hasTodo = false;
  for (let j = k; j < order.length; j += 1) {
    if (rows[order[j]].status === "done") hasDone = true;
    else hasTodo = true;
  }
  return k + (hasDone ? 1 : 0) + (hasTodo ? 1 : 0);
}

export function foldProcessRows(rows: ProgressRow[], maxRows: number = PROCESS_MAX_ROWS): ProcessEntry[] {
  const n = rows.length;
  if (n <= maxRows) return rows.map((row) => ({ kind: "row", row }));

  const order = priorityOrder(rows);
  const pinned = rows.filter(isPinned).length;
  let k = Math.min(n, maxRows);
  while (k > pinned && costOf(rows, order, k) > maxRows) k -= 1;
  k = Math.max(k, pinned); // 进行中 / 中断项永不折叠：再多也要可见（容器内部滚动兜底）

  const visible = new Set<number>(order.slice(0, k));
  const foldedDone: number[] = [];
  const foldedTodo: number[] = [];
  for (let i = 0; i < n; i += 1) {
    if (visible.has(i)) continue;
    (rows[i].status === "done" ? foldedDone : foldedTodo).push(i);
  }
  // 只含一项的折叠行不省高度（折叠行本身占一行）：还原成该项
  if (foldedDone.length === 1) visible.add(foldedDone.pop() as number);
  if (foldedTodo.length === 1) visible.add(foldedTodo.pop() as number);

  const doneAt = foldedDone.length > 0 ? foldedDone[0] : -1;
  const todoAt = foldedTodo.length > 0 ? foldedTodo[0] : -1;
  const out: ProcessEntry[] = [];
  for (let i = 0; i < n; i += 1) {
    if (visible.has(i)) out.push({ kind: "row", row: rows[i] });
    else if (i === doneAt) out.push({ kind: "fold", fold: "done", rows: foldedDone.map((j) => rows[j]) });
    else if (i === todoAt) out.push({ kind: "fold", fold: "todo", rows: foldedTodo.map((j) => rows[j]) });
  }
  return out;
}
