/**
 * 工作流卡与节点子代理卡按创建序固化的回归测试（plan-20261013-wf-order）。
 *
 * 纯逻辑测试（不依赖 DOM），验证：
 *   ① 时间线：工作流卡记录其工具调用所在的轮与首条调用的 id；
 *   ② 分段：常显项（工作流卡、工作流唤醒分隔线）各自成段，过程项只在常显项之间连续归段，
 *      挂靠的节点卡所在条目之后紧跟挂靠段；
 *   ③ 定位：节点子代理按派发时刻（线程首条消息 id）挂到派发前最后一个条目之后；
 *      无派发时刻时回退为挂到同轮同程序的工作流卡之后；首条用户消息不作锚点；
 *   ④ 漂移回归：内容逐步追加（节点陆续启动、消息陆续落库、最终汇报落库）时，已渲染项的相对顺序不变。
 *
 * 运行方式（client 目录，无测试框架，用 esbuild 打包后由 node 直接执行）：
 *   npx esbuild tests/workflow-card-order.test.ts --bundle --format=esm --platform=node \
 *     --alias:@chatcoder/shared=../packages/shared/src/index.ts --outfile=<临时目录>/wf-order.test.mjs
 *   node <临时目录>/wf-order.test.mjs
 */
import assert from "node:assert/strict";
import {
  buildTimeline,
  isOutsideProcess,
  placeWorkflowNodeCards,
  splitFlowSegments,
} from "../src/components/chat/timeline";
import type { FlowSegment, TimelineEntry, TurnItem, WorkflowNodeCard } from "../src/components/chat/timeline";
import { MsgType, SenderType } from "@chatcoder/shared";
import type { MessageOut } from "@chatcoder/shared";

/** 构造一条测试消息（只填时间线构建真正读取的字段）。 */
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

/** 子代理卡片（只含定位所需字段）。 */
interface Card extends WorkflowNodeCard {
  name: string;
}

function card(agentId: number, turnId: number | null, programId: number | null, spawnMessageId?: number): Card {
  return { agentId, name: `audit•${agentId}`, turnId, programId, spawnMessageId: spawnMessageId ?? null };
}

/** 占位的 turn 项（只为分段 / 挂靠测试提供 kind 与必要字段）。 */
const item = (kind: string): TurnItem => ({ kind, msg: {} as MessageOut }) as unknown as TurnItem;

const T = 10;
const PROGRAM = 7;

/** 场景消息：第 1 条为用户消息，第 2、3 条为工具调用（第 3 条启动工作流），之后依次为
 *  已启动文字、工作流唤醒、一次工具调用与最终汇报。 */
function scenario(upto: number): MessageOut[] {
  const all = [
    msg(1, T, MsgType.Text, { text: "开始执行" }, SenderType.User),
    msg(2, T, MsgType.ToolCall, { tool: "read_file", call_key: "k2", args: {} }),
    msg(3, T, MsgType.ToolCall, { tool: "start_workflow", call_key: "k3", args: { program_id: PROGRAM } }),
    msg(4, T, MsgType.Text, { text: "工作流已启动" }),
    msg(5, T, MsgType.Text, { text: "工作流已结束", workflow_wakeup: true }, SenderType.User),
    msg(6, T, MsgType.ToolCall, { tool: "workflow_status", call_key: "k6", args: {} }),
    msg(7, T, MsgType.Text, { text: "最终汇报" }),
  ];
  return all.slice(0, upto);
}

/** 与真实会话同形的场景：工作流卡之后，计划文字、启动文字与各次进度文字交错，节点陆续派发。
 *  派发时刻（spawnMessageId）取节点线程首条消息的 id，它们不在主消息流中，因此 id 有间隔。 */
const SPAWN_21 = 6;
const SPAWN_22 = 8;
const SPAWN_23 = 10;
function interleaved(upto: number): MessageOut[] {
  const all = [
    msg(1, T, MsgType.Text, { text: "开始执行" }, SenderType.User),
    msg(2, T, MsgType.ToolCall, { tool: "read_file", call_key: "k2", args: {} }),
    msg(3, T, MsgType.ToolCall, { tool: "start_workflow", call_key: "k3", args: { program_id: PROGRAM } }),
    msg(4, T, MsgType.Text, { text: "计划已生成" }),
    msg(5, T, MsgType.Text, { text: "工作流已启动" }),
    msg(7, T, MsgType.Text, { text: "工作流进行中" }),
    msg(9, T, MsgType.Text, { text: "正在运行" }),
    msg(11, T, MsgType.Text, { text: "最终汇报" }),
  ];
  return all.filter((m) => m.id <= upto);
}

