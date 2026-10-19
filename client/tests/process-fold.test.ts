/**
 * 进程栏动态折叠算法测试（plan-20261019-toolcard-v2）。
 * 纯逻辑，Node 原生 TS 运行：`node tests/process-fold.test.ts`（client 目录）。
 *
 * 口径：未超限（≤ 7 行）不折叠；超出后先折已完成、仍放不下再折最靠后的待办；
 * 优先级「进行中 > 即将执行 > 靠后执行 > 已完成」；折叠量不多不少（可见项数最大化）；
 * 未折叠项至少 5 个；进行中 / 中断项永不折叠；只含一项的折叠行还原成该项。
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

/** 折叠结果的简写：行用标题，折叠行用 `[done:N]` / `[todo:N]` */
function shape(entries: ProcessEntry[]): string[] {
  return entries.map((e) => (e.kind === "row" ? e.row.title : `[${e.fold}:${e.rows.length}]`));
}
const rowsOf = (entries: ProcessEntry[]) => entries.filter((e) => e.kind === "row").length;
const height = (entries: ProcessEntry[]) => entries.length;

// ① 常量口径
eq([PROCESS_MIN_VISIBLE, PROCESS_MAX_ROWS], [5, 7], "5 个未折叠项 + 最多 2 个折叠行 = 限高 7 行");

// ② 未超限不折叠（含恰好 7 行）
eq(shape(foldProcessRows(mk("dddrppp"))), ["d0", "d1", "d2", "r3", "p4", "p5", "p6"], "7 行刚好放下：不折叠");
eq(shape(foldProcessRows([])), [], "空列表");
eq(shape(foldProcessRows(mk("r"))), ["r0"], "单行");

// ③ 超出：先折已完成（最早的先折），折叠量刚好达到限高
{
  const out = foldProcessRows(mk("ddddrppp")); // 8 行：4 完成 + 1 进行中 + 3 待办
  eq(shape(out), ["[done:2]", "d2", "d3", "r4", "p5", "p6", "p7"], "8 行：只折最早的 2 个已完成，占满 7 行");
  assert(height(out) === 7, "高度恰好等于限高");
  assert(rowsOf(out) >= PROCESS_MIN_VISIBLE, "未折叠项 ≥ 5");
}
{
  const out = foldProcessRows(mk("dddddd" + "r" + "ppppp")); // 12 行：6 完成 + 进行中 + 5 待办
  eq(shape(out), ["[done:6]", "r6", "p7", "p8", "p9", "p10", "p11"], "已完成全部折叠后恰好放下，待办不动");
}

// ④ 已完成折光仍放不下：再折最靠后的待办；进行中与即将执行优先保留
{
  const out = foldProcessRows(mk("ddd" + "r" + "pppppppp")); // 12 行：3 完成 + 进行中 + 8 待办
  eq(shape(out), ["[done:3]", "r3", "p4", "p5", "p6", "p7", "[todo:4]"], "先折已完成，再折最靠后的 4 个待办");
  assert(height(out) === 7 && rowsOf(out) === 5, "折叠后正好 5 个未折叠项 + 2 个折叠行");
  const todo = out[out.length - 1];
  assert(todo.kind === "fold" && todo.rows.map((r) => r.title).join() === "p8,p9,p10,p11", "待处理折叠行按原顺序收纳被折叠项");
}

// ⑤ 优先级：已完成里离当前近的后折（最近完成的留在屏幕上）
{
  const out = foldProcessRows(mk("dddddd" + "r" + "pp")); // 9 行
  // 折 2 个只能降到 8 行（折叠行本身占一行），所以要折 3 个才刚好 7 行
  eq(shape(out), ["[done:3]", "d3", "d4", "d5", "r6", "p7", "p8"], "9 行：折最早的 3 个已完成，近的留下，刚好 7 行");
}

// ⑥ 只含一项的折叠行还原成该项本身（折叠一项不省高度）
{
  const out = foldProcessRows(mk("d" + "r" + "pppppp")); // 8 行：1 完成 + 进行中 + 6 待办
  eq(shape(out), ["d0", "r1", "p2", "p3", "p4", "p5", "[todo:2]"], "已完成只剩 1 项时直接显示，不为它做折叠行");
  assert(height(out) === 7, "高度仍不超限");
}

