/**
 * 时序过程合并与退出码 1 元数据透传测试（plan-flow-redesign）。
 */
import assert from "node:assert/strict";
import {
  buildTimeline,
  chunkProcessIndices,
  countProcessSteps,
  flattenClusterNodes,
  splitFlowSegments,
  leadingUserIndex,
} from "../src/components/chat/timeline";
import { MsgType, SenderType } from "@chatcoder/shared";
import type { MessageOut } from "@chatcoder/shared";

function msg(
  id: number,
  turnId: number,
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

// 1. 连续思考和工具合并；子代理保持独立并切断相邻分组。
const messages: MessageOut[] = [
  msg(1, 10, MsgType.Text, { text: "帮我排查" }, SenderType.User),
  msg(2, 10, MsgType.Text, { text: "我先检查一下：" }),
  msg(3, 10, MsgType.Text, { text: "核对 terminal.py", thinking: true }),
  msg(4, 10, MsgType.ToolCall, { tool: "fs_grep", call_key: "k1", args: { query: "returncode" } }),
  msg(5, 10, MsgType.ToolResult, { tool: "fs_grep", call_key: "k1", ok: true, output: "terminal.py:458: rc = ..." }),
  msg(6, 10, MsgType.ToolCall, { tool: "terminal_exec", call_key: "k2", args: { command: 'rg "x" src' } }),
  msg(7, 10, MsgType.ToolResult, {
    tool: "terminal_exec", call_key: "k2", ok: true,
    output: "未找到匹配项（退出码 1）", exit_note: "无匹配（退出码 1）", returncode: 1,
  }),
  msg(8, 10, MsgType.ToolCall, { tool: "spawn_subagent", call_key: "k3", args: { task_title: "前端调研", explore: true } }),
  msg(9, 10, MsgType.ToolResult, { tool: "spawn_subagent", call_key: "k3", ok: true, output: "Subagent #42 started" }),
  msg(10, 10, MsgType.Text, { text: "定位完成，开始修复：" }),
  msg(11, 10, MsgType.ToolCall, { tool: "fs_read", call_key: "k4", args: { path: "a.ts" } }),
  msg(12, 10, MsgType.ToolResult, { tool: "fs_read", call_key: "k4", ok: true, output: "code" }),
  msg(13, 10, MsgType.Text, { text: "全部完成。" }),
];

const entries = buildTimeline(messages);
assert.equal(entries.length, 1);
const turn = entries[0];
assert.equal(turn.kind, "turn");
const skip = leadingUserIndex(turn.items);
const segs = splitFlowSegments(turn.items, 0, turn.items.length, skip);
assert.equal(segs.length, 1, "子代理应与前后工具处于同一可折叠过程段");
assert.equal(segs[0].kind, "process");
assert.deepEqual(segs[0].indices, turn.items.map((_, index) => index).filter((index) => index !== skip));

const chunks = chunkProcessIndices(turn.items, turn.items.map((_, index) => index).filter((index) => index !== skip));
// 正文 → 思考/工具组 → 子代理 → 正文 → 单工具 → 正文
assert.equal(chunks.length, 6);
assert.equal(chunks[0].kind, "item"); // "我先检查一下："
assert.equal(chunks[1].kind, "group");
if (chunks[1].kind === "group") {
  assert.equal(countProcessSteps(turn.items, chunks[1].indices), 3);
  assert.ok(chunks[1].indices.every((index) => turn.items[index].kind !== "subagent"));
}
assert.equal(chunks[2].kind, "item"); // 子代理独立显示
assert.equal(chunks[3].kind, "item"); // 正文
assert.equal(chunks[4].kind, "item"); // 单步读取直接平铺，不套壳
assert.equal(chunks[5].kind, "item"); // 最终汇报

// 2. 验证 flattenClusterNodes 展平 action-cluster 并保留 exitNote / returncode
const toolsItem = turn.items.find((it) => it.kind === "tools");
assert.ok(toolsItem && toolsItem.kind === "tools");
assert.equal(toolsItem.nodes[0].kind, "action-cluster");
const flat = flattenClusterNodes(toolsItem.nodes);
assert.equal(flat.length, 2);
assert.equal(flat[0].kind, "leaf");
assert.equal(flat[1].kind, "leaf");
if (flat[1].kind === "leaf") {
  assert.equal(flat[1].leaf.exitNote, "无匹配（退出码 1）");
  assert.equal(flat[1].leaf.returncode, 1);
  assert.equal(flat[1].leaf.ok, true);
}

console.log("process-group-timeline: all assertions passed");
