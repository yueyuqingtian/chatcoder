/** plan-20261112-subagent-placement：派发别名和截断标题不得让子代理在最终回复下方重复出现。 */
import assert from "node:assert/strict";
import { MsgType, SenderType, type MessageOut } from "@chatcoder/shared";
import { buildTimeline, createTimelineBuilder, matchSubagentDispatches, subagentIdOf, type TimelineEntry, type TurnItem } from "../src/components/chat/timeline";

function msg(id: number, msgType: string, content: Record<string, unknown>): MessageOut {
  return { id, session_id: 1, turn_id: 10, thread_id: null, sender_type: SenderType.Agent,
    sender_id: 1, msg_type: msgType, content, created_at: null };
}
function itemsOf(entries: TimelineEntry[]): TurnItem[] {
  return entries.flatMap((entry) => entry.kind === "turn" ? entry.items : []);
}
const title = "检查权限配置和保存回调在多个机构与角色之间的完整同步流程并核对所有历史数据异常与边界条件";
const shortTitle = Array.from(title).slice(0, 40).join("");
const cards = [{ agentId: 21, turnId: 10, name: shortTitle }, { agentId: 22, turnId: 10, name: "另一个任务" }];
const text = (id: number) => msg(id, MsgType.Text, { text: id === 1 ? "开始检查" : "最终汇报" });

for (const tool of ["spawn_subagent", "SubAgent", "SubAgentAsync"]) {
  const call = msg(2, MsgType.ToolCall, { tool, call_key: "spawn", args: { task_title: title } });
  // 实际回放可能混用别名与原名；结果迟到时位置与身份也必须保持稳定。
  const result = msg(3, MsgType.ToolResult, { tool: "SubAgent", call_key: "spawn", ok: true,
    output: `Subagent #21 dispatched in the background for task: ${title}.` });
  const messages = [text(1), call, result, text(4)];
  const incremental = createTimelineBuilder();
  for (const partial of [messages.slice(0, 2), messages.slice(0, 3), messages]) {
    const items = itemsOf(incremental(partial));
    assert.equal(items[1].kind, "subagent", `${tool} 应在派发位置独立显示`);
    const matched = matchSubagentDispatches(items, cards);
    assert.equal(matched.get(1)?.agentId, 21, "结果到达前后均应认领截断标题");
    const unplaced = cards.filter((card) => !new Set([...matched.values()].map((m) => m.agentId)).has(card.agentId));
    assert.deepEqual(unplaced.map((card) => card.agentId), [22], "已原位显示的子代理不得进入轮末兜底");
  }
  const full = itemsOf(buildTimeline(messages));
  assert.deepEqual(itemsOf(incremental(messages)), full, "增量与历史重建保持一致");
  assert.equal(full[2].kind, "text", "最终回复必须位于子代理之后");
  assert.ok(full[1].kind === "subagent" && full[1].result === result, "别名结果应关联派发条目");
}

const dispatch = (args: Record<string, unknown>, output?: string, ok = true): Extract<TurnItem, { kind: "subagent" }> => ({
  kind: "subagent", msg: msg(2, MsgType.ToolCall, { tool: "spawn_subagent", args }),
  result: output == null ? undefined : msg(3, MsgType.ToolResult, { output, ok }),
});
const renamed = dispatch({ task_title: "另一个任务" }, "Subagent #21 dispatched in the background for task: 重命名任务.");
assert.equal(matchSubagentDispatches([renamed], cards).get(0)?.agentId, 21, "ID 优先于名称");
assert.equal(matchSubagentDispatches([dispatch({ task_title: title }, "Subagent #99 dispatched in the background")], cards).size, 0,
  "已知 ID 尚未回填时不能按名称串用其他代理");
assert.equal(matchSubagentDispatches([dispatch({ task_title: "权限" })], [{ agentId: 23, name: "权限管理" }]).size, 0,
  "名称包含不能认领其他任务");
assert.equal(matchSubagentDispatches([dispatch({ task_title: title })], [...cards, { agentId: 24, turnId: 10, name: shortTitle }]).size, 0,
  "截断后重名必须等待结果提供 ID");
for (const ok of [true, false]) {
  const failed = dispatch({ task_title: title }, "Error: a subagent is already dispatched (#21)", ok);
  assert.equal(subagentIdOf(failed), null);
  assert.equal(matchSubagentDispatches([failed], cards).size, 0, "失败派发不能复用已有子代理");
}
assert.equal(subagentIdOf(dispatch({}, "Exploration subagent #21 finished with result")), 21);

for (const tool of ["resume_subagent", "ResumeSubagent"]) {
  const items = itemsOf(buildTimeline([text(1), msg(2, MsgType.ToolCall, { tool, call_key: "resume", args: { agent_id: 21 } }), text(4)]));
  assert.equal(items[1].kind, "subagent");
  assert.equal(matchSubagentDispatches(items, cards).get(1)?.agentId, 21, "续跑按 ID 原位认领");
}
const emojiTitle = "😀".repeat(41);
assert.equal(matchSubagentDispatches([dispatch({ task_title: emojiTitle })], [{ agentId: 25, name: "😀".repeat(40) }]).get(0)?.agentId, 25,
  "名称截断按 Unicode 字符与服务端保持一致");
console.log("通过：子代理别名、标题截断、身份匹配、失败派发、续跑、增量更新及历史重建落位回归");
