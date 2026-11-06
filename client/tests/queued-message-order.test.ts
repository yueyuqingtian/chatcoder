/** plan-20261105-queued-message-order：后台唤醒中途注入不能被当作轮次顶部的发起消息。 */
import assert from "node:assert/strict";
import { buildTimeline, createTimelineBuilder, createRoundMerger, leadingUserIndex, splitFlowSegments, firstIdOf, placeWorkflowNodeCards } from "../src/components/chat/timeline";
import type { TimelineEntry, TurnItem } from "../src/components/chat/timeline";
import type { MessageOut } from "../src/api/client";
import { MsgType, SenderType } from "@chatcoder/shared";

function msg(id: number, turn: number, text: string, user = false, extra = {}): MessageOut {
  return { id, session_id: 1, turn_id: turn, thread_id: null, sender_type: user ? SenderType.User : SenderType.Agent,
    sender_id: null, msg_type: MsgType.Text, content: { text, ...extra }, created_at: null };
}
function itemsOf(entries: TimelineEntry[]): TurnItem[] {
  assert.equal(entries.length, 1);
  const entry = entries[0];
  assert.equal(entry.kind, "turn");
  return entry.kind === "turn" ? entry.items : [];
}
// 对齐 TurnGroup：顶部用户消息单独渲染，其余按切段后的顺序渲染。
function displayedIds(items: TurnItem[]): Array<number | null> {
  const top = leadingUserIndex(items);
  const segments = splitFlowSegments(items, 0, items.length, top);
  return [...(top >= 0 ? [firstIdOf(items[top])] : []), ...segments.flatMap((s) =>
    s.kind === "process" ? s.indices.map((i) => firstIdOf(items[i])) : s.kind === "sticky" ? [firstIdOf(items[s.index])] : [])];
}

const wakeup = msg(10, 2, "后台命令完成", true, { workflow_wakeup: true, wakeup_source: "bg_command" });
const messages = [wakeup, msg(11, 2, "编译通过，继续修文档"), msg(12, 2, "用户补充要求", true), msg(13, 2, "收到补充，继续执行")];
const full = itemsOf(buildTimeline(messages));
assert.equal(leadingUserIndex(full), -1, "后台唤醒后的首条用户消息是中途注入，不能成为顶部消息");
assert.deepEqual(displayedIds(full), [10, 11, 12, 13], "用户消息必须位于此前执行内容之后");

const incremental = createTimelineBuilder();
incremental(messages.slice(0, 2));
incremental(messages.slice(0, 3));
assert.deepEqual(displayedIds(itemsOf(incremental(messages))), [10, 11, 12, 13], "增量到达与刷新完整重建顺序一致");

const initial = [msg(1, 1, "开始任务", true), msg(2, 1, "已启动后台命令")];
const merger = createRoundMerger();
assert.deepEqual(displayedIds(itemsOf(merger(buildTimeline([...initial, ...messages.slice(0, 2)])))), [1, 2, 10, 11], "注入前续跑合并保持时间顺序");
const afterInjection = merger(buildTimeline([...initial, ...messages]));
assert.equal(afterInjection.length, 2, "注入后续跑仍沿用既有独立轮次语义");
assert.deepEqual(afterInjection.flatMap((e) => e.kind === "turn" ? displayedIds(e.items) : []), [1, 2, 10, 11, 12, 13], "注入后即使拆出续跑轮也不提升用户气泡");

const normal = itemsOf(buildTimeline([msg(20, 3, "正常发起", true), msg(21, 3, "执行"), msg(22, 3, "中途补充", true)]));
assert.equal(leadingUserIndex(normal), 0, "正常轮次仍将发起消息放在顶部");
assert.deepEqual(displayedIds(normal), [20, 21, 22]);
assert.equal(leadingUserIndex([]), -1);

const placed = placeWorkflowNodeCards(full, [{ agentId: 7, turnId: 2, spawnMessageId: 12 }]);
assert.equal(placed.attached.get(2)?.[0].agentId, 7, "中途输入仍是后续子代理的有效时间锚点");
console.log("通过：后台唤醒注入、普通轮次、增量重建、续跑合并与子代理挂靠排序回归");
