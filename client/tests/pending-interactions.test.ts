import assert from "node:assert/strict";
import { pendingActions, pendingQueueOf, firstVisiblePending, hasPending, usePendingInteractions } from "../src/store/pendingInteractions.ts";
import { useChatStore } from "../src/store/chat.ts";
import { api, type PendingApprovalItem } from "../src/api/client.ts";
import { wsClient, globalWsClient, type ServerEvent } from "../src/api/ws.ts";
import { waitingOf } from "../src/utils/sessionWaiting.ts";

// plan-20261103-interactions：运行真实仓库及同步分支，传输层用可控回调复现交错时序。
Object.defineProperty(globalThis, "requestAnimationFrame", { value: () => 1, configurable: true });
const question = (id: string, sid = 1): PendingApprovalItem => ({
  approval_id: id, session_id: sid, kind: "question", detail: {
    kind: "question", session_id: sid, questions: [{ question: "选择方案", options: ["A", "B"] }],
  },
  tool: "ask_user_question", action_label: null, agent_name: null, is_subagent: false,
});
pendingActions.reset();
const mixed = [
  { approvalId: "tool", detail: { session_id: 1, tool: "terminal_exec" } },
  { approvalId: "ask", detail: question("ask").detail },
];
assert.equal(firstVisiblePending(mixed, 1, "full")?.approvalId, "ask", "完全访问隐藏工具审批，仍可回答后面的需求提问");
assert.equal(firstVisiblePending(mixed.slice(0, 1), 1, "full"), null, "完全访问不显示工具审批窗");
assert.equal(firstVisiblePending(mixed, 1, "ask")?.approvalId, "tool", "询问审批模式保留真实工具审批");
assert.equal(firstVisiblePending(mixed, 2, "ask"), null, "非所属会话不能展示任何交互");
useChatStore.setState({ currentSessionId: 2, approvalQueue: [], pendingApproval: null });
pendingActions.noteRequest(1, "q1", "question", question("q1").detail);
assert.equal(useChatStore.getState().pendingApproval, null, "后台提问不得串进当前会话");
assert.equal(waitingOf({ id: 1, has_running: true }, hasPending), true);

let globalEvent: (event: ServerEvent) => void = () => {};
globalWsClient.connect = () => {};
globalWsClient.onOpen = () => () => {};
globalWsClient.on = (handler) => { globalEvent = handler; return () => {}; };
wsClient.connect = () => {};
wsClient.disconnect = () => {};
wsClient.send = () => {};
let connected = true;
Object.defineProperty(wsClient, "connected", { get: () => connected, configurable: true });
const store = useChatStore.getState();
useChatStore.setState({
  loadRevertLock: async () => {}, loadSuggestion: async () => {},
  refreshTurns: async () => {}, refreshTasks: async () => {},
  loadSessionSubagents: async () => {}, _drainQueue: async () => {},
});
// 首次加载与缓存恢复都经过 switchSession；隔离与交互无关的 API。
api.messagePage = async () => ({
  items: [], context: [], before_id: null, after_id: null, has_before: false,
  has_after: false, revision: 0, boundary_turn_ids: [],
});
api.listTurns = async () => [];
api.listSessionTasks = async () => [];
api.getSessionUsage = async () => { throw new Error("测试不请求占用"); };
await store.switchSession(1);
assert.equal(useChatStore.getState().sessionLoadError, false, "真实首次加载正常完成");
assert.equal(useChatStore.getState().pendingApproval?.detail.kind, "question", "切回立即显示完整提问");
store.connectGlobalEvents();
globalEvent({ event: "approval.request", payload: { ...question("q2") } });
assert.equal(useChatStore.getState().approvalQueue.length, 2, "全局事件即时投影当前输入区");
connected = false;
store.respondApproval("q1", true);
assert.equal(useChatStore.getState().pendingApproval?.approvalId, "q1", "连接未就绪时保留提问与答案重试入口");
assert.equal(hasPending(1), true);
connected = true;
store.respondApproval("q1", true, false, { answer: "A" });
assert.equal(useChatStore.getState().pendingApproval?.approvalId, "q2");
store.respondApproval("q2", true);
assert.equal(waitingOf({ id: 1, has_running: true }, hasPending), false, "回答后运行未结束也立即摘掉问号");
pendingActions.noteRequest(1, "q2", "question", question("q2").detail);
assert.equal(hasPending(1), false, "另一通道迟到请求不得复活已答问题");

const before = pendingActions.snapshotVersion();
pendingActions.noteRequest(1, "new", "question", question("new").detail);
pendingActions.reconcile([question("q1"), question("q2")], before);
assert.deepEqual(pendingQueueOf(1).map((it) => it.approvalId), ["new"], "旧清单既不能复活已答项，也不能丢掉新提问");
const beforeAnswer = pendingActions.snapshotVersion();
store.respondApproval("new", true);
pendingActions.reconcile([question("new")], beforeAnswer, 1);
assert.equal(hasPending(1), false, "请求期间回答，迟到清单不得恢复问号");

pendingActions.noteRequest(1, "stale", "approval", { tool: "terminal_exec" });
pendingActions.reconcile([], pendingActions.snapshotVersion(), 1);
assert.equal(useChatStore.getState().pendingApproval, null, "校对清单移除失效审批卡");
pendingActions.noteRequest(1, "details", "approval", {});
pendingActions.reconcile([question("details")], pendingActions.snapshotVersion(), 1);
assert.equal(usePendingInteractions.getState().pendingBySession[1].details.kind, "question");
assert.equal(useChatStore.getState().pendingApproval?.detail.kind, "question", "已有条目必须补全类型与选项");

await store.switchSession(2);
globalEvent({ event: "approval.response", payload: { session_id: 1, approval_id: "details" } });
const rendered: Array<string | null> = [];
const unsub = useChatStore.subscribe((s) => {
  if (s.currentSessionId === 1) rendered.push(s.pendingApproval?.approvalId ?? null);
});
await store.switchSession(1);
unsub();
assert.ok(rendered.length > 0 && rendered.every((id) => id === null), "切回每个中间状态都不能出现缓存旧卡");
store.handleWs("approval.request", { approval_id: "history", detail: { session_id: 1 } }, true);
assert.equal(useChatStore.getState().pendingApproval, null, "历史审批重放不闪卡");

let complete: (value: Awaited<ReturnType<typeof api.getPendingApprovals>>) => void = () => {};
api.getPendingApprovals = () => new Promise((resolve) => { complete = resolve; });
store.handleWs("sync.response", { count: 0 });
const oldComplete = complete;
await store.switchSession(2);
await store.switchSession(1);
oldComplete({ items: [question("old-navigation")], total: 1 });
await Promise.resolve();
assert.equal(hasPending(1), false, "A→B→A 后旧导航请求不得写回同编号会话");
store.handleWs("sync.response", { count: 0 });
complete({ items: [question("restored")], total: 1 });
await Promise.resolve();
assert.equal(useChatStore.getState().pendingApproval?.approvalId, "restored", "补发后的未决清单可恢复漏掉的真实提问");
store.handleWs("sync.response", { count: 0 });
store.respondApproval("restored", true);
complete({ items: [question("restored")], total: 1 });
await Promise.resolve();
assert.equal(useChatStore.getState().pendingApproval, null, "真实同步分支的迟到清单不能恢复已答卡");
store.disconnectGlobalEvents();
console.log("通过：多会话提问即时恢复、双通道交错、清单竞态、类型补齐、回答摘标与切换无旧卡");
