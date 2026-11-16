/** plan-20261114-message-flow：覆盖预览阶段接力、已完成工具收口及分组身份，不模拟实现细节。 */
import assert from "node:assert/strict";
import { MsgType, type MessageOut } from "@chatcoder/shared";
import { buildTimeline, chunkProcessIndices, createTimelineBuilder, splitFlowSegments, type TurnItem } from "../src/components/chat/timeline";
import { processGroupState } from "../src/components/chat/processGroupState";
import { subagentActivity, activityTarget } from "../src/components/chat/subagentActivity";

function message(id: number, type: string, content: Record<string, unknown>): MessageOut {
  return { id, session_id: 1, turn_id: 10, thread_id: 4, sender_type: "agent", sender_id: 4, msg_type: type, content, created_at: null };
}
const thought = message(1, MsgType.Thinking, { text: "先检查实现" });
const read = message(2, MsgType.ToolCall, { tool: "fs_read", args: { path: "src/a.ts" }, call_key: "read" });
const readDone = message(3, MsgType.ToolResult, { tool: "fs_read", call_key: "read", ok: true });
const run = message(4, MsgType.ToolCall, { tool: "terminal_exec", args: { command: "npm run build" }, call_key: "run" });
const runDone = message(5, MsgType.ToolResult, { tool: "terminal_exec", call_key: "run", ok: true });
const text = message(6, MsgType.Text, { text: "检查完成" });
const msgs = [thought, read, readDone, run];
function itemsOf(messages: MessageOut[]): TurnItem[] {
  const entry = buildTimeline(messages)[0];
  assert.equal(entry.kind, "turn");
  return entry.items;
}
const items = itemsOf(msgs);
const indices = items.map((_, index) => index);
const state = processGroupState(items, indices, true);
assert.deepEqual(state.calls.map((call) => call.key), ["read", "run"], "已完成的快速调用仍保留在轮播队列的数据源中");
assert.equal(state.verb, "探索");
assert.deepEqual(state.previews, ["正在运行 npm run build"], "完成的读取不能参与运行轮播");
assert.equal(state.summary, "1 段思考 · 1 次读取 · 1 次命令");
assert.deepEqual(processGroupState(items, indices, false).previews, [], "轮次结束后残留调用不能继续显示运行");
assert.deepEqual(processGroupState(items, indices, true, { run: { ok: true } }).previews, [], "实时结果应立即终止预览，不等待落库");
assert.equal(processGroupState(items, indices, true, { run: { ok: true, change_stat: { path: "a.ts", additions: 2, deletions: 1 } } }).tool, "editor_apply_diff", "命令实际写入文件时实时切换编辑图标");

const editItems = itemsOf([...msgs, runDone, message(6, MsgType.ToolCall, { tool: "editor_apply_diff", call_key: "edit", args: { path: "a.ts" } })]);
const editState = processGroupState(editItems, editItems.map((_, i) => i), true);
assert.equal(editState.verb, "编辑");
assert.equal(editState.tool, "editor_apply_diff");
assert.ok(editState.previews.every((preview) => !preview.includes("npm run build")));

assert.equal(subagentActivity({ messages: msgs, running: true }), "1|正在运行 npm run build");
assert.equal(subagentActivity({ messages: msgs, thinking: "重新分析结果", running: true }), "1|正在思考分析", "新思考不能被旧工具挡住");
assert.equal(subagentActivity({ messages: msgs, text: "正在整理\n最新进度", running: true }), "1|最新进度", "正文预览随最新输出更新");
assert.equal(subagentActivity({ messages: msgs, results: { run: { ok: true } }, running: true }), "0|已运行 npm run build");
assert.equal(subagentActivity({ messages: [...msgs, runDone], running: true }), "0|已运行 npm run build");
assert.equal(subagentActivity({ messages: [...msgs, runDone, text], text: "检查完成", thinking: "新的判断", running: true }), "1|正在思考分析", "重复正文缓冲不能遮住新思考");
assert.equal(subagentActivity({ messages: [...msgs, runDone, text], text: "残留正文", thinking: "残留思考", running: false }), "0|检查完成");
assert.equal(activityTarget({ file_path: "D:\\project\\src\\a.ts" }), "a.ts");

const dispatch = message(7, MsgType.ToolCall, { tool: "spawn_subagent", args: { task_title: "核查" }, call_key: "spawn" });
const afterDispatch = message(8, MsgType.Thinking, { text: "继续主任务" });
const all = [...msgs, runDone, dispatch, afterDispatch];
const allItems = itemsOf(all);
const chunks = chunkProcessIndices(allItems, allItems.map((_, i) => i));
assert.deepEqual(chunks.map((chunk) => chunk.kind), ["group", "item", "item"]);
assert.deepEqual(splitFlowSegments(allItems, 0, allItems.length, -1).map((segment) => segment.kind), ["process"], "独立派发卡也应随外层工作过程折叠");
const builder = createTimelineBuilder();
builder(all.slice(0, 2));
builder(all.slice(0, 4));
assert.deepEqual(builder(all), buildTimeline(all), "增量接力、工具结果晚到与完整回放保持同样时序");
console.log("通过：过程组分类、运行预览、实时结果收口、子代理活动接力与独立派发时序");
