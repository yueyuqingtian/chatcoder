/**
 * 跨会话待处理索引 · 会话隔离测试。
 * 纯状态逻辑，Node 原生 TS 运行：`node tests/pending-interactions.test.ts`（client 目录）。
 *
 * 覆盖提问卡跨会话故障的两条约束：
 *  · 索引按会话分桶，同一 approvalId 重放只记一次（保留首次到达时间）；
 *  · 作答 / 回执按 approvalId 全局清理，且只摘掉目标条目，绝不误伤同会话的其他等待项。
 */
import {
  pendingActions,
  usePendingInteractions,
  hasPending,
  pendingCountOf,
} from "../src/store/pendingInteractions.ts";

let failures = 0;
function assert(cond: boolean, msg: string) {
  if (!cond) { failures++; console.error(`FAIL: ${msg}`); }
  else { console.log(`ok: ${msg}`); }
}
function eq(actual: unknown, expected: unknown, msg: string) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a !== e) { failures++; console.error(`FAIL: ${msg}\n   actual:   ${a}\n   expected: ${e}`); }
  else { console.log(`ok: ${msg}`); }
}

const countOf = () => Object.keys(usePendingInteractions.getState().pendingBySession).length;

// ① 两个会话各自入索引，互不影响
{
  pendingActions.reset();
  pendingActions.noteRequest(11, "apr_a", "question", { kind: "question", session_id: 11 });
  pendingActions.noteRequest(22, "apr_b", "approval", { kind: "tool_call", session_id: 22 });
  eq([hasPending(11), hasPending(22), hasPending(33)], [true, true, false], "索引按会话分桶，33 无等待");
  eq(countOf(), 2, "两个会话各占一个桶");
}

// ② 同一 approvalId 重放不重复计数，且保留首次到达时间
{
  pendingActions.reset();
  pendingActions.noteRequest(11, "apr_a", "question", { kind: "question", session_id: 11 });
  const first = usePendingInteractions.getState().pendingBySession[11]["apr_a"].createdAt;
  pendingActions.noteRequest(11, "apr_a", "question", { kind: "question", session_id: 11 });
  const after = usePendingInteractions.getState().pendingBySession[11]["apr_a"];
  eq(pendingCountOf(11), 1, "重放只保留一条");
  eq(after.createdAt, first, "重放保留首次到达时间");
  eq(after.detail.kind, "question", "重放补全的 detail 仍在");
}

// ③ 按 approvalId 跨桶清理：只摘目标条目，同会话其余条目保留
{
  pendingActions.reset();
  pendingActions.noteRequest(11, "apr_a1", "question", { kind: "question", session_id: 11 });
  pendingActions.noteRequest(11, "apr_a2", "question", { kind: "question", session_id: 11 });
  pendingActions.noteRequest(22, "apr_b", "approval", { kind: "tool_call", session_id: 22 });
  pendingActions.clearByApprovalId("apr_a1");
  eq(hasPending(11), true, "同会话仍有剩余等待项");
  eq(pendingCountOf(11), 1, "只摘掉了 apr_a1");
  eq(
    Boolean(usePendingInteractions.getState().pendingBySession[11]["apr_a2"]),
    true,
    "apr_a2 未被误清",
  );
  eq(hasPending(22), true, "别的会话不受影响");
}

// ④ 清空最后一个条目时整桶删除，避免空对象堆积
{
  pendingActions.reset();
  pendingActions.noteRequest(11, "apr_only", "question", {});
  pendingActions.clearByApprovalId("apr_only");
  eq(hasPending(11), false, "条目清空后无等待");
  eq(countOf(), 0, "空桶被一并删除");
}

// ⑤ 空 approvalId 不清任何东西（回执缺字段时不能误清整个索引）
{
  pendingActions.reset();
  pendingActions.noteRequest(11, "apr_keep", "question", {});
  pendingActions.clearByApprovalId("");
  eq(pendingCountOf(11), 1, "空 id 不清条目");
}

pendingActions.reset();
console.log("");
if (failures > 0) {
  console.error(`失败 ${failures} 项`);
  process.exit(1);
}
console.log("全部通过");
