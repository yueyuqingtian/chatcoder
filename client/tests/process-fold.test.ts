/**
 * 进程栏动态折叠算法测试（plan-486-2114）。
 * 纯逻辑，Node 原生 TS 运行：`node tests/process-fold.test.ts`（client 目录）。
 *
 * 口径：折叠只作用于首尾连续段——前缀连续 done（从第 1 行起）与后缀连续 pending（到最后一行止）；
 * 中段步骤永不折叠，因此折叠行只会出现在首位或末位。顺序正确优先于限高：中段过多时允许超出 7 行，
 * 由卡片体滚动兜底。其余：未折叠项至少 5 个；进行中 / 中断项永不折叠；折叠数只能是 0 或 ≥ 2。
 */
import {
  foldLabel, foldProcessRows, PROCESS_MAX_ROWS, PROCESS_MIN_VISIBLE, type ProcessEntry,
} from "../src/components/chat/toolcard/processFold.ts";
import type { ProgressRow, ProgressStatus } from "../src/components/chat/taskProgress.ts";

let failures = 0;
function assert(cond: boolean, msg: string) {
  if (!cond) { failures++; console.error(`FAIL: ${msg}`); }
  else { console.log(`ok: ${msg}`); }
}
function eq(actual: unknown, expected: unknown, msg: string) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a !== e) { failures++; console.error(`FAIL: ${msg}\n   actual:   ${a}\n   expected: ${e}`); }
  else { console.log(`ok: ${msg}`); }
}

/** 用单字母描述一串步骤：d 已完成 / r 进行中 / p 待办 / i 中断 */
function mk(spec: string): ProgressRow[] {
  const st: Record<string, ProgressStatus> = { d: "done", r: "running", p: "pending", i: "interrupted" };
  return [...spec].map((c, i) => ({ key: String(i), title: `${c}${i}`, note: "", status: st[c] }));
}

/** 折叠结果的简写：行用标题，折叠行用 `[done:N@起-止]` / `[todo:N@起-止]`（含原列表下标区间） */
function shape(entries: ProcessEntry[]): string[] {
  return entries.map((e) => (e.kind === "row" ? e.row.title : `[${e.fold}:${e.rows.length}@${e.range[0]}-${e.range[1]}]`));
}
const rowsOf = (entries: ProcessEntry[]) => entries.filter((e) => e.kind === "row").length;
const height = (entries: ProcessEntry[]) => entries.length;
/** 标题里的原列表下标（mk 生成 `${状态}${下标}`）。 */
const idx = (title: string) => Number(title.slice(1));
const foldEntries = (entries: ProcessEntry[]) =>
  entries.filter((e): e is Extract<ProcessEntry, { kind: "fold" }> => e.kind === "fold");

// ① 常量口径
eq([PROCESS_MIN_VISIBLE, PROCESS_MAX_ROWS], [5, 7], "5 个未折叠项 + 最多 2 个折叠行 = 限高 7 行");

// ② 未超限不折叠（含恰好 7 行）
eq(shape(foldProcessRows(mk("dddrppp"))), ["d0", "d1", "d2", "r3", "p4", "p5", "p6"], "7 行刚好放下：不折叠");
eq(shape(foldProcessRows([])), [], "空列表");
eq(shape(foldProcessRows(mk("r"))), ["r0"], "单行");

// ③ 超出：先折最早的已完成，折叠量刚好达到限高
{
  const out = foldProcessRows(mk("ddddrppp")); // 8 行：4 完成 + 1 进行中 + 3 待办
  eq(shape(out), ["[done:2@0-1]", "d2", "d3", "r4", "p5", "p6", "p7"], "8 行：只折最早的 2 个已完成，占满 7 行");
  assert(height(out) === 7, "高度恰好等于限高");
  assert(rowsOf(out) >= PROCESS_MIN_VISIBLE, "未折叠项 ≥ 5");
}
{
  const out = foldProcessRows(mk("dddddd" + "r" + "ppppp")); // 12 行：6 完成 + 进行中 + 5 待办
  eq(shape(out), ["[done:6@0-5]", "r6", "p7", "p8", "p9", "p10", "p11"], "已完成全部折叠后恰好放下，待办不动");
}

// ④ 已完成折光仍放不下：再折最靠后的待办；进行中与即将执行优先保留
{
  const out = foldProcessRows(mk("ddd" + "r" + "pppppppp")); // 12 行：3 完成 + 进行中 + 8 待办
  eq(shape(out), ["[done:3@0-2]", "r3", "p4", "p5", "p6", "p7", "[todo:4@8-11]"], "先折已完成，再折最靠后的 4 个待办");
  assert(height(out) === 7 && rowsOf(out) === 5, "折叠后正好 5 个未折叠项 + 2 个折叠行");
  const [doneFold, todoFold] = foldEntries(out);
  assert(!!doneFold && !!todoFold, "首尾各有一个折叠行");
  assert(todoFold.rows.map((r) => r.title).join() === "p8,p9,p10,p11", "待处理折叠行按原顺序收纳被折叠项");
}

