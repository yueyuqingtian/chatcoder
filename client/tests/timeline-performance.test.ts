/**
 * 时间线增量构建回归测试（plan-75-334 阶段2）。
 *
 * 纯逻辑测试：不依赖浏览器 DOM，直接构造 MessageOut 列表，验证
 *   ① 追加快路径：只重建尾部 turn，其余 entry 引用逐项复用；
 *   ② 回退安全路径：REST 刷新 / 前缀引用变化 / 乱序插入 / 长度缩短时结果仍正确；
 *   ③ 内容一致性：快路径结果与全量 buildTimeline 在结构上完全一致。
 *
 * 运行方式（client 目录，无测试框架，用 esbuild 打包后由 node 直接执行）：
 *   npx esbuild tests/timeline-performance.test.ts --bundle --format=esm --platform=node \
 *     --alias:@chatcoder/shared=../packages/shared/src/index.ts --outfile=.tmp-timeline-test.mjs
 *   node .tmp-timeline-test.mjs
 */
import assert from "node:assert/strict";
import { buildTimeline, createTimelineBuilder } from "../src/components/chat/timeline";
import type { TimelineEntry, ToolNode } from "../src/components/chat/timeline";
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

/** 用户消息（时间线中独立成组或作为 turn 首条）。 */
function userMsg(id: number, turnId: number | null, text: string): MessageOut {
  return msg(id, turnId, MsgType.Text, { text }, SenderType.User);
}

/** AI 正文。 */
function textMsg(id: number, turnId: number | null, text: string): MessageOut {
  return msg(id, turnId, MsgType.Text, { text });
}

/** 工具调用（普通工具，进 ToolTree）。 */
function callMsg(id: number, turnId: number | null, tool: string, callKey: string): MessageOut {
  return msg(id, turnId, MsgType.ToolCall, { tool, call_key: callKey, args: {} });
}

/** 工具结果。 */
function resultMsg(id: number, turnId: number | null, tool: string, callKey: string): MessageOut {
  return msg(id, turnId, MsgType.ToolResult, { tool, call_key: callKey, ok: true, output: "" });
}

/** 思考消息。 */
function thinkMsg(id: number, turnId: number | null, text: string): MessageOut {
  return msg(id, turnId, MsgType.Thinking, { text });
}

/** 工具节点签名（叶子的 callKey 顺序，用于跨构建方式的内容比对）。 */
function nodeSig(nodes: ToolNode[]): string {
  return nodes
    .map((n) => {
      if (n.kind === "group") return `G(${n.tool}:${n.leaves.map((l) => l.callKey).join("+")})`;
      if (n.kind === "leaf") return `L(${n.leaf.callKey}:${n.leaf.ok === null ? "-" : n.leaf.ok})`;
      return `${n.kind}(${n.leaves.map((l) => l.callKey).join("+")})`;
    })
    .join("");
}

/** 时间线结构签名：kind / turnId / item 顺序 / 消息 id / 工具节点，全部可比。 */
function signature(entries: TimelineEntry[]): string {
  return entries
    .map((e) => {
      if (e.kind === "standalone") return `S:${e.msg.id}`;
      const items = e.items
        .map((it) => {
          if (it.kind === "tools") return `tools[${nodeSig(it.nodes)}]`;
          return `${it.kind}:${it.msg.id}`;
        })
        .join(",");
      return `T:${e.turnId ?? "null"}[${items}]`;
    })
    .join("|");
}

/** 构造一段典型历史：3 个 turn，含用户消息、思考、工具调用与结果、AI 正文。 */
function baseHistory(): MessageOut[] {
  return [
    userMsg(1, 1, "第一个问题"),
    thinkMsg(2, 1, "想一下"),
    callMsg(3, 1, "fs_read", "c1"),
    resultMsg(4, 1, "fs_read", "c1"),
    textMsg(5, 1, "第一个回答"),
    userMsg(6, 2, "第二个问题"),
    callMsg(7, 2, "fs_grep", "c2"),
    resultMsg(8, 2, "fs_grep", "c2"),
    textMsg(9, 2, "第二个回答"),
    userMsg(10, 3, "第三个问题"),
    textMsg(11, 3, "第三个回答"),
  ];
}

let passed = 0;
function check(name: string, fn: () => void): void {
  try {
    fn();
    passed += 1;
    console.log(`  通过：${name}`);
  } catch (err) {
    console.error(`  失败：${name}`);
    console.error(err);
    process.exitCode = 1;
  }
}

console.log("时间线增量构建回归测试（plan-75-334 阶段2）");

// ① 首次构建：与全量构建器结果一致
check("首次构建与 buildTimeline 结构一致", () => {
  const builder = createTimelineBuilder();
  const first = builder(baseHistory());
  assert.equal(signature(first), signature(buildTimeline(baseHistory())));
});