/** 取场景 turn 的时间线项。 */
function timelineItems(messages: MessageOut[]): TurnItem[] {
  const entries: TimelineEntry[] = buildTimeline(messages);
  const turn = entries.find((e) => e.kind === "turn" && e.turnId === T);
  assert.ok(turn && turn.kind === "turn", "应存在 turn 10");
  return turn.items;
}

/**
 * 按渲染顺序输出线性键序列，与 TurnGroup 的执行段渲染规则一致：
 * 有最终汇报时只分段渲染最终汇报之前的项，最终汇报及其后的项按序直显；
 * 挂靠了节点卡的条目之后紧跟其挂靠段（节点卡）。
 */
function renderOrder(items: TurnItem[], cards: Card[]): string[] {
  const firstUser = items.findIndex((it) => it.kind === "user");
  let finalIdx = -1;
  for (let k = items.length - 1; k >= 0; k--) {
    if (items[k].kind === "text") {
      finalIdx = k;
      break;
    }
  }
  const { attached } = placeWorkflowNodeCards(items, cards);
  const attachedIdx = new Set(attached.keys());
  const hasProcess =
    finalIdx >= 0 && items.some((it, i) => i !== firstUser && i < finalIdx && !isOutsideProcess(it));

  const order: string[] = [];
  const emit = (segments: FlowSegment[]) => {
    for (const seg of segments) {
      if (seg.kind === "sticky") order.push(`item:${seg.index}`);
      else if (seg.kind === "attached") {
        for (const c of attached.get(seg.index) ?? []) order.push(`card:${c.agentId}`);
      } else for (const index of seg.indices) order.push(`item:${index}`);
    }
  };
  if (hasProcess) {
    emit(splitFlowSegments(items, 0, finalIdx, firstUser, attachedIdx));
    emit(splitFlowSegments(items, finalIdx, items.length, firstUser, attachedIdx));
  } else {
    emit(splitFlowSegments(items, 0, items.length, firstUser, attachedIdx));
  }
  return order;
}

const cases: Array<[string, () => void]> = [];
function test(name: string, fn: () => void) {
  cases.push([name, fn]);
}

test("时间线：工作流卡记录其工具调用所在的轮，以及该簇首条调用的 id", () => {
  const cardItem = timelineItems(scenario(3)).find((it) => it.kind === "workflow");
  assert.deepEqual(cardItem, { kind: "workflow", programId: PROGRAM, turnId: T, firstId: 2 });
});

test("分段：常显项各自成段，过程项只在常显项之间连续归段，跳过首条用户消息", () => {
  const items = [item("user"), item("tools"), item("workflow"), item("thinking"), item("workflow-wakeup"), item("text")];
  assert.deepEqual(splitFlowSegments(items, 0, 5, 0), [
    { kind: "process", indices: [1] },
    { kind: "sticky", index: 2 },
    { kind: "process", indices: [3] },
    { kind: "sticky", index: 4 },
  ]);
});

test("分段：挂靠了节点卡的条目之后紧跟挂靠段", () => {
  const items = [item("user"), item("tools"), item("workflow"), item("text"), item("text"), item("text"), item("text")];
  assert.deepEqual(splitFlowSegments(items, 0, 7, 0, new Set([4, 5, 6])), [
    { kind: "process", indices: [1] },
    { kind: "sticky", index: 2 },
    { kind: "process", indices: [3, 4] },
    { kind: "attached", index: 4 },
    { kind: "process", indices: [5] },
    { kind: "attached", index: 5 },
    { kind: "process", indices: [6] },
    { kind: "attached", index: 6 },
  ]);
});

test("回退挂靠：无派发时刻时，节点卡挂到同轮、同程序工作流卡之后", () => {
  const items = [item("user"), { kind: "workflow", programId: PROGRAM, turnId: T } as TurnItem, item("text")];
  const { attached, unplaced } = placeWorkflowNodeCards(items, [
    card(23, T, PROGRAM),
    card(21, T, PROGRAM),
    card(22, T, PROGRAM),
    card(30, T, null),
    card(31, T, 99),
  ]);
  assert.deepEqual(attached.get(1)?.map((c) => c.agentId), [21, 22, 23]);
  assert.deepEqual(unplaced.map((c) => c.agentId), [30, 31]);
});