// ⑤ 已完成里离当前近的后折（最近完成的留在屏幕上）
{
  const out = foldProcessRows(mk("dddddd" + "r" + "pp")); // 9 行
  // 折 2 个只能降到 8 行（折叠行本身占一行），所以要折 3 个才刚好 7 行
  eq(shape(out), ["[done:3@0-2]", "d3", "d4", "d5", "r6", "p7", "p8"], "9 行：折最早的 3 个已完成，近的留下，刚好 7 行");
}

// ⑥ 只含一项的折叠行还原成该项本身（折叠一项不省高度）
{
  const out = foldProcessRows(mk("d" + "r" + "pppppp")); // 8 行：1 完成 + 进行中 + 6 待办
  eq(shape(out), ["d0", "r1", "p2", "p3", "p4", "p5", "[todo:2@6-7]"], "已完成只剩 1 项时直接显示，不为它做折叠行");
  assert(height(out) === 7, "高度仍不超限");
}

// ⑦ 全部完成：按最近完成优先
{
  const out = foldProcessRows(mk("ddddddddd")); // 9 行全完成
  eq(shape(out), ["[done:3@0-2]", "d3", "d4", "d5", "d6", "d7", "d8"], "全部完成：折最早的，最近完成的留下");
}

// ⑧ 进行中 / 中断永不折叠，即使并行步骤很多
{
  const out = foldProcessRows(mk("rrrrrrrrrr")); // 10 个进行中
  assert(out.every((e) => e.kind === "row") && out.length === 10, "10 个进行中全部可见（容器内部滚动兜底）");
  const out2 = foldProcessRows(mk("ddd" + "i" + "pppppp"));
  assert(shape(out2).includes("i3"), "中断项不被折叠");
  assert(height(out2) <= PROCESS_MAX_ROWS, "中断场景同样不超限");
  assert(foldEntries(out2).every((e) => e.rows.every((r) => r.status !== "interrupted")), "折叠行内不含中断项");
}

// ⑨ 乱序 `pppp r dddd`：首尾都不是可折段 → 顺序优先，不折叠也不限高
{
  const out = foldProcessRows(mk("pppp" + "r" + "dddd")); // 待办在前、进行中居中、已完成在后（异常顺序）
  eq(shape(out), ["p0", "p1", "p2", "p3", "r4", "d5", "d6", "d7", "d8"], "首尾均非连续可折段：9 行原样返回");
  assert(out.every((e) => e.kind === "row"), "乱序场景不折叠");
  assert(height(out) === 9, "允许超出限高：顺序完整优先于限高");
}

// ⑩ 折叠行内容与标签
{
  const out = foldProcessRows(mk("ddddrppp"));
  const fold = out[0];
  assert(fold.kind === "fold" && fold.fold === "done" && fold.rows.length === 2, "已完成折叠行收纳 2 项");
  assert(fold.kind === "fold" && fold.range[0] === 0 && fold.range[1] === 1, "折叠行区间从第 1 行起");
  eq(foldLabel("done", 4), "已完成 4 项", "已完成文案");
  eq(foldLabel("todo", 1), "待处理 1 项", "待处理文案");
}

// ⑪ 自定义限高：maxRows 不大于保证可见数时限高无意义，一律不折叠
{
  const out = foldProcessRows(mk("ddrpp"), 4); // 5 行，限高 4 ≤ PROCESS_MIN_VISIBLE
  eq(shape(out), ["d0", "d1", "r2", "p3", "p4"], "限高 4：不折叠，5 行顺序不变");
  assert(out.every((e) => e.kind === "row"), "maxRows ≤ 5 时不产生折叠行");
}

