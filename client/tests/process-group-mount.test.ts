/** plan-20261114-message-flow：首次渲染必须直接区分历史调用与真实 pending，不能等 effect 清理后才收口。 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { createElement, type ComponentProps } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";
import { MsgType, type MessageOut } from "@chatcoder/shared";
import * as flowExpand from "../src/store/flowExpand";
import * as timeline from "../src/components/chat/timeline";
import * as groupState from "../src/components/chat/processGroupState";
import * as runningTicker from "../src/components/chat/useRunningTicker";
import type { ProcessGroup as ProcessGroupComponent } from "../src/components/chat/ProcessGroup";

type Props = ComponentProps<typeof ProcessGroupComponent>;
const require = createRequire(import.meta.url);
let results: Readonly<Record<string, unknown>> = {};
const emptyView = () => null;
// plan-20261114-message-flow：Node 不原生解析 TSX，转译实际组件并隔离重型视图，保留真实 React hooks 和分组计算。
const dependencies: Record<string, unknown> = {
  "../../store/chat": { useChatStore: (selector: (state: unknown) => unknown) => selector({ runningToolResults: results, subagentThinking: {} }) },
  "../../store/streamMerged": { useMergedThinkingText: () => "" },
  "../../store/flowExpand": flowExpand,
  "../icons": { IconChevronRight: emptyView },
  "../iconkit/ToolGlyphs": { ToolGlyph: emptyView },
  "./ChatCollapse": { ChatCollapse: emptyView },
  "./ThinkingBlock": { ThinkingBlock: emptyView },
  "./ThinkingTicker": { ThinkingTicker: emptyView },
  "./ThinkingExpanded": { ThinkingExpanded: emptyView },
  "./timeline": timeline,
  "./processGroupState": groupState,
  "./useRunningTicker": runningTicker,
};
const source = readFileSync(new URL("../src/components/chat/ProcessGroup.tsx", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, {
  fileName: "ProcessGroup.tsx",
  compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022 },
}).outputText;
const componentModule = { exports: {} as { ProcessGroup: typeof ProcessGroupComponent } };
new Function("require", "module", "exports", compiled)(
  (id: string) => {
    if (Object.hasOwn(dependencies, id)) return dependencies[id];
    if (id === "react" || id === "react/jsx-runtime") return require(id);
    throw new Error(`未隔离的组件依赖：${id}`);
  },
  componentModule,
  componentModule.exports,
);
const { ProcessGroup } = componentModule.exports;

function message(id: number, type: string, content: Record<string, unknown>): MessageOut {
  return { id, session_id: 1, turn_id: 10, thread_id: 4, sender_type: "agent", sender_id: 4, msg_type: type, content, created_at: null };
}
const read = message(1, MsgType.ToolCall, { tool: "fs_read", args: { path: "src/a.ts" }, call_key: "read" });
const readDone = message(2, MsgType.ToolResult, { tool: "fs_read", call_key: "read", ok: true });
const run = message(3, MsgType.ToolCall, { tool: "terminal_exec", args: { command: "npm run build" }, call_key: "run" });
const runDone = message(4, MsgType.ToolResult, { tool: "terminal_exec", call_key: "run", ok: true });
const runFailed = message(4, MsgType.ToolResult, { tool: "terminal_exec", call_key: "run", ok: false, error: "构建失败" });

function propsOf(messages: MessageOut[], flow: Props["flow"]): Props {
  const entry = timeline.buildTimeline(messages)[0];
  assert.equal(entry.kind, "turn");
  return { items: entry.items, indices: entry.items.map((_, index) => index), turnId: 10, agentId: 4, flow, active: true, renderItem: emptyView };
}
function render(props: Props, runningResults: Readonly<Record<string, unknown>> = {}): string {
  results = runningResults;
  return renderToStaticMarkup(createElement(ProcessGroup, props));
}
function assertSettled(html: string, label: string): void {
  assert.match(html, /tc-process-head/, `${label}：应渲染真实分组头`);
  assert.match(html, /aria-expanded="false"/, `${label}：测试覆盖收起态`);
  assert.doesNotMatch(html, /tc-process-preview|text-shine|data-running/, `${label}：已完成组初次渲染不能重播预览或扫光`);
  assert.match(html, /2 次|1 次/, `${label}：应保留静态工具摘要`);
}

for (const flow of ["main", "subagent"] as const) {
  for (const result of [runDone, runFailed]) {
    const props = propsOf([read, readDone, run, result], flow);
    const label = `${flow} ${result.content.ok ? "成功" : "失败"}历史组`;
    const initial = render(props);
    assertSettled(initial, label);
    assert.equal(render(props), initial, `${label}：同一组卸载后重新挂载应保持静态摘要`);
  }

  const pending = propsOf([read, readDone, run], flow);
  const pendingHtml = render(pending);
  assert.match(pendingHtml, /class="tc-process-preview text-shine"[^>]*>正在运行 npm run build</, `${flow}：历史读取完成后，真实 pending 仍应预览并扫光`);
  assert.doesNotMatch(pendingHtml, /正在读取/, `${flow}：已完成读取不能抢占预览`);

  // plan-20261114-message-flow：实时结果可能先于落库到达，挂载时应直接读 results 收口。
  for (const ok of [true, false]) {
    assertSettled(render(pending, { run: { ok } }), `${flow} 实时${ok ? "成功" : "失败"}结果已到但尚未落库`);
  }
  assertSettled(render({ ...pending, active: false }), `${flow} 轮次结束后遗留 pending`);

  const earlierPending = render(propsOf([read, run, runDone], flow));
  assert.match(earlierPending, /class="tc-process-preview text-shine"[^>]*>正在读取 a.ts</, `${flow}：较早 pending 不能被最后一个已完成调用遮住`);
}
console.log("通过：主会话与子代理历史成功/失败组首次渲染和重新挂载不重播、真实 pending 保留、实时结果先到即收口");
