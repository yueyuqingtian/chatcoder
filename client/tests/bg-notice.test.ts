/**
 * 后台命令通知的展示口径回归测试（plan-20261020-bgtask）。
 *
 * 纯逻辑测试（不依赖 DOM / store），验证：
 *   ① 后台命令完成 / 停止的系统消息不再是独立卡片，而是并入工具行（tools 条目，伪工具名区分动词）；
 *   ② 结构化条目 bg_items 渲染成一条一行：非 0 退出码标失败（红叉），被用户停止不算失败；
 *   ③ 旧消息没有 bg_items 时，整段文本放进展开区，不丢内容；
 *   ④ 展示用纯函数：运行时间写法、命令取首行。
 *
 * 运行方式（client 目录，无测试框架，用 esbuild 打包后由 node 直接执行）：
 *   npx esbuild tests/bg-notice.test.ts --bundle --format=esm --platform=node \
 *     --alias:@chatcoder/shared=../packages/shared/src/index.ts --outfile=<临时目录>/bg-notice.test.mjs
 *   node <临时目录>/bg-notice.test.mjs
 */
import assert from "node:assert/strict";
import { buildTimeline } from "../src/components/chat/timeline";
import type { TimelineEntry, ToolLeaf } from "../src/components/chat/timeline";
import { bgCommandLine, formatBgElapsed } from "../src/utils/bgFormat";
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

/** 取某一轮里所有 tools 条目的叶子。 */
function leavesOf(entries: TimelineEntry[]): ToolLeaf[] {
  const out: ToolLeaf[] = [];
  for (const e of entries) {
    if (e.kind !== "turn") continue;
    for (const it of e.items) {
      if (it.kind !== "tools") continue;
      for (const n of it.nodes) if (n.kind === "leaf") out.push(n.leaf);
    }
  }
  return out;
}

const notice = (id: number, kind: string, extra: Record<string, unknown>): MessageOut =>
  msg(id, 7, MsgType.Text, { text: "[摘要文本]", mailbox: { kind, priority: 0 }, ...extra }, SenderType.System);

const user = msg(1, 7, MsgType.Text, { text: "构建一下" }, SenderType.User);

// ── ① + ② 完成通知：一条一行，伪工具名区分动词 ───────────────────────────────

{
  const entries = buildTimeline([
    user,
    notice(2, "bg_command_done", {
      bg_items: [
        { shell_id: "bg_a", command: "npm run build", returncode: 0, elapsed: 102.3, tail: "built in 3s" },
        { shell_id: "bg_b", command: "npm test", returncode: 1, elapsed: 8, tail: "1 failed" },
      ],
    }),
  ]);
  const leaves = leavesOf(entries);
  assert.equal(leaves.length, 2, "两条命令两行");
  assert.equal(leaves[0].tool, "bg_command_done");
  assert.equal(leaves[0].ok, true, "退出码 0 = 成功");
  assert.equal(leaves[0].args.command, "npm run build");
  assert.ok(leaves[0].output.includes("退出码 0") && leaves[0].output.includes("1 分 42 秒"), "展开区含退出码与耗时");
  assert.ok(leaves[0].output.includes("built in 3s"), "展开区含日志尾部");
  assert.equal(leaves[1].ok, false, "非 0 退出码标失败");
  assert.ok(leaves[1].output.includes("退出码 1"));
  // 不再有独立卡片条目
  const kinds = entries.flatMap((e) => (e.kind === "turn" ? e.items.map((i) => i.kind) : []));
  assert.ok(!kinds.includes("bg-command" as never));
}

// ── 用户停止：不算失败 ───────────────────────────────────────────────────────

{
  const entries = buildTimeline([
    user,
    notice(3, "bg_command_stopped", {
      bg_items: [{ shell_id: "bg_c", command: "npm run dev", returncode: 1, elapsed: 725, tail: "", stopped_by: "user" }],
    }),
  ]);
  const [leaf] = leavesOf(entries);
  assert.equal(leaf.tool, "bg_command_stopped");
  assert.equal(leaf.ok, true, "被用户停止的退出码不是失败");
  assert.ok(leaf.output.includes("用户手动停止") && leaf.output.includes("12 分 05 秒"));
}

// ── ③ 旧消息没有 bg_items：整段文本进展开区 ────────────────────────────────────

{
  const entries = buildTimeline([user, notice(4, "bg_command_done", {})]);
  const [leaf] = leavesOf(entries);
  assert.equal(leaf.tool, "bg_command_done");
  assert.equal(leaf.output, "[摘要文本]");
  assert.equal(leaf.ok, true);
}

// ── 通知夹在两次普通工具调用之间时，不并入工具簇 ───────────────────────────────

{
  const call = (id: number, key: string) => msg(id, 7, MsgType.ToolCall, { tool: "fs_read", args: { path: "a.ts" }, call_key: key });
  const result = (id: number, key: string) => msg(id, 7, MsgType.ToolResult, { tool: "fs_read", call_key: key, ok: true, output: "x" });
  const entries = buildTimeline([
    user,
    call(2, "k1"), result(3, "k1"),
    notice(4, "bg_command_done", { bg_items: [{ shell_id: "bg_d", command: "make", returncode: 0, elapsed: 1, tail: "" }] }),
    call(5, "k2"), result(6, "k2"),
  ]);
  const toolItems = entries.flatMap((e) => (e.kind === "turn" ? e.items.filter((i) => i.kind === "tools") : []));
  assert.equal(toolItems.length, 3, "读取 / 通知 / 读取各自成条，通知不被吞进操作合并行");
}

// ── ④ 纯函数 ────────────────────────────────────────────────────────────────

assert.equal(formatBgElapsed(0), "0 秒");
assert.equal(formatBgElapsed(45.9), "45 秒");
assert.equal(formatBgElapsed(192), "3 分 12 秒");
assert.equal(formatBgElapsed(3725), "1 小时 02 分");
assert.equal(formatBgElapsed(Number.NaN), "0 秒");
assert.equal(bgCommandLine("  \n  npm   run  dev\nsecond line"), "npm run dev");
assert.equal(bgCommandLine(""), "");

console.log("bg-notice.test: ok");
