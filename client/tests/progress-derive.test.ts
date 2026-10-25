/**
 * 进程行派生测试（plan-20261025-progress-keep）。
 * 纯逻辑；progressDerive 有运行时依赖（progressOwner），用 esbuild 打包后由 node 执行（client 目录）：
 *   npx esbuild tests/progress-derive.test.ts --bundle --format=esm --platform=node --outfile=.tmp-progress-derive.mjs
 *   node .tmp-progress-derive.mjs
 *
 * 覆盖：一轮结束后清单保留（全部完成 / 漏标项收口为待办）；新一轮未制定清单时沿用之前的；
 * 新清单（AI 清单或落库步骤）替换旧清单；回滚轮次作废；续接清单本轮运行中为待续接、本轮结束后落回落库步骤；
 * 停止的轮次呈现为中断；后台子代理仍在跑的步骤照常转圈；turn 行缺失的老轮次不复活。
 */
import { deriveProgress, type ProgressSource } from "../src/components/chat/progressDerive";
import type { ProgressRow } from "../src/components/chat/taskProgress";
import type { TaskOut } from "../src/api/client";
import type { TodoItem } from "../src/store/chat";

let failures = 0;
function eq(actual: unknown, expected: unknown, msg: string) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a !== e) { failures++; console.error(`FAIL: ${msg}\n   actual:   ${a}\n   expected: ${e}`); }
  else { console.log(`ok: ${msg}`); }
}

let seq = 1000;
function step(turnId: number, title: string, status: string, extra: Partial<TaskOut> = {}): TaskOut {
  seq += 1;
  return {
    id: seq, session_id: 1, turn_id: turnId, parent_task_id: 1, kind: "step", is_hidden: false,
    title, description: null, acceptance_criteria: null, agent_id: null, status, priority: 0, artifact_ids: null,
    ...extra,
  };
}
const todo = (content: string, status: TodoItem["status"]): TodoItem => ({ content, status });
const src = (over: Partial<ProgressSource>): ProgressSource => ({
  todos: null, todosTurnId: null, todosCarried: false, tasks: [], turns: [],
  runningTurnId: null, isRunning: false, owners: [], ...over,
});
const shape = (rows: ProgressRow[]) => rows.map((r) => [r.title, r.status]);

// ① 一轮结束、清单全部完成：保留整份清单，不再收起
{
  const r = deriveProgress(src({
    todos: [todo("调研", "completed"), todo("实现", "completed")], todosTurnId: 5,
    turns: [{ id: 5, status: "completed" }],
  }));
  eq(shape(r.rows), [["调研", "done"], ["实现", "done"]], "已完成的轮次保留清单");
  eq([r.phase, r.done, r.unfinished, r.fromPrevious, r.carried], ["concluded", 2, 0, false, false], "阶段为已结束、全部完成");
}

// ② 一轮结束但模型漏标：残留的进行中收口为待办，不转圈、不兜底
{
  const r = deriveProgress(src({
    todos: [todo("调研", "completed"), todo("实现", "in_progress"), todo("验证", "pending")], todosTurnId: 5,
    turns: [{ id: 5, status: "completed" }],
  }));
  eq(shape(r.rows), [["调研", "done"], ["实现", "pending"], ["验证", "pending"]], "漏标的进行中收口为待办");
  eq(r.unfinished, 2, "未完成项如实计数");
}

// ③ 新一轮已开始、尚未制定清单（turn.started 清空了 AI 清单）：沿用上一轮落库步骤，标记 fromPrevious
{
  const r = deriveProgress(src({
    tasks: [step(5, "调研", "done"), step(5, "实现", "done")],
    turns: [{ id: 5, status: "completed" }, { id: 6, status: "running" }],
    runningTurnId: 6, isRunning: true,
  }));
  eq(shape(r.rows), [["调研", "done"], ["实现", "done"]], "新一轮未制定清单时沿用之前的");
  eq([r.phase, r.turnId, r.fromPrevious], ["concluded", 5, true], "沿用的清单标记为来自之前的轮次");
}

