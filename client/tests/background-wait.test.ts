/**
 * 后台等待期间的「整轮」口径回归测试（plan-20261014-actions-wait）。
 *
 * 纯逻辑测试（不依赖 DOM / store），验证：
 *   ① 尾部一轮判定：跳过 turn_id 回填前的唤醒孤儿条目（它马上并入前一轮），普通的新用户消息不跳过；
 *   ② 唤醒消息归轮：按 workflow.wakeup 的 turn_id 就地回填后，唤醒轮立即并入上一轮（一轮一个计时条）；
 *      只认「最新的一条用户侧消息」，不会把遗留的孤儿消息错归到新轮；
 *   ③ 工作流忙碌口径：运行中的程序 ∨ 待唤醒；
 *   ④ 整轮用时终点下限：取已启动、不在运行的程序里最晚的结束时刻，草稿与运行中的不计。
 *
 * 运行方式（client 目录，无测试框架，用 esbuild 打包后由 node 直接执行）：
 *   npx esbuild tests/background-wait.test.ts --bundle --format=esm --platform=node \
 *     --alias:@chatcoder/shared=../packages/shared/src/index.ts --outfile=<临时目录>/bg-wait.test.mjs
 *   node <临时目录>/bg-wait.test.mjs
 */
import assert from "node:assert/strict";
import {
  buildTimeline,
  createRoundMerger,
  isOrphanContinuation,
  tailRoundIndex,
} from "../src/components/chat/timeline";
import type { TimelineEntry } from "../src/components/chat/timeline";
import { adoptWakeupTurn } from "../src/utils/wakeupMessages";
import {
  latestWorkflowEndMs,
  selectSessionWorkflowBusy,
  sessionWorkflowBusy,
  sessionWorkflowRunning,
} from "../src/store/workflowBusy";
import { MsgType, SenderType } from "@chatcoder/shared";
import type { MessageOut, WorkflowRunOut } from "../src/api/client";

function msg(
  id: number,
  turnId: number | null,
  msgType: string,
  content: Record<string, unknown>,
  sender: string = SenderType.Agent,
): MessageOut {
  return {
    id,
    session_id: 1,
    turn_id: turnId,
    thread_id: null,
    sender_type: sender,
    sender_id: null,
    msg_type: msgType,
    content,
    created_at: null,
  };
}

const R = 10; // 发起工作流的那一轮
const C = 11; // 唤醒轮

/** 一轮完整的「发起工作流」：用户消息 + 已启动文字。 */
const round = (): MessageOut[] => [
  msg(1, R, MsgType.Text, { text: "开始执行" }, SenderType.User),
  msg(2, R, MsgType.Text, { text: "工作流已启动" }),
];
/** 唤醒消息刚广播时：还没有 turn_id（服务端先发消息、后建轮次）。 */
const orphanWakeup = (id = 3): MessageOut =>
  msg(id, null, MsgType.Text, { text: "工作流已完成", workflow_wakeup: true }, SenderType.User);

// ── ① 尾部一轮 ────────────────────────────────────────────

{
  const entries = buildTimeline(round());
  assert.equal(entries.length, 1);
  assert.equal(tailRoundIndex(entries), 0, "只有一轮：它就是尾部");
}

{
  const entries = buildTimeline([...round(), orphanWakeup()]);
  assert.equal(entries.length, 2, "唤醒消息在 turn_id 回填前是独立的空 turn 条目");
  assert.ok(isOrphanContinuation(entries[1]));
  assert.ok(!isOrphanContinuation(entries[0]));
  assert.equal(tailRoundIndex(entries), 0, "跳过续跑孤儿条目：尾部一轮仍是发起工作流的那一轮");
}

{
  // 用户在等待期间发了新消息（乐观占位、尚无 turn_id）：它是用户消息而不是续跑，上一轮不再是尾部
  const typed = msg(3, null, MsgType.Text, { text: "再帮我看下 xx" }, SenderType.User);
  const entries = buildTimeline([...round(), typed]);
  assert.ok(!isOrphanContinuation(entries[1]));
  assert.equal(tailRoundIndex(entries), 1);
}

{
  assert.equal(tailRoundIndex([]), -1);
  const onlyOrphan = buildTimeline([orphanWakeup(1)]);
  assert.equal(tailRoundIndex(onlyOrphan), -1, "全是孤儿：没有可挂靠的尾部一轮");
}

// ── ② 唤醒消息归轮 ────────────────────────────────────────

{
  const before = [...round(), orphanWakeup()];
  const adopted = adoptWakeupTurn(before, C);
  assert.notEqual(adopted, before, "有可回填的唤醒消息：返回新数组");
  assert.equal(adopted[2].turn_id, C);
  assert.equal(adopted[0], before[0], "其它消息保持原引用（时间线缓存继续命中）");
  assert.equal(adopted[1], before[1]);
  assert.equal(before[2].turn_id, null, "不修改入参");

  // 回填后唤醒轮立即并入上一轮：一轮一个计时条，turnIds 含两轮
  const withReply = [...adopted, msg(4, C, MsgType.Text, { text: "汇报：已完成" })];
  const merged = createRoundMerger()(buildTimeline(withReply));
  assert.equal(merged.length, 1, "上一轮与唤醒轮合为一轮");
  const only = merged[0] as Extract<TimelineEntry, { kind: "turn" }>;
  assert.deepEqual(only.turnIds, [R, C]);
  assert.ok(only.items.some((it) => it.kind === "workflow-wakeup"));
  assert.equal(tailRoundIndex(merged), 0);

  // 对照：不回填时，唤醒轮自成一轮、孤儿条目独立——这正是回填之前唤醒期间上一轮操作行停在中间的原因
  const unmerged = createRoundMerger()(buildTimeline([...before, msg(4, C, MsgType.Text, { text: "汇报：已完成" })]));
  assert.equal(unmerged.length, 3);
}

