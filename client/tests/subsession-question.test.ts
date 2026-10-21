/**
 * 子会话提问的展示口径回归测试（plan-20261021-subq-row）。
 *
 * 纯逻辑测试（不依赖 DOM / store），验证：
 *   ① 提问文本解析：来源标题、问题正文，截掉尾部「答复方式」说明，解析不出时原样保留；
 *   ② 提问不再是常显卡片，而是一条普通的工具条目（伪工具名），并随「工作过程」一起折叠；
 *   ③ 回归：提问 / 后台通知落在长命令的「调用」与「结果」之间时，命令不能被定格成「运行中」——
 *      此前遇到系统消息就提前 flush，结果之后才到达，任务结束后那一行仍然转圈；
 *   ④ 真正没有结果的调用仍然是运行中（修复不能把它也变成已完成）。
 *
 * 运行方式（client 目录，无测试框架，用 esbuild 打包后由 node 直接执行）：
 *   npx esbuild tests/subsession-question.test.ts --bundle --format=esm --platform=node \
 *     --alias:@chatcoder/shared=../packages/shared/src/index.ts --outfile=<临时目录>/subsession-question.test.mjs
 *   node <临时目录>/subsession-question.test.mjs
 */
import assert from "node:assert/strict";
import { buildTimeline, createTimelineBuilder, isOutsideProcess, splitFlowSegments } from "../src/components/chat/timeline";
import type { TimelineEntry, ToolLeaf, TurnItem } from "../src/components/chat/timeline";
import {
  SUBSESSION_QUESTION_TOOL,
  parseSubsessionQuestion,
  splitInlineMarks,
} from "../src/utils/subsessionQuestion";
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

