/**
 * 切段边界回归测试（plan-586-2374）。
 *
 * 为什么要有这条用例：`splitFlowSegments` 的起点常被传成「最终汇报下标」，
 * 而该值在没有最终汇报时是 -1（典型场景：刚发出一条新消息，本轮还没有 text 项）。
 * 一次性能改造把切段从「渲染分支内惰性求值」提升成了「无条件求值」，
 * 于是 `for (index = -1; …)` 读到了 `items[-1]` → `isOutsideProcess(undefined)`
 * 读 `.kind` 抛 `Cannot read properties of undefined (reading 'kind')`，
 * 整个应用白屏。修复方式是在函数内把范围钳到 `[0, items.length]`。
 *
 * 本用例锁住两条性质：
 *   ① 任何越界入参都不抛错（负起点、终点超界、起点大于终点）；
 *   ② 钳位不改变合法区间的切段结果（语义等价，不会把该显示的项吞掉）。
 *
 * 纯逻辑，Node 原生 TS 运行：`node tests/flow-segments-bounds.test.ts`（client 目录）。
 */
import { splitFlowSegments, type TurnItem } from "../src/components/chat/timeline.ts";
import type { MessageOut } from "@chatcoder/shared";

let failures = 0;
function assert(cond: boolean, msg: string) {
  if (!cond) { failures++; console.error(`FAIL: ${msg}`); }
  else { console.log(`ok: ${msg}`); }
}

/** 构造一个最小可用的 TurnItem（只填切段逻辑真正读取的字段：kind）。 */
function textItem(id: number): TurnItem {
  return {
    kind: "text",
    msg: { id, content: { text: `t${id}` } } as unknown as MessageOut,
  };
}

/** 一个「工作流卡」项：isOutsideProcess 返回 true，切段时自成 sticky 段。 */
function workflowItem(firstId: number): TurnItem {
  return { kind: "workflow", programId: 1, turnId: 1, firstId };
}

/** 段的形状简写，便于比较。 */
function shape(segs: ReturnType<typeof splitFlowSegments>): string {
  return segs
    .map((s) => (s.kind === "process" ? `p[${s.indices.join(",")}]` : `${s.kind}@${s.index}`))
    .join(" ");
}

const items: TurnItem[] = [textItem(1), workflowItem(2), textItem(3)];

// ① 本次白屏的直接复现：from = -1（没有最终汇报时的起点）不得抛错
try {
  const segs = splitFlowSegments(items, -1, 3, -1);
  assert(true, "from = -1 不抛错（白屏场景已消除）");
  // 钳到 0 之后，应与 from = 0 的结果完全一致
  assert(
    shape(segs) === shape(splitFlowSegments(items, 0, 3, -1)),
    "from = -1 的结果与 from = 0 等价（钳位不改变语义）",
  );
} catch (e) {
  assert(false, `from = -1 抛错：${(e as Error).message}`);
}

// ② 终点超界：不得多读一次空槽，也不得抛错
try {
  const segs = splitFlowSegments(items, 0, items.length + 5, -1);
  assert(true, "to 超界不抛错");
  assert(
    shape(segs) === shape(splitFlowSegments(items, 0, items.length, -1)),
    "to 超界等价于到末尾",
  );
} catch (e) {
  assert(false, `to 超界抛错：${(e as Error).message}`);
}

// ③ 起点大于终点：[5, 2) 应为空段，不得抛错
try {
  assert(splitFlowSegments(items, 5, 2, -1).length === 0, "起点大于终点得到空段");
} catch (e) {
  assert(false, `起点大于终点抛错：${(e as Error).message}`);
}

// ④ 两个越界同时出现
try {
  assert(splitFlowSegments(items, -3, 99, -1).length > 0, "负起点 + 超界终点仍能正常切段");
} catch (e) {
  assert(false, `负起点 + 超界终点抛错：${(e as Error).message}`);
}

// ⑤ 空 items：任何入参都不得抛错
try {
  assert(splitFlowSegments([], -1, 0, -1).length === 0, "空 items 不抛错");
  assert(splitFlowSegments([], 0, 10, -1).length === 0, "空 items + 超界终点不抛错");
} catch (e) {
  assert(false, `空 items 抛错：${(e as Error).message}`);
}

// ⑥ 合法区间的语义不变：工作流卡自成 sticky 段，其余归入过程段
const legal = splitFlowSegments(items, 0, items.length, -1);
assert(shape(legal) === "p[0] sticky@1 p[2]", `合法区间切段结构不变（实际 ${shape(legal)}）`);

// ⑦ skipIndex（turn 顶部首条用户消息）仍被正确跳过
const skipped = splitFlowSegments(items, 0, items.length, 0);
assert(shape(skipped) === "sticky@1 p[2]", `skipIndex 仍生效（实际 ${shape(skipped)}）`);

// ⑧ attached 段仍紧跟在被挂靠项之后
const attached = new Set<number>([0]);
const withAttached = splitFlowSegments(items, 0, items.length, -1, attached);
assert(
  shape(withAttached) === "p[0] attached@0 sticky@1 p[2]",
  `attached 段紧跟被挂靠项（实际 ${shape(withAttached)}）`,
);

console.log("");
if (failures > 0) {
  console.error(`切段边界：${failures} 项失败`);
  process.exit(1);
}
console.log("切段边界：全部通过");