// ② 追加快路径：尾部 turn 追加消息，历史 turn 的 entry 引用必须逐项复用
check("追加快路径：只重建尾部 turn，历史 entry 引用不变", () => {
  const builder = createTimelineBuilder();
  const h1 = baseHistory();
  const e1 = builder(h1);

  // 追加一条属于尾部 turn（turn 3）的新消息
  const h2 = [...h1, textMsg(12, 3, "第三个回答的补充")];
  const e2 = builder(h2);

  assert.equal(e2.length, e1.length, "entry 数量不变");
  // 前两个 turn 的 entry 必须是同一对象引用
  assert.equal(e2[0], e1[0], "turn 1 entry 引用复用");
  assert.equal(e2[1], e1[1], "turn 2 entry 引用复用");
  // 尾部 turn 必须重建（内容变了）
  assert.notEqual(e2[2], e1[2], "尾部 turn 重建");
  // 内容与全量构建一致
  assert.equal(signature(e2), signature(buildTimeline(h2)));
});

// ③ 追加全新 turn：历史 entry 引用不变，末尾新增 entry
check("追加全新 turn：历史 entry 引用不变且末尾新增", () => {
  const builder = createTimelineBuilder();
  const h1 = baseHistory();
  const e1 = builder(h1);

  const h2 = [...h1, userMsg(12, 4, "第四个问题"), textMsg(13, 4, "第四个回答")];
  const e2 = builder(h2);

  assert.equal(e2.length, e1.length + 1, "新增一个 entry");
  assert.equal(e2[0], e1[0], "turn 1 引用复用");
  assert.equal(e2[1], e1[1], "turn 2 引用复用");
  assert.equal(e2[2], e1[2], "turn 3 引用复用（未参与追加）");
  assert.equal(signature(e2), signature(buildTimeline(h2)));
});

// ④ null-turn 用户消息追加：独立成组，不改变既有 turn 归属
check("null-turn 用户消息追加：独立成组且历史引用不变", () => {
  const builder = createTimelineBuilder();
  const h1 = baseHistory();
  const e1 = builder(h1);

  const h2 = [...h1, userMsg(12, null, "独立的消息")];
  const e2 = builder(h2);

  assert.equal(e2.length, e1.length + 1, "新增独立 entry");
  assert.equal(e2[0], e1[0], "turn 1 引用复用");
  assert.equal(e2[1], e1[1], "turn 2 引用复用");
  assert.equal(e2[2], e1[2], "turn 3 引用复用");
  assert.equal(signature(e2), signature(buildTimeline(h2)));
});

// ⑤ 回退：前缀引用变化（乐观消息替换）后结果仍与全量一致
check("回退路径：前缀引用被替换（乐观替换）", () => {
  const builder = createTimelineBuilder();
  const h1 = baseHistory();
  const e1 = builder(h1);

  // 替换第 3 条消息（模拟乐观占位被真实消息替换）
  const h2 = h1.slice();
  h2[2] = callMsg(3, 1, "fs_read", "c3-replaced");
  const e2 = builder(h2);

  assert.equal(signature(e2), signature(buildTimeline(h2)), "内容正确");
  // 前缀变了 ⇒ 该 turn 必须重建；未受影响的 turn 仍应复用
  assert.notEqual(e2[0], e1[0], "被替换消息所在 turn 重建");
  assert.equal(e2[2], e1[2], "未受影响的 turn 3 引用复用");
});

// ⑥ 回退：乱序插入属于旧 turn 的消息（REST 合并混入）
check("回退路径：给已存在的旧 turn 追加消息（乱序）", () => {
  const builder = createTimelineBuilder();
  const h1 = baseHistory();
  const e1 = builder(h1);

  const h2 = [...h1, textMsg(12, 1, "迟到落库的 turn 1 消息")];
  const e2 = builder(h2);

  assert.equal(signature(e2), signature(buildTimeline(h2)), "内容与全量一致");
  assert.notEqual(e2[0], e1[0], "turn 1 重建");
  // 顺序不能变：turn 1 仍在最前
  assert.equal(e2[0].kind === "turn" ? e2[0].turnId : null, 1, "turn 1 仍在最前");
});

// ⑦ 回退：长度缩短（回滚/切换会话残留）
check("回退路径：消息数量缩短（回滚）", () => {
  const builder = createTimelineBuilder();
  builder(baseHistory());
  const h2 = baseHistory().slice(0, 6);
  const e2 = builder(h2);

  assert.equal(signature(e2), signature(buildTimeline(h2)));
  assert.equal(e2.length, 2, "只剩两个 turn");
});

// ⑧ 相同数组引用：直接复用上次结果
check("相同输入引用直接复用上次结果", () => {
  const builder = createTimelineBuilder();
  const h = baseHistory();
  const e1 = builder(h);
  const e2 = builder(h);
  assert.equal(e1, e2, "同引用输入返回同一结果数组");
});