{
  // 最新的用户侧消息不是未归属的唤醒消息 → 不回填
  const typedAfter = msg(5, R, MsgType.Text, { text: "再来一个" }, SenderType.User);
  const messages = [...round(), orphanWakeup(3), typedAfter];
  assert.equal(adoptWakeupTurn(messages, C), messages, "遗留的孤儿消息不得错归到新轮");

  // 已归属的唤醒消息 → 不重复回填
  const owned = [...round(), msg(3, R, MsgType.Text, { text: "x", workflow_wakeup: true }, SenderType.User)];
  assert.equal(adoptWakeupTurn(owned, C), owned);

  // AI 消息夹在中间不影响判定（只看用户侧消息）
  const withAgent = [...round(), orphanWakeup(3), msg(4, null, MsgType.Text, { text: "ai" })];
  assert.equal(adoptWakeupTurn(withAgent, C)[2].turn_id, C);

  assert.deepEqual(adoptWakeupTurn([], C), []);
}

// ── ③ 工作流忙碌口径 ──────────────────────────────────────

function run(over: Partial<WorkflowRunOut>): WorkflowRunOut {
  return {
    id: 1, session_id: 1, parent_run_id: null, kind: "program", package_key: null, title: "t", goal: "", rationale: "",
    based_on: null, status: "succeeded", pause_kind: null, pause_reason: null, paused_at: null, resume_count: 0,
    rework_total: 0, token_used: 0, token_budget: 0, workspace: "", isolation: "none", graph: null, plan_rev: 1,
    origin_turn_id: null, last_turn_id: null, last_seq: 0, started_at: "2026-10-14 10:00:00", updated_at: null,
    finished_at: null, error: null,
    ...over,
  };
}

{
  assert.equal(sessionWorkflowRunning(undefined), false);
  assert.equal(sessionWorkflowRunning([run({ status: "running" })]), true);
  assert.equal(sessionWorkflowBusy(undefined, undefined), false);
  assert.equal(sessionWorkflowBusy([run({ status: "succeeded" })], false), false, "程序已结束、没有待唤醒：空闲");
  assert.equal(sessionWorkflowBusy([run({ status: "running" })], false), true);
  assert.equal(sessionWorkflowBusy([run({ status: "succeeded" })], true), true, "程序已结束但待唤醒：仍忙碌（接上 2 秒空档）");
  assert.equal(sessionWorkflowBusy(undefined, true), true);

  const state = { bySession: { 7: [run({ status: "succeeded" })] }, wakeupPending: { 7: true } as Record<number, boolean> };
  assert.equal(selectSessionWorkflowBusy(state, 7), true);
  assert.equal(selectSessionWorkflowBusy(state, 8), false);
  assert.equal(selectSessionWorkflowBusy(state, null), false);
  assert.equal(selectSessionWorkflowBusy(state, undefined), false);
}

// ── ④ 整轮用时终点下限 ────────────────────────────────────

{
  const ms = (s: string) => Date.parse(`${s.replace(" ", "T")}Z`);
  assert.equal(latestWorkflowEndMs(undefined), 0);
  assert.equal(latestWorkflowEndMs([]), 0);
  assert.equal(latestWorkflowEndMs([run({ status: "running" })]), 0, "运行中的不算（还没结束）");

  const runs = [
    run({ id: 1, status: "succeeded", finished_at: "2026-10-14 10:05:00" }),
    run({ id: 2, status: "paused", paused_at: "2026-10-14 10:20:00", finished_at: null }),
    run({ id: 3, status: "cancelled", finished_at: "2026-10-14 10:10:00" }),
    run({ id: 4, status: "running", updated_at: "2026-10-14 11:00:00" }),
    run({ id: 5, status: "draft", started_at: null, updated_at: "2026-10-14 12:00:00" }),
  ];
  assert.equal(latestWorkflowEndMs(runs), ms("2026-10-14 10:20:00"), "取暂停时刻；运行中与草稿都不计");

  assert.equal(
    latestWorkflowEndMs([run({ status: "waiting_human", finished_at: null, paused_at: null, updated_at: "2026-10-14 10:30:00" })]),
    ms("2026-10-14 10:30:00"),
    "等待决定没有结束 / 暂停时间戳：回落到 updated_at",
  );

  // 先暂停、很久之后才取消：暂停期间没有在工作，终点以暂停时刻为准，不被取消时刻拉长
  assert.equal(
    latestWorkflowEndMs([run({ status: "cancelled", paused_at: "2026-10-14 10:20:00", finished_at: "2026-10-16 09:00:00" })]),
    ms("2026-10-14 10:20:00"),
    "暂停后才取消：取 paused_at",
  );
  // 暂停后续跑并完成：续跑清掉了 paused_at，取完成时刻
  assert.equal(
    latestWorkflowEndMs([run({ status: "succeeded", paused_at: null, finished_at: "2026-10-14 11:00:00" })]),
    ms("2026-10-14 11:00:00"),
    "续跑后完成：取 finished_at",
  );
}

console.log("background-wait: all assertions passed");