// ⑫ 新增：首尾折叠与中段顺序
{
  // 无边界已完成段，只有末尾连续待办 → 折叠行只在末尾
  const a = foldProcessRows(mk("rdrdrdpppp"));
  eq(shape(a), ["r0", "d1", "r2", "d3", "r4", "d5", "[todo:4@6-9]"], "rdrdrdpppp：折叠行只在末尾，前 6 行顺序不动");
  assert(foldEntries(a).every((e, i, arr) => i === arr.length - 1 && e.fold === "todo"), "折叠行是唯一且位于末尾的待处理行");
  assert(height(a) === 7 && rowsOf(a) === 6, "7 行、6 个未折叠项");
}
{
  // 已完成夹在中间（首行是进行中）→ 中段 done 全部可见
  const b = foldProcessRows(mk("rddddrpppppp"));
  eq(shape(b), ["r0", "d1", "d2", "d3", "d4", "r5", "[todo:6@6-11]"], "rddddrpppppp：中段 4 个 done 全部可见，只折末尾待办");
  const visibleDone = b.filter((e) => e.kind === "row" && e.row.status === "done").length;
  assert(visibleDone === 4, "中段 done 一个都没被折叠");
  assert(foldEntries(b).length === 1 && b[b.length - 1].kind === "fold", "折叠行只在末尾");
}
{
  // 首尾都是可折段 → 两个折叠行分别落在首尾，done 在前、todo 在后
  const c = foldProcessRows(mk("ddrddrpppp"));
  eq(shape(c), ["[done:2@0-1]", "r2", "d3", "d4", "r5", "p6", "[todo:3@7-9]"], "ddrddrpppp：done 折首、todo 折尾，中间 5 项可见");
  const folds = foldEntries(c);
  assert(folds.length === 2, "两个折叠行");
  assert(c[0].kind === "fold" && c[0].fold === "done" && c[0].range[0] === 0, "首位是已完成折叠行");
  assert(c[c.length - 1].kind === "fold" && (c[c.length - 1] as { fold?: string }).fold === "todo", "末位是待处理折叠行");
  assert(height(c) === 7 && rowsOf(c) === 5, "高度 7、可见 5 项");
}
{
  // 中断项阻断后缀待办段 → 只折末尾连续待办
  const d = foldProcessRows(mk("ppippppp"));
  eq(shape(d), ["p0", "p1", "i2", "p3", "p4", "p5", "[todo:2@6-7]"], "ppippppp：中断阻断，只折末尾 2 个连续待办");
  const [todo] = foldEntries(d);
  assert(!!todo && todo.rows.map((r) => r.title).join() === "p6,p7", "折叠内容从末尾往前连续取");
  assert(d.some((e) => e.kind === "row" && e.row.title === "i2"), "中断项保持可见");
}
{
  // 9 个待办：从最晚的开始折，保留最早的待办
  const e = foldProcessRows(mk("ppppppppp"));
  eq(shape(e), ["p0", "p1", "p2", "p3", "p4", "p5", "[todo:3@6-8]"], "ppppppppp：折末尾 3 个，保留最早 6 个待办");
  const [todo] = foldEntries(e);
  assert(!!todo && todo.range[1] === 8, "折叠段一直延伸到末行");
  assert(e[0].kind === "row" && e[0].row.title === "p0", "最早的待办 p0 保留在顶部");
}
{
  // 无边界段：全部交错 → 不折叠、不截断，允许超限
  const f = foldProcessRows(mk("rdrdrdrdrdrd"));
  eq(shape(f), [
    "r0", "d1", "r2", "d3", "r4", "d5", "r6", "d7", "r8", "d9", "r10", "d11",
  ], "rdrdrdrdrdrd：无边界连续段，12 行原样返回");
  assert(f.every((e) => e.kind === "row"), "不折叠");
  assert(height(f) === 12 && height(f) > PROCESS_MAX_ROWS, "允许超出限高");
}