// ⑦ 全部完成：按最近完成优先
{
  const out = foldProcessRows(mk("ddddddddd")); // 9 行全完成
  eq(shape(out), ["[done:3]", "d3", "d4", "d5", "d6", "d7", "d8"], "全部完成：折最早的，最近完成的留下");
}

// ⑧ 进行中 / 中断永不折叠，即使并行步骤很多
{
  const out = foldProcessRows(mk("rrrrrrrrrr")); // 10 个进行中
  assert(out.every((e) => e.kind === "row") && out.length === 10, "10 个进行中全部可见（容器内部滚动兜底）");
  const out2 = foldProcessRows(mk("ddd" + "i" + "pppppp"));
  assert(shape(out2).includes("i3"), "中断项不被折叠");
  assert(height(out2) <= PROCESS_MAX_ROWS, "中断场景同样不超限");
}

// ⑨ 乱序：待办排在进行中之前也按「离焦点远近」处理，进行中不动
{
  const out = foldProcessRows(mk("pppp" + "r" + "dddd")); // 待办在前、进行中居中、已完成在后（异常顺序）
  assert(shape(out).includes("r4"), "进行中项可见");
  assert(height(out) <= PROCESS_MAX_ROWS, "乱序场景不超限");
}

// ⑩ 折叠行内容与标签
{
  const out = foldProcessRows(mk("ddddrppp"));
  const fold = out[0];
  assert(fold.kind === "fold" && fold.fold === "done" && fold.rows.length === 2, "已完成折叠行收纳 2 项");
  eq(foldLabel("done", 4), "已完成 4 项", "已完成文案");
  eq(foldLabel("todo", 1), "待处理 1 项", "待处理文案");
}

// ⑪ 自定义容量
{
  const out = foldProcessRows(mk("ddrpp"), 4);
  assert(height(out) <= 4, "自定义限高 4 行也不超限");
}

// ⑫ 不变量扫描：各种组合下高度不超限（进行中 ≤ 限高时）、顺序保持、无重复无遗漏
{
  let checked = 0;
  const alphabet = ["d", "r", "p", "i"];
  const total = (e: ProcessEntry) => (e.kind === "row" ? 1 : e.rows.length);
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
      const pinned = rows.filter((r) => r.status === "running" || r.status === "interrupted").length;
      const out = foldProcessRows(rows);
      if (pinned <= PROCESS_MAX_ROWS - 2) {
        if (height(out) > PROCESS_MAX_ROWS) { failures++; console.error(`FAIL: 超限 ${spec} -> ${shape(out)}`); }
      }
      const flat = out.flatMap((e) => (e.kind === "row" ? [e.row.title] : e.rows.map((r) => r.title)));
      if (out.reduce((a, e) => a + total(e), 0) !== n || new Set(flat).size !== n) {
        failures++; console.error(`FAIL: 丢项或重复 ${spec} -> ${shape(out)}`);
      }
      // 可见行的相对顺序与原列表一致
      const visibleTitles = out.filter((e) => e.kind === "row").map((e) => (e as { row: ProgressRow }).row.title);
      const sorted = [...visibleTitles].sort((a, b) => Number(a.slice(1)) - Number(b.slice(1)));
      if (visibleTitles.join() !== sorted.join()) { failures++; console.error(`FAIL: 可见项顺序被打乱 ${spec}`); }
      // 进行中 / 中断永远可见
      for (const r of rows) {
        if ((r.status === "running" || r.status === "interrupted") && !visibleTitles.includes(r.title)) {
          failures++; console.error(`FAIL: 进行中项被折叠 ${spec} ${r.title}`);
        }
      }
      // 超限时可见项数最大化：多放一项（把优先级最高的折叠项拿出来）必然超限
      if (n > PROCESS_MAX_ROWS && pinned <= PROCESS_MAX_ROWS - 2 && height(out) < PROCESS_MAX_ROWS - 0) {
        // 允许少 1 行：单项折叠还原、或折叠行数为 1 时的整数边界（k + 1 组）
        if (height(out) < PROCESS_MAX_ROWS - 1) { failures++; console.error(`FAIL: 折叠过多（还有空位）${spec} -> ${shape(out)}`); }
      }
      checked += 1;
    }
  }
  assert(checked > 1000, `不变量扫描 ${checked} 组组合`);
}

if (failures > 0) {
  console.error(`\n${failures} 项失败`);
  process.exit(1);
}
console.log("\n全部通过");
