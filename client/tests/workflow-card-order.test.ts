/**
 * 工作流卡与节点子代理卡按创建序固化的回归测试（plan-20261013-wf-order）。
 *
 * 纯逻辑测试（不依赖 DOM），验证：
 *   ① 时间线：工作流卡记录其工具调用所在的轮；
 *   ② 分段：常显项（工作流卡、工作流唤醒分隔线）各自成段，过程项只在常显项之间连续归段；
 *   ③ 挂靠：节点子代理挂到同轮同程序的工作流卡之后，按创建序排列，挂不到的原样交给兜底；
 *   ④ 漂移回归：内容逐步追加（节点陆续启动、唤醒、最终汇报落库）时，已渲染项的相对顺序不变。
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
import type { TimelineEntry, TurnItem, WorkflowNodeCard } from "../src/components/chat/timeline";
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

/** 子代理卡片（只含挂靠所需字段）。 */
interface Card extends WorkflowNodeCard {
  name: string;
}

function card(agentId: number, turnId: number | null, programId: number | null): Card {
  return { agentId, name: `audit•${agentId}`, turnId, programId };
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
 * 每个工作流卡之后紧跟其挂靠的节点子代理卡。
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
  const hasProcess =
    finalIdx >= 0 && items.some((it, i) => i !== firstUser && i < finalIdx && !isOutsideProcess(it));
  const segments = hasProcess
    ? splitFlowSegments(items, 0, finalIdx, firstUser)
    : splitFlowSegments(items, 0, items.length, firstUser);

  const order: string[] = [];
  const emit = (index: number) => {
    order.push(`item:${index}`);
    for (const c of attached.get(index) ?? []) order.push(`card:${c.agentId}`);
  };
  for (const seg of segments) {
    if (seg.kind === "sticky") emit(seg.index);
    else for (const index of seg.indices) emit(index);
  }
  if (hasProcess) {
    for (let i = finalIdx; i < items.length; i++) if (i !== firstUser) emit(i);
  }
  return order;
}

const cases: Array<[string, () => void]> = [];
function test(name: string, fn: () => void) {
  cases.push([name, fn]);
}

test("时间线：工作流卡记录其工具调用所在的轮", () => {
  const card = timelineItems(scenario(3)).find((it) => it.kind === "workflow");
  assert.deepEqual(card, { kind: "workflow", programId: PROGRAM, turnId: T });
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

test("挂靠：节点卡挂到同轮同程序工作流卡之后，按创建序排列；无程序或无对应卡的原样交给兜底", () => {
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

test("挂靠：同程序多张工作流卡时同轮优先，本轮无同轮卡则取该程序的最后一张卡", () => {
  const items = [
    { kind: "workflow", programId: PROGRAM, turnId: 10 } as TurnItem,
    item("text"),
    { kind: "workflow", programId: PROGRAM, turnId: 11 } as TurnItem,
  ];
  const { attached } = placeWorkflowNodeCards(items, [card(21, 10, PROGRAM), card(22, 11, PROGRAM), card(23, 12, PROGRAM)]);
  assert.deepEqual(attached.get(0)?.map((c) => c.agentId), [21]);
  assert.deepEqual(attached.get(2)?.map((c) => c.agentId), [22, 23]);
});

test("漂移回归：内容逐步追加（节点陆续启动、唤醒、最终汇报落库）时，已渲染项的相对顺序不变", () => {
  // [消息条数, 已启动的节点子代理]
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
  // 创建序：工作流卡 → 其节点卡（按子代理创建序）→ 已启动文字 → 唤醒 → 工具 → 最终汇报
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
