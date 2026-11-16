/** 输入框引用 chip 的发送后生命周期（plan-693-2868 P1 收尾修复）。
 *
 *  守住的缺陷：微应用 chip 曾按「发送后保留」处理，消息发出去后 chip 还挂在输入框上
 *  （看起来引用没提交），后续输入还会把它同步回草稿，切走再切回来又出现，下一条消息
 *  因此可能重复携带同一枚引用。
 */
import assert from "node:assert/strict";
import { refsAfterSend, deliveredFromQueue } from "../src/components/chat/composerRefsLifecycle";

const microapp = { kind: "microapp", value: "java-debug", label: "Java 调试台", version: "1.0.0" };
const file = { kind: "file", value: "src/app.ts", label: "app.ts" };
const skill = { kind: "skill", value: "microapp-create", label: "微应用创建" };
const dir = { kind: "dir", value: "D:\\proj", label: "proj" };

// ① 送达 → 全部清空：不允许任何 kind 被留下（曾把 microapp 定为「保留型」，正是缺陷根因）
assert.deepEqual(refsAfterSend([microapp, file, skill, dir], true), [],
  "发送成功后不能保留任何引用 chip（含微应用）");
assert.deepEqual(refsAfterSend([microapp], true), [], "只剩一枚微应用 chip 时也必须清空");

// ② 失败 → 原样保留：用户已经选好的引用不能丢，重试一次即可
const keptOnFailure = refsAfterSend([microapp, skill], false);
assert.deepEqual(keptOnFailure, [microapp, skill], "发送失败时引用 chip 必须原样保留");
assert.equal(keptOnFailure.length, 2);

// ③ 空引用两种分支都保持空（不产生无意义的新数组内容）
assert.deepEqual(refsAfterSend([], true), []);
assert.deepEqual(refsAfterSend([], false), []);

// ④ 「先取用再清空」：清空不能影响已经随消息取走的结构化副本（refPayload 是浅拷贝）
const refs = [microapp, file];
const payload = refs.map((r) => ({ ...r }));
const cleared = refsAfterSend(refs, true);
assert.deepEqual(payload, [{ ...microapp }, { ...file }], "清空 chip 后取走的 refs 副本必须完好");
assert.deepEqual(cleared, []);
assert.notEqual(cleared, refs, "清空后不能复用原数组引用（调用方可能持有它）");

// ⑤ 连续两轮：第一轮成功后清空 → 第二轮不会重复携带同一枚 chip
let current = [...refs];
current = refsAfterSend(current, true);
assert.deepEqual(current, [], "第一条消息送达后 chip 已清空");
assert.deepEqual(refsAfterSend(current, true), [], "第二条消息不会再次携带上一轮的引用");

// ⑥ 入队识别：sendTurn 的 null 分不清失败与「运行中入队」，用队列增量区分
const queuedBefore = new Set(["q-1"]);
assert.equal(deliveredFromQueue(queuedBefore, [{ id: "q-1" }]), false,
  "队列没有新增项 = 发送失败（chip 保留）");
assert.equal(deliveredFromQueue(queuedBefore, [{ id: "q-1" }, { id: "q-2" }]), true,
  "队列多出一条新项 = 已被接收（入队同样算送达，chip 清空）");
assert.equal(deliveredFromQueue(new Set<string>(), [{ id: "q-9" }]), true,
  "空队列 → 新增一条也算送达");
assert.equal(deliveredFromQueue(new Set(["q-1", "q-2"]), [{ id: "q-2" }]), false,
  "队列里原有项被消费（无新增）不能算作本次送达");

// ⑦ 失败后重试成功 → 清空（失败保留不是永久保留）
assert.deepEqual(refsAfterSend(refsAfterSend([microapp], false), true), [],
  "失败保留的 chip 在重试送达后必须清空");

console.log("通过：送达清空（无 kind 例外）、失败保留、先取用后清空、跨消息不重复携带、入队/失败区分");
