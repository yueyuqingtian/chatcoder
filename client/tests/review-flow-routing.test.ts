/** plan-20261116-review-fixes：执行真实寻址和视图门控，依赖替身只隔离 IPC、Store 与 React 调度。 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import ts from "typescript";

function parsed(file: string) {
  const source = readFileSync(new URL(file, import.meta.url), "utf8");
  return ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
}
function declaration(file: ts.SourceFile, name: string): ts.Node {
  let result: ts.Node | undefined;
  function visit(node: ts.Node) {
    if ((ts.isVariableDeclaration(node) || ts.isFunctionDeclaration(node)) && node.name?.getText(file) === name) result = node;
    ts.forEachChild(node, visit);
  }
  visit(file);
  assert.ok(result, `找不到真实声明 ${name}`);
  return result;
}
function run(source: string, deps: Record<string, unknown>) {
  const compiled = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText;
  return new Function(...Object.keys(deps), compiled)(...Object.values(deps));
}

const host = parsed("../src/browserAgent/host.ts");
const functions = ["microappReadyProbe", "microappCallOp"].map((name) => declaration(host, name).getText(host)).join("\n");
const buckets: Record<string, { tabs: Array<{ id: string; uid: string; meta: { appId: string; version: string } }> }> = {
  "1": { tabs: [{ id: "microapp", uid: "session-a", meta: { appId: "demo-app", version: "1.0.0" } }] },
  "2": { tabs: [{ id: "microapp", uid: "session-b", meta: { appId: "demo-app", version: "1.0.0" } }] },
};
const calls: Array<{ op: string; uid: string }> = [];
const invoke = run(`${functions}\nreturn microappCallOp;`, {
  usePanelStore: { getState: () => ({ buckets, ensureMicroAppTab: () => {} }) },
  numericSid: Number, bucketKeyOf: String, panelTabKey: (tab: { uid: string }) => tab.uid,
  microappHost: { instanceFor: () => undefined },
  mainExec: async (_: unknown, op: string, payload: { uid: string }) => { calls.push({ op, uid: payload.uid }); return { uid: payload.uid }; },
  errText: String, sleep: async () => {}, MICROAPP_READY_BUDGET_MS: 5000, MICROAPP_READY_MAX_TRIES: 1,
  MICROAPP_RETRYABLE: /^microapp-(guest-missing|guest-loading|bridge-missing)/, MICROAPP_FATAL_ARG: /非法/,
  MICROAPP_ERR_LOADING: "microapp-guest-loading", MICROAPP_READY_INTERVAL_MS: 120,
}) as (sid: string, args: Record<string, unknown>) => Promise<{ uid: string }>;
assert.equal((await invoke("1", { appId: "demo-app", version: "1.0.0" })).uid, "session-a");
assert.deepEqual(calls.map((call) => call.uid), ["session-a", "session-a"]);
assert.equal((await invoke("2", { appId: "demo-app", version: "1.0.0" })).uid, "session-b");
const before = calls.length;
await assert.rejects(invoke("3", { appId: "demo-app", version: "1.0.0", autoOpen: false }), /当前会话没有/);
assert.equal(calls.length, before, "没有本会话实例时不得借用其它会话");

const flow = parsed("../src/components/chat/MessageFlow.tsx");
const findLoaded = declaration(flow, "indexOfRemoteMatch") as ts.VariableDeclaration;
const entries = [{ kind: "turn", turnId: 10, items: [{ kind: "text", msg: { id: 90 } }] }];
function searchIndex(ids: number[], id: number) {
  const locate = run(`return (${findLoaded.initializer!.getText(flow)});`, {
    entries, loadedMessageIds: new Set(ids), useCallback: (fn: unknown) => fn,
  }) as (match: { id: number; turn_id: number }) => number;
  return locate({ id, turn_id: 10 });
}
assert.equal(searchIndex([90], 10), -1, "仅加载同轮尾部时必须补拉早期命中");
assert.equal(searchIndex([10, 90], 10), 0, "命中已补拉后才能直接定位同轮");
assert.equal(searchIndex([90], 90), 0, "已加载消息不必重复请求定位页");

const turn = parsed("../src/components/chat/TurnGroup.tsx");
const running = declaration(turn, "turnRunning") as ts.VariableDeclaration;
const active = declaration(turn, "roundActive") as ts.VariableDeclaration;
function roundActive(mode: string, status: string, isRunning = true, awaitingBackground = false) {
  return run(`const turnRunning = ${running.initializer!.getText(turn)}; return ${active.initializer!.getText(turn)};`, {
    entry: { turnId: 10 }, flow: mode, isRunning, awaitingBackground, turnRowStatus: status,
    TERMINAL_TURN_STATUSES: new Set(["completed", "failed", "interrupted", "rolled_back", "cancelled"]),
  });
}
assert.equal(roundActive("subagent", "completed"), true, "主轮次完成不能关闭仍运行子代理的实时过程");
assert.equal(roundActive("subagent", "completed", false), false);
assert.equal(roundActive("main", "completed"), false, "主会话仍应过滤陈旧运行标记");
assert.equal(roundActive("main", "completed", false, true), true);
console.log("通过：微应用跨会话 UID 隔离、搜索精确装载判定、子代理独立运行态");