// ⑨ 连续追加一致性：模拟流式落库，逐条追加后与全量比对
check("连续 30 次追加：每次结果与全量构建一致", () => {
  const builder = createTimelineBuilder();
  let history = baseHistory();
  builder(history);
  for (let i = 0; i < 30; i++) {
    const id = 100 + i;
    const next = i % 3 === 2 ? userMsg(id, null, `补充 ${i}`) : textMsg(id, 3, `流式追加 ${i}`);
    history = [...history, next];
    const got = builder(history);
    assert.equal(signature(got), signature(buildTimeline(history)), `第 ${i + 1} 次追加内容一致`);
  }
});

// ⑩ 引用复用统计：长会话追加一条消息时，未变化 turn 的复用率
check("长会话（120 turn）追加一条消息：历史 entry 复用率 > 90%", () => {
  const builder = createTimelineBuilder();
  const history: MessageOut[] = [];
  let id = 1;
  for (let t = 1; t <= 120; t++) {
    history.push(userMsg(id++, t, `问题 ${t}`));
    history.push(textMsg(id++, t, `回答 ${t}`));
  }
  const e1 = builder(history);
  const tail = textMsg(id, 120, "尾部追加");
  const appended = [...history, tail];
  const e2 = builder(appended);

  let reused = 0;
  for (let i = 0; i < e1.length; i++) if (e2[i] === e1[i]) reused += 1;
  const rate = reused / e1.length;
  console.log(`    复用 ${reused}/${e1.length}（${(rate * 100).toFixed(1)}%）`);
  assert.ok(rate > 0.9, `复用率应 > 90%，实际 ${(rate * 100).toFixed(1)}%`);
  assert.equal(signature(e2), signature(buildTimeline(appended)));
});

/** 取某 entry 里的 tools 项节点（用例内断言配对结果用）。 */
function toolsNodesOf(entry: TimelineEntry | undefined): ToolNode[] {
  if (!entry || entry.kind !== "turn") return [];
  const item = entry.items.find((it) => it.kind === "tools");
  return item && item.kind === "tools" ? item.nodes : [];
}

// ⑪ 工具结果落库：尾部 turn 追加 tool_result，工具树必须完成配对且历史引用不变
check("追加快路径：尾部 turn 追加工具结果（tool_result 落库）", () => {
  const builder = createTimelineBuilder();
  const history: MessageOut[] = [
    userMsg(1, 1, "问题一"),
    callMsg(2, 1, "fs_read", "a1"),
    resultMsg(3, 1, "fs_read", "a1"),
    textMsg(4, 1, "回答一"),
    userMsg(5, 2, "问题二"),
    callMsg(6, 2, "fs_grep", "b1"),
  ];
  const e1 = builder(history);
  // 结果未到达时工具叶子 ok=null
  const before = toolsNodesOf(e1[1]);
  assert.equal(before.length, 1, "工具行已渲染");
  assert.ok(before.every((n) => n.kind !== "leaf" || n.leaf.ok === null), "结果未到达时 ok=null");

  // 工具结果与后续正文落库
  const history2 = [...history, resultMsg(7, 2, "fs_grep", "b1"), textMsg(8, 2, "回答二")];
  const e2 = builder(history2);

  assert.equal(e2[0], e1[0], "历史 turn 1 引用复用");
  assert.notEqual(e2[1], e1[1], "尾部 turn 2 重建");
  assert.equal(signature(e2), signature(buildTimeline(history2)), "内容与全量一致");
  const after = toolsNodesOf(e2[1]);
  assert.ok(after.some((n) => n.kind === "leaf" && n.leaf.ok === true), "结果落库后配对成功（ok=true）");
});

// ⑫ 乐观用户消息替换：同 id、对象引用不同的用户消息替换后，回退安全路径且内容正确
check("回退路径：乐观用户消息被替换（同 id 新引用）", () => {
  const builder = createTimelineBuilder();
  const history: MessageOut[] = [
    userMsg(1, 1, "原始问题"),
    textMsg(2, 1, "回答"),
    userMsg(3, null, "待发送"),
  ];
  const e1 = builder(history);

  // 后端回执替换同 id 的用户消息对象（乐观占位 → 真实落库）
  const replaced = userMsg(3, null, "待发送");
  const history2 = [history[0], history[1], replaced];
  const e2 = builder(history2);

  assert.equal(signature(e2), signature(buildTimeline(history2)), "内容与全量一致");
  // 前缀（前两条）引用未变 ⇒ turn 1 应继续复用
  assert.equal(e2[0], e1[0], "未变化 turn 引用复用");
  // 被替换的用户消息独立成组（第 2 个 entry）：必须重建为包含新对象的那条
  assert.notEqual(e2[1], e1[1], "被替换的用户消息 entry 重建");
  const replacedEntry = e2[1];
  assert.ok(
    replacedEntry.kind === "turn"
      && replacedEntry.items[0].kind === "user"
      && (replacedEntry.items[0].msg as MessageOut) === replaced,
    "entry 指向被替换后的消息对象",
  );
});

console.log(`\n完成：${passed} 项通过${process.exitCode ? "，存在失败项" : "，全部通过"}`);
