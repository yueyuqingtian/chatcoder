/**
 * 乐观用户消息配对测试（plan-549-2281 复盘）。
 *
 * 复现缺陷：本文件覆盖的两处都曾按「文本相等取第一条」匹配真实消息——
 *  · 临时 id 用 `Date.now()`：同一毫秒连发两条时 id 相同，`addMessage` 的去重
 *    直接把第二条吞掉（`messages.some(m => m.id === msg.id)`）；
 *  · `_mergeMessages` / `_mergeRefreshed` / `addMessage` / `message.created` 四处
 *    都 `fetched.find(fm => _sameUserContent(...))`，连发两条「继续」时两条占位
 *    都配到同一条真实消息，另一条在合并后的 map 里消失。
 *
 * 现在的口径：只有「本次拉取中新出现的真实消息」是候选，占位按插入顺序、候选按 id 升序
 * 依次消费，一条真实消息最多配对一条占位（见 store/chat.ts 的 pairOptimisticMessages）。
 *
 * 用法（client 目录）：`npm test -- optimistic`，或直跑
 *   node --experimental-transform-types --import ./scripts/ts-resolve.mjs ./tests/optimistic-pairing.test.ts
 */
import assert from "node:assert/strict";
import { pairOptimisticMessages } from "../src/store/chat.ts";
import type { MessageOut } from "../src/api/client.ts";

let failures = 0;
function ok(cond: boolean, msg: string) {
  if (!cond) { failures++; console.error(`FAIL: ${msg}`); }
  else console.log(`ok: ${msg}`);
}

/** 构造一条消息；乐观占位用 id > 1e12（与 store 的临时 id 值域一致）。 */
function msg(id: number, text: string, sender: "user" | "assistant" = "user"): MessageOut {
  return {
    id,
    session_id: 1,
    turn_id: null,
    thread_id: null,
    sender_type: sender,
    sender_id: null,
    msg_type: "text",
    content: { text },
    token_usage: 0,
    created_at: "2026-01-01T00:00:00.000Z",
  } as MessageOut;
}

// ① 连续两条同文本：两条占位各自配到一条真实消息，不互相吞掉
{
  const existing = [msg(1_000_000_000_001, "继续"), msg(1_000_000_000_002, "继续")];
  const fetched = [msg(11, "继续"), msg(12, "继续")];
  const { pairs, pending } = pairOptimisticMessages(existing, fetched);
  ok(pairs.size === 2, "两条占位都配到了真实消息（不再共用同一条）");
  ok(pairs.get(1_000_000_000_001)?.id === 11, "第一条占位按出现顺序配到 id=11");
  ok(pairs.get(1_000_000_000_002)?.id === 12, "第二条占位配到 id=12（不是重复的 11）");
  ok(new Set([...pairs.values()].map((m) => m.id)).size === 2, "一条真实消息不会被两条占位同时消费");
  ok(pending.length === 0, "没有剩余未配对占位");
}

// ② 历史里已有同文本消息：占位不得被历史消息「吃掉」
{
  const existing = [msg(11, "继续"), msg(1_000_000_000_003, "继续")];
  const fetched = [msg(11, "继续"), msg(12, "继续")];
  const { pairs } = pairOptimisticMessages(existing, fetched);
  ok(pairs.get(1_000_000_000_003)?.id === 12, "已确认过的同文本历史消息不参与配对，占位配到本次新落库的那条");
}

// ③ 真实消息还没落库：占位保留为 pending，交回调用方原样保留
{
  const existing = [msg(1_000_000_000_004, "继续")];
  const { pairs, pending } = pairOptimisticMessages(existing, [msg(11, "别的输入")]);
  ok(pairs.size === 0, "文本不一致时不配对");
  ok(pending.length === 1 && pending[0].id === 1_000_000_000_004, "未配对的占位进入 pending 等待落库");
}

// ④ 单条到达（addMessage / message.created 的口径）：只消费第一条匹配的占位
{
  const existing = [msg(1_000_000_000_005, "继续"), msg(1_000_000_000_006, "继续")];
  const { pairs } = pairOptimisticMessages(existing, [msg(21, "继续")]);
  ok(pairs.size === 1 && pairs.get(1_000_000_000_005)?.id === 21, "单条真实消息只替换第一条占位，保持原位顺序");
}

// ⑤ 只发附件（空文本）仍按附件 file_id 集合配对，且同样是一对一
{
  const withFile = (id: number, fileId: string): MessageOut => ({
    ...msg(id, ""),
    content: { text: "", attachments: [{ file_id: fileId }] },
  } as MessageOut);
  const existing = [withFile(1_000_000_000_007, "f1"), withFile(1_000_000_000_008, "f2")];
  const fetched = [withFile(31, "f1"), withFile(32, "f2")];
  const { pairs } = pairOptimisticMessages(existing, fetched);
  ok(pairs.get(1_000_000_000_007)?.id === 31, "只发附件：按 file_id 配到 f1");
  ok(pairs.get(1_000_000_000_008)?.id === 32, "只发附件：按 file_id 配到 f2");
}

// ⑥ 非 user 消息不参与配对（AI 回复的文本恰好相同也不得顶掉占位）
{
  const existing = [msg(1_000_000_000_009, "继续")];
  const { pairs } = pairOptimisticMessages(existing, [msg(41, "继续", "assistant")]);
  ok(pairs.size === 0, "assistant 消息不参与乐观占位配对");
}

// ⑦ 无占位时零开销：不产生任何配对或 pending
{
  const { pairs, pending } = pairOptimisticMessages([msg(1, "继续")], [msg(2, "继续")]);
  ok(pairs.size === 0 && pending.length === 0, "没有占位时直接返回空结果");
}

console.log(failures === 0 ? "\nALL PASS" : `\n${failures} FAILURE(S)`);
process.exit(failures === 0 ? 0 : 1);