test("回退挂靠：同程序多张工作流卡时同轮优先，本轮无同轮卡则取该程序的最后一张卡", () => {
  const items = [
    { kind: "workflow", programId: PROGRAM, turnId: 10 } as TurnItem,
    item("text"),
    { kind: "workflow", programId: PROGRAM, turnId: 11 } as TurnItem,
  ];
  const { attached } = placeWorkflowNodeCards(items, [card(21, 10, PROGRAM), card(22, 11, PROGRAM), card(23, 12, PROGRAM)]);
  assert.deepEqual(attached.get(0)?.map((c) => c.agentId), [21]);
  assert.deepEqual(attached.get(2)?.map((c) => c.agentId), [22, 23]);
});

test("定位：节点卡位于派发前最后一个条目之后，而不是工作流卡之后（与真实会话同形）", () => {
  const items = timelineItems(interleaved(11));
  const cards = [card(21, T, PROGRAM, SPAWN_21), card(22, T, PROGRAM, SPAWN_22), card(23, T, PROGRAM, SPAWN_23)];
  assert.deepEqual(renderOrder(items, cards), [
    "item:1",
    "item:2",
    "item:3",
    "item:4",
    "card:21",
    "item:5",
    "card:22",
    "item:6",
    "card:23",
    "item:7",
  ]);
});

test("定位：派发时刻早于所有条目（或只能锚定首条用户消息）时回退到工作流卡", () => {
  const items = timelineItems(interleaved(11));
  const { attached } = placeWorkflowNodeCards(items, [card(21, T, PROGRAM, 1)]);
  assert.equal(attached.has(0), false, "首条用户消息不作锚点");
  assert.deepEqual(attached.get(2)?.map((c) => c.agentId), [21]);
});

test("漂移回归：内容逐步追加（节点陆续启动、消息陆续落库、最终汇报落库）时，已渲染项的相对顺序不变", () => {
  // [已落库消息的最大 id, 已启动的节点子代理]
  const steps: Array<[number, Card[]]> = [
    [5, []],
    [5, [card(21, T, PROGRAM, SPAWN_21)]],
    [7, [card(21, T, PROGRAM, SPAWN_21), card(22, T, PROGRAM, SPAWN_22)]],
    [9, [card(21, T, PROGRAM, SPAWN_21), card(22, T, PROGRAM, SPAWN_22), card(23, T, PROGRAM, SPAWN_23)]],
    [11, [card(21, T, PROGRAM, SPAWN_21), card(22, T, PROGRAM, SPAWN_22), card(23, T, PROGRAM, SPAWN_23)]],
  ];
  let prev: string[] | null = null;
  let last: string[] = [];
  for (const [upto, cards] of steps) {
    const order = renderOrder(timelineItems(interleaved(upto)), cards);
    if (prev) {
      const kept = order.filter((k) => prev!.includes(k));
      assert.deepEqual(kept, prev, `消息 id ≤ ${upto} 时既有项的相对顺序被改变`);
    }
    prev = order;
    last = order;
  }
  assert.deepEqual(last, [
    "item:1",
    "item:2",
    "item:3",
    "item:4",
    "card:21",
    "item:5",
    "card:22",
    "item:6",
    "card:23",
    "item:7",
  ]);
});

test("漂移回归（旧数据回退）：无派发时刻时，最终汇报落库前后工作流卡与节点卡的相对顺序不变", () => {
  // [消息条数, 已启动的节点子代理]（无派发时刻，回退到工作流卡之后）
  const steps: Array<[number, number[]]> = [
    [3, []],
    [3, [21]],
    [4, [21, 22]],
    [4, [21, 22, 23]],
    [5, [21, 22, 23]],
    [6, [21, 22, 23]],
    [7, [21, 22, 23]],
  ];
  let prev: string[] | null = null;
  let last: string[] = [];
  for (const [upto, agents] of steps) {
    const order = renderOrder(timelineItems(scenario(upto)), agents.map((id) => card(id, T, PROGRAM)));
    if (prev) {
      const kept = order.filter((k) => prev!.includes(k));
      assert.deepEqual(kept, prev, `消息数 ${upto}、节点 ${agents.join(",")} 时既有项的相对顺序被改变`);
    }
    prev = order;
    last = order;
  }
  assert.deepEqual(last, [
    "item:1",
    "item:2",
    "card:21",
    "card:22",
    "card:23",
    "item:3",
    "item:4",
    "item:5",
    "item:6",
  ]);
});

let failed = 0;
for (const [name, fn] of cases) {
  try {
    fn();
    console.log(`ok - ${name}`);
  } catch (err) {
    failed++;
    console.error(`not ok - ${name}`);
    console.error(err);
  }
}
console.log(`\n${cases.length - failed}/${cases.length} passed`);
if (failed > 0) process.exitCode = 1;