// ④ 新一轮制定了 AI 清单：立即替换旧清单，运行中兜底「下一项」为进行中
{
  const r = deriveProgress(src({
    todos: [todo("新任务 A", "pending"), todo("新任务 B", "pending")], todosTurnId: 6,
    tasks: [step(5, "调研", "done")],
    turns: [{ id: 5, status: "completed" }, { id: 6, status: "running" }],
    runningTurnId: 6, isRunning: true,
  }));
  eq(shape(r.rows), [["新任务 A", "running"], ["新任务 B", "pending"]], "新清单替换旧清单");
  eq([r.phase, r.fromPrevious], ["running", false], "新清单属于本轮");
}

// ⑤ 新一轮的清单已落库（重启后没有 AI 清单）：落库步骤取最新含步骤的轮次
{
  const r = deriveProgress(src({
    tasks: [step(5, "旧", "done"), step(6, "新", "in_progress")],
    turns: [{ id: 5, status: "completed" }, { id: 6, status: "running" }],
    runningTurnId: 6, isRunning: true,
  }));
  eq(shape(r.rows), [["新", "running"]], "落库步骤以最新含步骤的轮次为准");
}

// ⑥ 回滚：被回滚轮次的 AI 清单与落库步骤都作废，退回更早的清单
{
  const r = deriveProgress(src({
    todos: [todo("被回滚的", "completed")], todosTurnId: 6,
    tasks: [step(5, "更早的", "done"), step(6, "被回滚的", "done")],
    turns: [{ id: 5, status: "completed" }, { id: 6, status: "rolled_back" }],
  }));
  eq(shape(r.rows), [["更早的", "done"]], "回滚轮次的清单不再展示");
}

// ⑦ 续接清单：本轮运行中呈现为待续接（未完成项警示、不转圈），不带逐行备注
{
  const r = deriveProgress(src({
    todos: [todo("调研", "completed"), todo("实现", "in_progress")], todosTurnId: 7, todosCarried: true,
    turns: [{ id: 6, status: "interrupted" }, { id: 7, status: "running" }],
    runningTurnId: 7, isRunning: true,
  }));
  eq(shape(r.rows), [["调研", "done"], ["实现", "interrupted"]], "续接清单的未完成项为中断态");
  eq([r.carried, r.fromPrevious, r.rows.map((x) => x.note).join("")], [true, false, ""], "续接标记独立于备注");
}

// ⑧ 续接所在的轮次结束仍未更新：不再标待续接，落回落库步骤（与重启后一致）
{
  const r = deriveProgress(src({
    todos: [todo("调研", "completed"), todo("实现", "in_progress")], todosTurnId: 7, todosCarried: true,
    tasks: [step(6, "调研", "done"), step(6, "实现", "cancelled")],
    turns: [{ id: 6, status: "interrupted" }, { id: 7, status: "completed" }],
  }));
  eq(shape(r.rows), [["调研", "done"], ["实现", "interrupted"]], "落回落库步骤展示");
  eq([r.carried, r.turnId, r.phase], [false, 6, "interrupted"], "不再标记待续接");
}

// ⑨ 停止的轮次：进行中呈现为中断，不转圈
{
  const r = deriveProgress(src({
    todos: [todo("调研", "completed"), todo("实现", "in_progress"), todo("验证", "pending")], todosTurnId: 5,
    turns: [{ id: 5, status: "interrupted" }],
  }));
  eq(shape(r.rows), [["调研", "done"], ["实现", "interrupted"], ["验证", "pending"]], "停止后进行中为中断态");
}

// ⑩ 轮次已结束，但它派出的后台子代理还在跑：该子代理负责的步骤照常转圈，其余收口
{
  const r = deriveProgress(src({
    tasks: [step(5, "并行 A", "in_progress", { agent_id: 41 }), step(5, "收尾", "in_progress")],
    turns: [{ id: 5, status: "completed" }],
    owners: [{ id: 41, running: true, kind: "subagent" }],
  }));
  eq(shape(r.rows), [["并行 A", "running"], ["收尾", "pending"]], "后台子代理的步骤保持进行中");
}

// ⑪ turn 行缺失的老轮次（超出加载上限）：比正在运行的轮次更早 → 已结束，不复活为进行中
{
  const r = deriveProgress(src({
    tasks: [step(3, "很早的步骤", "in_progress")],
    turns: [{ id: 9, status: "running" }],
    runningTurnId: 9, isRunning: true,
  }));
  eq(shape(r.rows), [["很早的步骤", "pending"]], "老轮次的残留进行中不复活");
  eq([r.phase, r.fromPrevious], ["concluded", true], "老轮次按已结束、来自之前的轮次处理");
}

