/**
 * 任务进度行 · 子会话归属与运行态修正测试（plan-20261020-progress）。
 * 纯逻辑，Node 原生 TS 运行：`node tests/progress-owner.test.ts`（client 目录）。
 *
 * 覆盖：owner 缺失时按标题 #id 推断、只认实际存在的子会话 id、运行中置 running、
 * 已结束沿用清单状态、已完成的步骤不被翻回进行中、owner 已填时以 owner 为准。
 */
import { resolveSubsessionRows, type SubsessionRunState } from "../src/components/chat/progressOwner.ts";
import type { ProgressRow } from "../src/components/chat/taskProgress.ts";

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

function row(title: string, status: ProgressRow["status"], owner?: number | null): ProgressRow {
  return { key: title, title, note: "", status, owner: owner ?? null };
}
const subs = (...spec: [number, boolean][]): SubsessionRunState[] =>
  spec.map(([id, running]) => ({ id, running }));
const shape = (rows: ProgressRow[]) => rows.map((r) => [r.title, r.status, r.owner ?? null]);

// ① owner 缺失时按标题里的 #id 推断归属，并置为 running
{
  const out = resolveSubsessionRows(
    [row("实现登录页（已派发 #465）", "pending"), row("整理文档", "pending")],
    subs([465, true]),
  );
  eq(shape(out), [
    ["实现登录页（已派发 #465）", "running", 465],
    ["整理文档", "pending", null],
  ], "标题里的 #465 推断为归属，运行中置 running 并写回 owner");
}

// ② 只认真实存在的子会话 id：标题里的 #999 不在 subs 中，不认
{
  const out = resolveSubsessionRows([row("参考 #999 的做法", "pending")], subs([465, true]));
  eq(shape(out), [["参考 #999 的做法", "pending", null]], "#999 不是本会话的子会话，不参与判定");
}

// ③ 多个 #id：取第一个真实存在的子会话
{
  const out = resolveSubsessionRows(
    [row("对齐 #111 与 #222", "pending")],
    subs([222, true]),
  );
  eq(shape(out), [["对齐 #111 与 #222", "running", 222]], "跳过不存在的 #111，命中 #222");
}

// ④ 子会话已结束：沿用清单状态，不擅自改
{
  const out = resolveSubsessionRows(
    [row("实现登录页 #465", "pending"), row("写测试 #466", "running")],
    subs([465, false], [466, false]),
  );
  eq(shape(out), [
    ["实现登录页 #465", "pending", 465],
    ["写测试 #466", "running", 466],
  ], "已结束的子会话不改状态（pending 保持 pending、running 保持清单原值）");
}

// ⑤ 不误改已完成项：子会话还在跑，但该步骤已标记完成
{
  const out = resolveSubsessionRows([row("已完成的步骤 #465", "done")], subs([465, true]));
  eq(shape(out), [["已完成的步骤 #465", "done", 465]], "done 不被翻回 running（只补归属）");
}

// ⑥ owner 已填时以 owner 为准：标题里的 #id 不覆盖它
{
  const out = resolveSubsessionRows(
    [row("整理文档 #465", "pending", 466)],
    subs([465, true], [466, false]),
  );
  eq(shape(out), [["整理文档 #465", "pending", 466]], "owner=466 已结束 → 保持 pending，不被标题 #465 覆盖");
}
{
  const out = resolveSubsessionRows(
    [row("整理文档 #465", "pending", 466)],
    subs([465, false], [466, true]),
  );
  eq(shape(out), [["整理文档 #465", "running", 466]], "owner=466 在跑 → 以 owner 判定为 running");
}

// ⑦ 并行派发：多个子会话同时在跑，多行同时转圈
{
  const out = resolveSubsessionRows(
    [row("调研 #465", "running"), row("实现 #466", "pending"), row("补测试 #467", "pending")],
    subs([465, true], [466, true], [467, false]),
  );
  eq(out.filter((r) => r.status === "running").length, 2, "465 / 466 同时在跑 → 两项转圈");
  eq(out[2].status, "pending", "467 已结束 → 保持待办");
}

// ⑧ 无子会话时原样返回（不产生多余拷贝 / 不改状态）
{
  const rows = [row("单一主代理步骤", "running")];
  const out = resolveSubsessionRows(rows, []);
  assert(out === rows, "subs 为空直接返回原数组");
}

// ⑨ 不修改入参对象
{
  const input = [row("实现 #465", "pending")];
  const out = resolveSubsessionRows(input, subs([465, true]));
  eq(input[0].status, "pending", "原始行对象未被就地修改");
  assert(out !== input, "返回新数组");
}

if (failures > 0) {
  console.error(`\n${failures} 项失败`);
  process.exit(1);
}
console.log("\n全部通过");
