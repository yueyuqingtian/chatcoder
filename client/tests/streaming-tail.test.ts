/** plan-20261112-loading-home-sidebar：覆盖工具调用前后正文落库与流式缓冲交接，等待标记由 active 独立控制。 */
import assert from "node:assert/strict";
import { visibleStreamingText } from "../src/components/chat/streamingTailState";

assert.equal(visibleStreamingText("已派发子代理", "已派发子代理"), "", "派发后正文落库时只清空显示正文");
assert.equal(visibleStreamingText("已收集结果\n", " 已收集结果 "), "", "收集结果后的空白差异不能造成重复正文");
assert.equal(visibleStreamingText("继续整理结果", "已收集结果"), "继续整理结果", "新的流式正文继续展示");
assert.equal(visibleStreamingText("已收集结果，继续整理", "已收集结果"), "已收集结果，继续整理", "部分重合不能丢掉后续内容");
assert.equal(visibleStreamingText("正在处理"), "正在处理", "没有已保存正文时保留流式内容");
assert.equal(visibleStreamingText("", "已收集结果"), "", "工具执行间隙允许空正文交给等待状态行");
console.log("流式尾部正文交接回归通过");