// ⑫ 没有任何清单
{
  const r = deriveProgress(src({ turns: [{ id: 1, status: "completed" }] }));
  eq([r.rows.length, r.phase, r.turnId], [0, "concluded", null], "无清单时为空");
}

// ⑬ plan-568-2333：一轮正常做完、模型漏标最后一项——前面全已完成时补标为完成
{
  const r = deriveProgress(src({
    todos: [todo("调研", "completed"), todo("实现", "completed"), todo("汇总交付", "in_progress")],
    todosTurnId: 5,
    turns: [{ id: 5, status: "completed" }],
  }));
  eq(shape(r.rows), [["调研", "done"], ["实现", "done"], ["汇总交付", "done"]], "正常完成后补标漏标的末项");
  eq([r.done, r.unfinished], [3, 0], "计数不再差一项");
}

// ⑭ 同一份清单但轮次被停止：末项必须如实留在未完成，不能补成完成
{
  const r = deriveProgress(src({
    todos: [todo("调研", "completed"), todo("实现", "completed"), todo("汇总交付", "in_progress")],
    todosTurnId: 5,
    turns: [{ id: 5, status: "interrupted" }],
  }));
  eq(shape(r.rows), [["调研", "done"], ["实现", "done"], ["汇总交付", "interrupted"]], "停止的轮次不补标");
  eq(r.unfinished, 1, "停止时未完成项如实计数");
}

// ⑮ 中间还有未完成项：说明活确实没干完，末项不补标
{
  const r = deriveProgress(src({
    todos: [todo("调研", "completed"), todo("实现", "pending"), todo("汇总交付", "in_progress")],
    todosTurnId: 5,
    turns: [{ id: 5, status: "completed" }],
  }));
  eq(shape(r.rows), [["调研", "done"], ["实现", "pending"], ["汇总交付", "pending"]], "前面仍有未完成时不补标");
  eq(r.unfinished, 2, "未完成项如实计数");
}

// ⑯ 末项归属子代理：是否完成由执行者运行态决定，不参与补标
{
  const running = deriveProgress(src({
    tasks: [step(5, "调研", "done"), step(5, "并行收尾", "in_progress", { agent_id: 41 })],
    turns: [{ id: 5, status: "completed" }],
    owners: [{ id: 41, running: true, kind: "subagent" }],
  }));
  eq(shape(running.rows), [["调研", "done"], ["并行收尾", "running"]], "子代理仍在跑的末项不补成完成");

  const finished = deriveProgress(src({
    tasks: [step(5, "调研", "done"), step(5, "并行收尾", "in_progress", { agent_id: 41 })],
    turns: [{ id: 5, status: "completed" }],
    owners: [{ id: 41, running: false, kind: "subagent" }],
  }));
  eq(shape(finished.rows), [["调研", "done"], ["并行收尾", "pending"]], "子代理结束后末项仍按清单状态（不擅自补标）");
}

// ⑰ 末项从未开始（pending）或已被取消（cancelled）：不是「做着做着收尾」，一律不补标
{
  const neverStarted = deriveProgress(src({
    todos: [todo("调研", "completed"), todo("实现", "completed"), todo("可选项", "pending")],
    todosTurnId: 5,
    turns: [{ id: 5, status: "completed" }],
  }));
  eq(shape(neverStarted.rows), [["调研", "done"], ["实现", "done"], ["可选项", "pending"]], "末项本来就没开始时不补标");

  const cancelled = deriveProgress(src({
    tasks: [step(5, "调研", "done"), step(5, "实现", "done"), step(5, "已砍掉", "cancelled")],
    turns: [{ id: 5, status: "completed" }],
  }));
  eq(shape(cancelled.rows), [["调研", "done"], ["实现", "done"], ["已砍掉", "interrupted"]], "末项已取消时仍为中断态");
}

if (failures > 0) {
  console.error(`\n${failures} 项失败`);
  process.exit(1);
}
console.log("\n全部通过");
