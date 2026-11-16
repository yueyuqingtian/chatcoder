/** plan-20261116-subagent-collapse：真实轮次组件必须收起子代理，展开后保留派发顺序。 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { createElement, type ComponentProps, type ReactNode } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";
import { MsgType, type MessageOut } from "@chatcoder/shared";
import * as timeline from "../src/components/chat/timeline";
import * as toolLabels from "../src/components/chat/toolLabels";
import * as time from "../src/utils/time";
import type { TurnGroup as TurnGroupComponent } from "../src/components/chat/TurnGroup";

type Props = ComponentProps<typeof TurnGroupComponent>;
const require = createRequire(import.meta.url);
const empty = () => null;
const childrenView = ({ children }: { children: ReactNode }) => children;
let expanded = false;
const state = { requestRollbackPreview: empty, turns: [], subagentMeta: {}, thinkingBuffers: {}, turnChanges: {} };
// plan-20261116-subagent-collapse：隔离插件内容，保留真实轮次分段、折叠容器及 React hooks，验证用户实际看到的顺序。
const dependencies: Record<string, unknown> = {
  "./timeline": timeline,
  "./toolLabels": toolLabels,
  "../../utils/time": time,
  "../../store/chat": { useChatStore: (selector: (s: typeof state) => unknown) => selector(state) },
  "../../store/flowExpand": { dropFlowExpand: empty, turnCollapseKey: String, subagentCollapseKey: String,
    useTurnCollapse: (_key: string, collapsed: boolean) => [expanded ? false : collapsed, empty] },
  "./useRunningTicker": { useRunningTicker: empty },
  "../ui": { Tooltip: childrenView },
  "../ui/usePresence": { motionDisabled: () => false },
  "../ui/ViewportBlock": { ViewportBlock: childrenView },
  "../icons": { IconRotateCcw: empty, IconArrowToggle: empty },
  "../../perf/metrics": { recordComponentRender: empty },
  "../MarkdownContent": { MarkdownContent: childrenView },
  "./MessageActions": { MessageActions: ({ scope }: { scope: string }) => createElement("span", null, `${scope}操作`) },
  "./AttachmentCard": { MessageImageGrid: empty, attachmentsOf: () => [] },
  "./UserBubble": { UserBubble: empty },
  "./TurnChangesCard": { TurnChangesCard: empty },
  "../workflow/WorkflowCard": { WorkflowCard: () => createElement("span", null, "工作流卡") },
  "../workflow/WorkflowNotice": { WorkflowNotice: empty },
  "./ToolShotCard": { ToolShotCard: empty },
  "./ErrorCard": { DismissibleErrorCard: empty },
  "./ProcessGroup": { ProcessGroup: ({ indices, renderItem }: { indices: number[]; renderItem: (i: number) => ReactNode }) => indices.map((i) => renderItem(i)) },
  "../../plugins/registry": { PluginSlot: ({ slot, meta, nodes }: { slot: string; meta?: { name: string }; nodes?: unknown }) =>
    createElement("span", null, slot === "subagent-card" ? `子代理：${meta?.name}` : slot === "tool-tree" ? JSON.stringify(nodes) : slot) },
};
const source = readFileSync(new URL("../src/components/chat/TurnGroup.tsx", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, { fileName: "TurnGroup.tsx",
  compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022 } }).outputText;
const componentModule = { exports: {} as { TurnGroup: typeof TurnGroupComponent } };
new Function("require", "module", "exports", compiled)((id: string) => {
  if (Object.hasOwn(dependencies, id)) return dependencies[id];
  if (id === "react" || id === "react/jsx-runtime") return require(id);
  throw new Error(`未隔离的组件依赖：${id}`);
}, componentModule, componentModule.exports);

const msg = (id: number, type: string, content: Record<string, unknown>): MessageOut => ({
  id, session_id: 1, turn_id: 10, thread_id: null, sender_type: "agent", sender_id: 1,
  msg_type: type, content, created_at: null,
});
const before = msg(1, MsgType.ToolCall, { tool: "fs_read", call_key: "before", args: { path: "before.ts" } });
const after = msg(5, MsgType.ToolCall, { tool: "fs_read", call_key: "after", args: { path: "after.ts" } });
const final = msg(6, MsgType.Text, { text: "最终汇报" });
function render(messages: MessageOut[], overrides: Partial<Props> = {}): string {
  const entry = timeline.buildTimeline(messages)[0];
  assert.equal(entry.kind, "turn");
  return renderToStaticMarkup(createElement(componentModule.exports.TurnGroup, {
    entry, isRunning: false, globalRunning: false, latestUserId: 0, flowErrorTurnId: null,
    hiddenTurnErrors: [], turnStatus: "completed", ...overrides,
  }));
}
for (const tool of ["spawn_subagent", "SubAgentAsync", "resume_subagent"]) {
  const dispatch = msg(2, MsgType.ToolCall, { tool, call_key: "dispatch", args: { task_title: "时序核查", agent_id: 42 } });
  for (const ok of [true, false]) {
    const result = msg(3, MsgType.ToolResult, { tool, call_key: "dispatch", ok,
      output: ok ? "Subagent #42 dispatched in the background" : "Error: 派发失败" });
    const messages = [before, dispatch, result, after, final];
    expanded = false;
    const collapsed = render(messages);
    assert.doesNotMatch(collapsed, /子代理：|before\.ts|after\.ts/, `${tool} ${ok}：已完成调用全部随过程收起`);
    assert.match(collapsed, /最终汇报/, "收起后最终汇报仍然可见");
    assert.match(collapsed, /aria-expanded="false"/, "保留展开入口");
    expanded = true;
    const open = render(messages);
    assert.ok(open.indexOf("before.ts") < open.indexOf("子代理：") && open.indexOf("子代理：") < open.indexOf("after.ts")
      && open.indexOf("after.ts") < open.indexOf("最终汇报"), "展开后保持前工具、子代理、后工具、汇报的时序");
    expanded = false;
    for (const overrides of [
      { isRunning: true, globalRunning: true, turnStatus: "running" },
      { awaitingBackground: true },
      { turnStatus: "failed" },
      { turnStatus: "interrupted" },
    ]) assert.match(render(messages, overrides), /子代理：/, "运行、等待后台、异常中断均保留过程卡");
    for (const partial of [[dispatch, result], [dispatch, result, final]]) {
      const html = render(partial);
      assert.doesNotMatch(html, /子代理：/, "只有子代理调用的轮次也默认收起");
      assert.match(html, /aria-expanded="false"/, "纯子代理轮保留展开入口");
      assert.match(html, /ai操作/, "完成后的操作行留在折叠区外");
    }
    const withWorkflow: Props["entry"] = { kind: "turn", turnId: 10, items: [
      { kind: "subagent", msg: dispatch, result }, { kind: "workflow", programId: 9, turnId: 10, firstId: 4 }, { kind: "text", msg: final },
    ] };
    const workflowHtml = render(messages, { entry: withWorkflow });
    assert.match(workflowHtml, /工作流卡/, "工作流常显入口保持可见");
    assert.doesNotMatch(workflowHtml, /子代理：/, "相邻工作流不能让子代理逃出折叠");
  }
}
console.log("通过：子代理派发/别名/续跑与失败调用随过程折叠，展开时序、纯子代理轮、运行与异常状态、工作流常显均正常");
