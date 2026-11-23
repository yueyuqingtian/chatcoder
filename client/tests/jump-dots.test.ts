import assert from "node:assert/strict";
import type { MessageOut, SessionOutlineItem } from "../src/api/client.ts";
import {
  buildJumpDotItems,
  computeBlockSize,
  findActiveDot,
  resolveBlockWindow,
} from "../src/components/chat/jumpDotsModel.ts";
import { userMessagePreview, type TimelineEntry } from "../src/components/chat/timeline.ts";

function userMsg(id: number, turnId: number, content: Record<string, unknown>): MessageOut {
  return {
    id,
    session_id: 1,
    turn_id: turnId,
    thread_id: null,
    sender_type: "user",
    sender_id: null,
    msg_type: "text",
    content,
    token_usage: 0,
    created_at: "2026-11-21T07:52:00Z",
  };
}

// 1. 摘要提取：剥离引用行并保留附件/引用结构
{
  const msg = userMsg(10, 1, {
    text: "打包 0.10.6\n\n引用文件夹：D:\\myProject\\chatcoder\\test",
    refs: [{ kind: "dir", value: "D:\\myProject\\chatcoder\\test", label: "test" }],
    attachments: [
      { file_id: "f1", filename: "a.png", path: "f1/a.png", url: "/api/uploads/f1/a.png", size: 1024, mime_type: "image/png", type: "image" },
    ],
  });
  const preview = userMessagePreview(msg);
  assert.equal(preview.text, "打包 0.10.6");
  assert.equal(preview.refs.length, 1);
  assert.equal(preview.attachments.length, 1);
  assert.equal(preview.attachments[0].filename, "a.png");
}

// 2. 分页大纲合并：未加载分页条目标记 loaded=false，乐观新消息不丢且不重复
{
  const outline: SessionOutlineItem[] = [
    { message_id: 1, turn_id: 1, created_at: "2026-11-21T01:00:00Z", text: "第一页问题", attachments: [], refs: [] },
    { message_id: 20, turn_id: 2, created_at: "2026-11-21T02:00:00Z", text: "已加载问题", attachments: [], refs: [] },
  ];
  const entries: TimelineEntry[] = [
    { kind: "turn", turnId: 2, items: [{ kind: "user", msg: userMsg(20, 2, { text: "已加载问题" }) }] },
    { kind: "turn", turnId: null, items: [{ kind: "user", msg: userMsg(1e12 + 5, 3, { text: "刚发送乐观消息" }) }] },
  ];
  const items = buildJumpDotItems(entries, outline);
  assert.equal(items.length, 3);
  assert.equal(items[0].loaded, false);
  assert.equal(items[0].entryIndex, -1);
  assert.equal(items[1].loaded, true);
  assert.equal(items[1].entryIndex, 0);
  assert.equal(items[2].text, "刚发送乐观消息");
  assert.equal(items[2].entryIndex, 1);

  // 跨页空洞跳页后，findActiveDot 忽略未加载项（entryIndex = -1）
  const gapped: TimelineEntry[] = [
    { kind: "turn", turnId: 1, items: [{ kind: "user", msg: userMsg(1, 1, { text: "第一页问题" }) }] },
    ...entries,
  ];
  const outline3: SessionOutlineItem[] = [
    ...outline,
    { message_id: 30, turn_id: 4, created_at: "2026-11-21T03:00:00Z", text: "中间未加载", attachments: [], refs: [] },
  ];
  // 让第 2 条保持未加载、第 1 条已加载在下标 0、第 3 条（30）未加载
  const itemsGap = buildJumpDotItems([gapped[0]], outline3);
  assert.equal(itemsGap[0].loaded, true);
  assert.equal(itemsGap[1].loaded, false);
  assert.equal(itemsGap[2].loaded, false);
  assert.equal(findActiveDot(itemsGap, 0), 0);
}

// 3. 分区容量与窗口求解
{
  assert.equal(computeBlockSize(null), 15);
  assert.equal(computeBlockSize(600), 15);
  assert.equal(computeBlockSize(160), 8); // 矮窗口收紧下限 8
  assert.equal(computeBlockSize(200), 11);

  const shortWin = resolveBlockWindow(12, 15, 10, null);
  assert.equal(shortWin.showPager, false);
  assert.equal(shortWin.start, 0);
  assert.equal(shortWin.end, 12);

  const longDefault = resolveBlockWindow(46, 15, 44, null);
  assert.equal(longDefault.showPager, true);
  assert.equal(longDefault.blockCount, 4);
  assert.equal(longDefault.currentBlock, 2);
  assert.equal(longDefault.start, 30);
  assert.equal(longDefault.end, 45);

  const manual = resolveBlockWindow(46, 15, 44, 0);
  assert.equal(manual.currentBlock, 0);
  assert.equal(manual.start, 0);
  assert.equal(manual.end, 15);
}

console.log("JumpDots v2 模型与分区单测全部通过");
