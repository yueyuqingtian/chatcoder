/**
 * 消息流展开态移交测试（纯逻辑，Node 原生 TS 运行：`node tests/flow-expand.test.ts`，client 目录）。
 *
 * 固化这些规则：
 *  - 展开的流式思考落库后，落库后的思考块保持展开，流式键随之清空；
 *  - 收起的流式思考落库后，思考块保持默认收起；
 *  - 没有流式状态时落库不产生展开态；同一段思考重复移交结果不变；
 *  - 中断（未落库即卸载）后，流式键被丢弃，下一段从收起开始；
 *  - 子代理线程的流式键与主会话互不影响；
 *  - 移交只动流式键与对应思考块，不碰工具行与聚合行的展开态。
 */
import {
  dropFlowExpand,
  handOffThinkingExpand,
  liveThinkingKey,
  readFlowExpand,
  thinkingKey,
  writeFlowExpand,
} from "../src/store/flowExpand.ts";

function assert(cond: boolean, msg: string) {
  if (!cond) {
    console.error(`FAIL: ${msg}`);
    process.exitCode = 1;
  } else {
    console.log(`ok: ${msg}`);
  }
}

// 展开的流式思考落库：思考块保持展开，流式键清空
writeFlowExpand(liveThinkingKey(), true);
handOffThinkingExpand(101);
assert(readFlowExpand(thinkingKey(101)) === true, "展开的流式思考落库后，思考块保持展开");
assert(readFlowExpand(liveThinkingKey()) === undefined, "移交后流式键被清空");

// 收起的流式思考落库：思考块保持默认收起（未记录）
writeFlowExpand(liveThinkingKey(), false);
handOffThinkingExpand(102);
assert(readFlowExpand(thinkingKey(102)) === undefined, "收起的流式思考落库后，思考块保持默认收起");
assert(readFlowExpand(liveThinkingKey()) === undefined, "收起移交后流式键同样被清空");

// 没有流式状态时落库：不产生展开态
handOffThinkingExpand(103);
assert(readFlowExpand(thinkingKey(103)) === undefined, "没有流式状态时落库不产生展开态");

// 同一段思考重复移交：已移交的结果不被改写
handOffThinkingExpand(101);
assert(readFlowExpand(thinkingKey(101)) === true, "同一段思考重复移交，结果不变");

// 中断：未落库即卸载，流式键被丢弃，下一段从收起开始
writeFlowExpand(liveThinkingKey(), true);
dropFlowExpand(liveThinkingKey());
handOffThinkingExpand(104);
assert(readFlowExpand(thinkingKey(104)) === undefined, "中断后的下一段不沿用展开态");

// 子代理线程：流式键按线程区分，移交只作用于本线程
writeFlowExpand(liveThinkingKey(7), true);
writeFlowExpand(liveThinkingKey(), false);
handOffThinkingExpand(105, 7);
assert(readFlowExpand(thinkingKey(105)) === true, "子代理线程展开的思考落库后保持展开");
assert(readFlowExpand(liveThinkingKey(7)) === undefined, "子代理移交后其流式键被清空");
assert(readFlowExpand(liveThinkingKey()) === false, "子代理移交不改动主会话的流式键");

// 移交不碰工具行与聚合行的展开态
writeFlowExpand("K1", true);
writeFlowExpand("group:K1", true);
writeFlowExpand(liveThinkingKey(), true);
handOffThinkingExpand(106);
assert(readFlowExpand(thinkingKey(106)) === true, "主会话展开的思考落库后保持展开");
assert(readFlowExpand("K1") === true && readFlowExpand("group:K1") === true, "移交不改动工具行与聚合行的展开态");