// ⑬ 不变量扫描：折叠只落在首尾、内容连续无丢项；存在可行组合时高度不超限
{
  /** 从第 1 行起连续 done 的项数。 */
  const headDoneOf = (rows: ProgressRow[]) => {
    let i = 0;
    while (i < rows.length && rows[i].status === "done") i += 1;
    return i;
  };
  /** 从末尾向前连续 pending 的项数（不与前缀已完成段重叠）。 */
  const tailTodoOf = (rows: ProgressRow[], headDone: number) => {
    const n = rows.length;
    let i = 0;
    while (i < n - headDone && rows[n - 1 - i].status === "pending") i += 1;
    return i;
  };
  /** 枚举合法折叠组合（单项折叠不省高度，故 countH / countT 不能为 1）。 */
  const plansOf = (headDone: number, tailTodo: number): Array<[number, number]> => {
    const plans: Array<[number, number]> = [[0, 0]];
    for (let h = 0; h <= headDone; h += 1) {
      for (let t = 0; t <= tailTodo; t += 1) {
        if ((h === 0 && t === 0) || h === 1 || t === 1) continue;
        plans.push([h, t]);
      }
    }
    return plans;
  };
  /** 是否存在「可见项 ≥ 5 且高度 ≤ 7」的可行组合。 */
  const hasFeasible = (rows: ProgressRow[]) => {
    const n = rows.length;
    const minVisible = Math.min(PROCESS_MIN_VISIBLE, n);
    const headDone = headDoneOf(rows);
    const tailTodo = tailTodoOf(rows, headDone);
    return plansOf(headDone, tailTodo).some(([h, t]) => {
      const visible = n - h - t;
      const hgt = visible + (h > 0 ? 1 : 0) + (t > 0 ? 1 : 0);
      return visible >= minVisible && hgt <= PROCESS_MAX_ROWS;
    });
  };

  let checked = 0;
  let overflow = 0;
  const alphabet = ["d", "r", "p", "i"];
  for (let n = 1; n <= 12; n += 1) {
    for (let s = 0; s < 120; s += 1) {
      // 伪随机但可复现
      let x = (n * 7919 + s * 104729) >>> 0;
      let spec = "";
      for (let i = 0; i < n; i += 1) {
        x = (Math.imul(x, 1664525) + 1013904223) >>> 0;
        spec += alphabet[(x >>> 16) % 4];
      }
      const rows = mk(spec);
      const out = foldProcessRows(rows);
      const detail = `${spec} -> ${shape(out).join(",")}`;
      const folds = foldEntries(out);

      // 折叠行只能位于首位或末位
      out.forEach((entry, pos) => {
        if (entry.kind === "fold" && pos !== 0 && pos !== out.length - 1) {
          failures++; console.error(`FAIL: 折叠行夹在中间 ${detail}`);
        }
      });

      // 折叠行数量与长度
      if (folds.length > 2) { failures++; console.error(`FAIL: 折叠行超过 2 个 ${detail}`); }
      for (const f of folds) {
        if (f.rows.length < 2) { failures++; console.error(`FAIL: 单项折叠 ${detail}`); }
        // 边界段：已完成必须从第 1 行起，待处理必须到最后一行止
        if (f.fold === "done" && f.range[0] !== 0) { failures++; console.error(`FAIL: 已完成折叠段未从首行起 ${detail}`); }
        if (f.fold === "todo" && f.range[1] !== n - 1) { failures++; console.error(`FAIL: 待处理折叠段未到末行止 ${detail}`); }
        // range 与 rows 一一对应
        if (f.range[1] - f.range[0] + 1 !== f.rows.length) { failures++; console.error(`FAIL: range 长度与 rows 不符 ${detail}`); }
        if (f.range[0] !== idx(f.rows[0].title) || f.range[1] !== idx(f.rows[f.rows.length - 1].title)) {
          failures++; console.error(`FAIL: range 与 rows 下标不符 ${detail}`);
        }
        for (let j = 0; j < f.rows.length; j += 1) {
          // rows[j] 就是原 rows[range[0] + j]，且原列表连续
          if (idx(f.rows[j].title) !== f.range[0] + j) { failures++; console.error(`FAIL: 折叠行 rows 不连续 ${detail}`); }
          // 折叠段状态一致
          if (f.rows[j].status !== f.rows[0].status) { failures++; console.error(`FAIL: 折叠行状态不一致 ${detail}`); }
          // 进行中 / 中断永不折叠
          if (f.rows[j].status === "running" || f.rows[j].status === "interrupted") {
            failures++; console.error(`FAIL: 进行中或中断项被折叠 ${detail}`);
          }
        }
      }

      // 无丢项、无重复
      const flat = out.flatMap((e) => (e.kind === "row" ? [e.row.title] : e.rows.map((r) => r.title)));
      if (flat.length !== n || new Set(flat).size !== n) {
        failures++; console.error(`FAIL: 丢项或重复 ${detail}`);
      }
      const covered = flat.map(idx).sort((a, b) => a - b);
      if (covered.some((v, i) => v !== i)) { failures++; console.error(`FAIL: 原列表下标未完整覆盖 ${detail}`); }

      // 可见项相对顺序与原列表一致
      const visibleIdx = out
        .filter((e): e is { kind: "row"; row: ProgressRow } => e.kind === "row")
        .map((e) => idx(e.row.title));
      if (visibleIdx.some((v, i) => i > 0 && v <= visibleIdx[i - 1])) {
        failures++; console.error(`FAIL: 可见项顺序被打乱 ${detail}`);
      }

      // 存在可行组合时必须满足限高与可见数；不存在时允许超限
      if (hasFeasible(rows)) {
        if (height(out) > PROCESS_MAX_ROWS) { failures++; console.error(`FAIL: 存在可行组合却超限 ${detail}`); }
        if (rowsOf(out) < Math.min(PROCESS_MIN_VISIBLE, n)) {
          failures++; console.error(`FAIL: 可见项少于保证数 ${detail}`);
        }
      } else if (height(out) > PROCESS_MAX_ROWS) {
        overflow += 1;
      }
      checked += 1;
    }
  }
  assert(checked > 1000, `不变量扫描 ${checked} 组组合`);
  console.log(`ok: 其中 ${overflow} 组无可行组合，按顺序优先允许超限`);
}

if (failures > 0) {
  console.error(`\n${failures} 项失败`);
  process.exit(1);
}
console.log("\n全部通过");
