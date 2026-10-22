/**
 * 子会话汇报 / 结论的展示口径与唤醒消息归轮回归测试（plan-20261022-release-ci）。
 *
 * 纯逻辑测试（不依赖 DOM / store），验证：
 *   ① 汇报 / 结论文本解析：来源标题、结束状态、正文；解析不出时原样保留，绝不丢内容；
 *   ② 主会话里的汇报 / 结论不再按普通正文平铺，而是一条单叶子的工具条目（伪工具名），失败 / 中断着失败色；
 *   ③ 子代理唤醒 / 邮箱唤醒的消息与工作流唤醒一样，按事件里的 turn_id 归入新一轮；不是孤儿唤醒消息时不动。
 *
 * 运行方式（client 目录，无测试框架，用 esbuild 打包后由 node 直接执行）：
 *   npx esbuild tests/subsession-notice.test.ts --bundle --format=esm --platform=node \
 *     --alias:@chatcoder/shared=../packages/shared/src/index.ts --outfile=<临时目录>/subsession-notice.test.mjs
 *   node <临时目录>/subsession-notice.test.mjs
 */
import assert from "node:assert/strict";
import { buildTimeline } from "../src/components/chat/timeline";
import type { TimelineEntry, ToolLeaf } from "../src/components/chat/timeline";
import {
  SUBSESSION_NOTICE_TOOLS,
  SUBSESSION_REPORT_TOOL,
  SUBSESSION_RESULT_TOOL,
  parseSubsessionNotice,
} from "../src/utils/subsessionQuestion";
import { adoptWakeupTurn } from "../src/utils/wakeupMessages";
import { MsgType, SenderType } from "@chatcoder/shared";
import type { MessageOut } from "../src/api/client";

function msg(
  id: number,
  turnId: number | null,
  msgType: string,
  content: Record<string, unknown>,
  sender: string = SenderType.Agent,
): MessageOut {
  return {
    id,
    session_id: 1,
    turn_id: turnId,
    thread_id: null,
    sender_type: sender,
    sender_id: null,
    msg_type: msgType,
    content,
    created_at: null,
  };
}

function leavesOf(entries: TimelineEntry[]): ToolLeaf[] {
  const out: ToolLeaf[] = [];
  for (const e of entries) {
    if (e.kind !== "turn") continue;
    for (const it of e.items) {
      if (it.kind !== "tools") continue;
      for (const n of it.nodes) {
        if (n.kind === "leaf") out.push(n.leaf);
        else out.push(...n.leaves);
      }
    }
  }
  return out;
}

const textItems = (entries: TimelineEntry[]) =>
  entries.flatMap((e) => (e.kind === "turn" ? e.items.filter((it) => it.kind === "text") : []));

// ── ① 解析 ────────────────────────────────────────────────────────────────

{
  const r = parseSubsessionNotice("[子会话汇报]「重构登录页」结束：turn 42 已完成。结论原文：\n## 结论\n- 改了 3 个文件");
  assert.equal(r.kind, "result");
  assert.equal(r.title, "重构登录页");
  assert.equal(r.status, "已完成");
  assert.equal(r.body, "## 结论\n- 改了 3 个文件");

  const p = parseSubsessionNotice("[子会话汇报]「补测试」汇报：已完成 2/5");
  assert.equal(p.kind, "report");
  assert.equal(p.title, "补测试");
  assert.equal(p.status, "");
  assert.equal(p.body, "已完成 2/5");

  const raw = parseSubsessionNotice("一段没有标准头部的旧文本");
  assert.equal(raw.body, "一段没有标准头部的旧文本", "解析不出时原样保留");
}

// ── ② 时间线：汇报 / 结论是工具条目，不再是正文 ───────────────────────────────

{
  const user = msg(1, 7, MsgType.Text, { text: "派三个子会话" }, SenderType.User);
  const report = msg(2, null, MsgType.Text, {
    text: "[子会话汇报]「A」汇报：做了一半",
    subsession_report: true, subsession_id: 31,
    mailbox: { kind: "subsession_report", priority: 2, source_session_id: 31 },
  }, SenderType.System);
  const result = msg(3, null, MsgType.Text, {
    text: "[子会话汇报]「A」结束：turn 9 已完成。结论原文：\n全部完成",
    subsession_report: true, subsession_id: 31,
    mailbox: { kind: "subsession_result", priority: 1, source_session_id: 31 },
  }, SenderType.System);
  // 更早的结论没有 mailbox 标记：靠文本里的「结束：」识别
  const legacyFail = msg(4, null, MsgType.Text, {
    text: "[子会话汇报]「B」结束：turn 10 执行失败。结论原文：\n报错了",
    subsession_report: true, subsession_id: 32,
  }, SenderType.System);

  const entries = buildTimeline([user, report, result, legacyFail]);
  const leaves = leavesOf(entries).filter((l) => SUBSESSION_NOTICE_TOOLS.has(l.tool));
  assert.deepEqual(leaves.map((l) => l.tool), [SUBSESSION_REPORT_TOOL, SUBSESSION_RESULT_TOOL, SUBSESSION_RESULT_TOOL]);
  assert.equal(textItems(entries).length, 0, "汇报 / 结论不再按普通正文平铺");

  const [rep, ok, fail] = leaves;
  assert.equal(rep.args.source_session_id, 31);
  assert.equal(rep.output, "做了一半");
  assert.equal(ok.args.status, "已完成");
  assert.equal(ok.output, "全部完成");
  assert.equal(ok.ok, true);
  assert.equal(fail.args.status, "执行失败");
  assert.equal(fail.ok, false, "失败的结论着失败色");
}

// ── ③ 唤醒消息归轮 ─────────────────────────────────────────────────────────

{
  const prev = msg(1, 5, MsgType.Text, { text: "上一轮" }, SenderType.User);
  const wake = msg(2, null, MsgType.Text, { text: "[系统提醒] …", subagent_wakeup: true, wakeup_reason: "subsession_result" }, SenderType.User);
  const adopted = adoptWakeupTurn([prev, wake], 6);
  assert.equal(adopted[1].turn_id, 6, "邮箱 / 子代理唤醒消息归入新一轮");
  assert.equal(adopted[0], prev, "其他消息引用不变");

  const plainUser = msg(3, null, MsgType.Text, { text: "用户刚发的" }, SenderType.User);
  const untouched = [prev, plainUser];
  assert.equal(adoptWakeupTurn(untouched, 6), untouched, "最新的用户消息不是唤醒消息时不回填");

  const wf = msg(4, null, MsgType.Text, { text: "工作流结束", workflow_wakeup: true }, SenderType.User);
  assert.equal(adoptWakeupTurn([prev, wf], 8)[1].turn_id, 8, "工作流唤醒照旧");
}

console.log("subsession-notice.test: ok");