/** 一轮里全部工具叶子（含聚合行里的）。 */
function allLeaves(entries: TimelineEntry[]): ToolLeaf[] {
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

const itemsOf = (entries: TimelineEntry[]): TurnItem[] =>
  entries.flatMap((e) => (e.kind === "turn" ? e.items : []));

const QUESTION_TEXT =
  "[子会话汇报]「A1 价下发到收费查药接口」提问（question_id=q_499_365da561）："
  + "执行「A1 价下发到收费查药接口」时遇到一个写入范围冲突，需要确认后再继续：\n\n"
  + "可选方案:\nA（推荐）：允许改 `StorePricePo.java`\nB：严格不动实体\n"
  + "答复方式：send_to_subsession(499, message, question_id=\"q_499_365da561\")。自己答得了就直接答。";

const question = (id: number, extra: Record<string, unknown> = {}): MessageOut =>
  msg(id, 7, MsgType.Text, {
    text: QUESTION_TEXT,
    subsession_report: true,
    subsession_id: 499,
    question_id: "q_499_365da561",
    mailbox: { kind: "subsession_question", priority: 0, source_session_id: 499, question_id: "q_499_365da561" },
    ...extra,
  }, SenderType.System);

const user = msg(1, 7, MsgType.Text, { text: "按照文档完成开发" }, SenderType.User);
const call = (id: number, key: string, tool = "terminal_exec", args: Record<string, unknown> = { command: "$files = @(" }) =>
  msg(id, 7, MsgType.ToolCall, { tool, args, call_key: key });
const result = (id: number, key: string, tool: string, output: string) =>
  msg(id, 7, MsgType.ToolResult, { tool, call_key: key, ok: true, output });

// ── ① 文本解析 ─────────────────────────────────────────────────────────────

{
  const { title, body } = parseSubsessionQuestion(QUESTION_TEXT);
  assert.equal(title, "A1 价下发到收费查药接口", "来源标题取自头部标记");
  assert.ok(body.startsWith("执行「A1 价下发到收费查药接口」时遇到一个写入范围冲突"), "正文从头部标记之后开始");
  assert.ok(body.includes("可选方案:\nA（推荐）") && body.includes("B：严格不动实体"), "多段正文与选项换行原样保留");
  assert.ok(!body.includes("答复方式") && !body.includes("send_to_subsession"), "尾部工具调用说明被截掉");
  assert.ok(!body.includes("question_id"), "头部标记不进正文");
}

{
  // 没有标准头部（旧文案）：取第一个冒号之后；连冒号都没有就整段原样
  assert.deepEqual(parseSubsessionQuestion("子会话提问：要不要加索引？"), { title: "", body: "要不要加索引？" });
  assert.deepEqual(parseSubsessionQuestion("要不要加索引"), { title: "", body: "要不要加索引" });
  assert.deepEqual(parseSubsessionQuestion("   "), { title: "", body: "" });
  // 头部之后什么都没有：退回整段原文，不能展示成空
  const head = "[子会话汇报]「X」提问（question_id=q_1）：";
  assert.equal(parseSubsessionQuestion(head).body, head);
  // 标题里带全角括号 / 引号也不影响收尾锚点
  assert.equal(parseSubsessionQuestion("[子会话汇报]「改（旧）接口」提问（question_id=q_2）：问题").title, "改（旧）接口");
}

{
  const spans = splitInlineMarks("见 `a.java` 与 **重点** 结束");
  assert.deepEqual(spans.map((s) => s.kind), ["text", "code", "text", "bold", "text"]);
  assert.equal(spans[1].text, "a.java");
  assert.equal(spans[3].text, "重点");
  assert.deepEqual(splitInlineMarks("没有标记"), [{ kind: "text", text: "没有标记" }]);
  assert.deepEqual(splitInlineMarks("未闭合 `code"), [{ kind: "text", text: "未闭合 `code" }]);
}

// ── ② 提问是普通工具条目，随工作过程折叠 ───────────────────────────────────────

{
  const entries = buildTimeline([user, call(2, "k1"), question(3), result(4, "k1", "terminal_exec", "ok"), msg(5, 7, MsgType.Text, { text: "全部完成" })]);
  const items = itemsOf(entries);
  assert.ok(!items.some((it) => (it.kind as string) === "subsession-question"), "不再有独立的提问卡条目");

  const toolItems = items.filter((it) => it.kind === "tools");
  assert.equal(toolItems.length, 2, "命令与提问各自成条");
  const leaf = allLeaves(entries).find((l) => l.tool === SUBSESSION_QUESTION_TOOL)!;
  assert.ok(leaf, "提问以伪工具叶子呈现");
  assert.equal(leaf.ok, true, "提问是已发生的通知，不是运行中 / 失败");
  assert.equal(leaf.args.source_session_id, 499, "来源会话取自 mailbox.source_session_id");
  assert.equal(leaf.args.title, "A1 价下发到收费查药接口");
  assert.equal(leaf.args.question_id, "q_499_365da561");
  assert.ok(leaf.output.includes("写入范围冲突") && !leaf.output.includes("答复方式"), "展开区正文已截掉说明");

  // 折叠：提问不再是常显项，完成后与其它过程项在同一段，被工作过程容器收起
  const questionIdx = items.findIndex((it) => it.kind === "tools" && it.nodes.some((n) => n.kind === "leaf" && n.leaf.tool === SUBSESSION_QUESTION_TOOL));
  assert.ok(!isOutsideProcess(items[questionIdx]), "提问不是常显项");
  const finalIdx = items.findIndex((it) => it.kind === "text");
  const firstUser = items.findIndex((it) => it.kind === "user");
  const segments = splitFlowSegments(items, 0, finalIdx, firstUser, undefined);
  assert.ok(!segments.some((s) => s.kind === "sticky"), "过程段里没有常显段");
  const processIdx = segments.flatMap((s) => (s.kind === "process" ? s.indices : []));
  assert.ok(processIdx.includes(questionIdx), "提问在可折叠的过程段内");
}

{
  // 旧数据：没有 mailbox，只有 subsession_id + question_id，也要认出来
  const legacy = msg(3, 7, MsgType.Text, { text: QUESTION_TEXT, subsession_id: 499, question_id: "q_x" }, SenderType.System);
  const [leaf] = allLeaves(buildTimeline([user, legacy])).filter((l) => l.tool === SUBSESSION_QUESTION_TOOL);
  assert.ok(leaf, "旧消息兜底识别");
  assert.equal(leaf.args.source_session_id, 499, "来源会话兜底取 content.subsession_id");
}

// ── ③ 回归：长命令的调用与结果之间落了系统消息，命令不能停在「运行中」─────────────

{
  // 与现场一致：先读文件、再跑长命令，命令运行期间收到子会话提问并转后台，结果在提问之后才写入
  const msgs = [
    user,
    call(2, "k-read", "fs_read", { path: "a.java" }), result(3, "k-read", "fs_read", "class A {}"),
    call(4, "k-run"),
    question(5),
    result(6, "k-run", "terminal_exec", "命令已转后台继续运行（收到需要优先处理的消息，先处理消息）。\nshell_id: bg_1"),
  ];
  const leaves = allLeaves(buildTimeline(msgs)).filter((l) => l.tool !== SUBSESSION_QUESTION_TOOL);
  const run = leaves.find((l) => l.callKey === "k-run")!;
  assert.equal(run.ok, true, "结果在提问之后到达，命令仍应取到结果而不是运行中");
  assert.ok(run.output.includes("命令已转后台继续运行"), "结果内容完整带上");
  assert.equal(leaves.every((l) => l.ok !== null), true, "没有任何叶子被定格为运行中");

  // 增量构建（流式追加）与完整构建的结果一致：结果消息最后到达时，旧叶子要被更新
  const builder = createTimelineBuilder();
  builder(msgs.slice(0, 5));
  const streamed = allLeaves(builder(msgs)).find((l) => l.callKey === "k-run")!;
  assert.equal(streamed.ok, true, "结果追加到达后，增量构建同样把命令更新为已完成");
}

{
  // 后台命令通知夹在另一条命令的调用与结果之间，同样不能把它定格
  const msgs = [
    user,
    call(2, "k-long"),
    msg(3, 7, MsgType.Text, { text: "[摘要]", mailbox: { kind: "bg_command_done", priority: 0 }, bg_items: [{ shell_id: "bg_a", command: "make", returncode: 0, elapsed: 3, tail: "" }] }, SenderType.System),
    result(4, "k-long", "terminal_exec", "done"),
  ];
  const long = allLeaves(buildTimeline(msgs)).find((l) => l.callKey === "k-long")!;
  assert.equal(long.ok, true, "后台通知之后到达的结果同样被取到");
}

// ── ④ 真正没有结果的调用仍是运行中 ────────────────────────────────────────────

{
  const msgs = [user, call(2, "k-pending"), question(3)];
  const run = allLeaves(buildTimeline(msgs)).find((l) => l.callKey === "k-pending")!;
  assert.equal(run.ok, null, "确实还没有结果的命令继续显示运行中");
}

console.log("subsession-question.test: ok");
