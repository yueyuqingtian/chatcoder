/** 消息流交互界面（plan-interactive-ui）核心纯逻辑回归测试。
 *
 *  覆盖：
 *  1. buildTimeline 将 present_ui / PresentUi / mcp__*__present_ui 拆为独立 interactive-ui 条目；
 *  2. isOutsideProcess 与 splitFlowSegments 确保其作为 sticky 常显，不进入过程折叠；
 *  3. stripRefLines 正确剥离「已选交互方案：…」后缀行；
 *  4. buildSandboxDocument 对片段与完整文档正确注入 CSP、主题变量与通信桥。
 */
import assert from "node:assert/strict";
import { MsgType, SenderType } from "@chatcoder/shared";
import type { MessageOut } from "@chatcoder/shared";
import { buildTimeline, isOutsideProcess, splitFlowSegments } from "../src/components/chat/timeline";
import { stripRefLines } from "../src/utils/refLines";
import { buildSandboxDocument } from "../src/components/chat/sandboxDoc";

function msg(
  id: number,
  turnId: number,
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

// 1. 时间线归类与常显分段
{
  const msgs = [
    msg(1, 10, MsgType.Text, { text: "设计两个版式给我选" }, SenderType.User),
    msg(2, 10, MsgType.ToolCall, { tool: "fs_read", call_key: "c1", args: { path: "a.ts" } }),
    msg(3, 10, MsgType.ToolResult, { tool: "fs_read", call_key: "c1", ok: true, output: "ok" }),
    msg(4, 10, MsgType.ToolCall, { tool: "present_ui", call_key: "ui1", args: { title: "比选卡", html: "<div>hi</div>" } }),
    msg(5, 10, MsgType.ToolResult, {
      tool: "present_ui",
      call_key: "ui1",
      ok: true,
      output: "已展示",
      ui_payload: { title: "比选卡", variants: [{ id: "v1", label: "方案A", html: "<div>A</div>" }] },
    }),
    msg(6, 10, MsgType.ToolCall, { tool: "mcp__workspace-tools-abc__present_ui", call_key: "ui2", args: { title: "ta3卡" } }),
    msg(7, 10, MsgType.Text, { text: "请从以上方案中选择。" }),
  ];

  const entries = buildTimeline(msgs);
  assert.equal(entries.length, 1);
  const turn = entries[0];
  assert.equal(turn.kind, "turn");
  const kinds = turn.items.map((it) => it.kind);
  assert.deepEqual(kinds, ["user", "tools", "interactive-ui", "interactive-ui", "text"]);

  const uiItem = turn.items[2];
  assert.equal(uiItem.kind, "interactive-ui");
  assert.equal(uiItem.callKey, "ui1");
  assert.ok(uiItem.result, "present_ui 工具结果应挂接到 interactive-ui 条目");
  assert.equal(isOutsideProcess(uiItem), true, "交互界面卡必须常显，不随过程折叠");

  const finalIdx = turn.items.findIndex((it) => it.kind === "text" && it.msg.id === 7);
  const segs = splitFlowSegments(turn.items, 0, finalIdx, 0);
  assert.deepEqual(
    segs.map((s) => s.kind),
    ["process", "sticky", "sticky"],
    "process 段与两个 sticky 交互界面卡应严格分离",
  );
}

// 2. stripRefLines 剥离已选交互方案后缀行
{
  const raw = "请把按钮改小一点\n\n已选交互方案：B · 席位滑块（定价卡 · v2）";
  assert.equal(stripRefLines(raw, true), "请把按钮改小一点");
  assert.equal(stripRefLines(raw, false), raw);
}

// 3. 沙箱文档包装：CSP + 主题变量 + 通信桥
{
  const fragDoc = buildSandboxDocument("<div>Hello</div>", "inst-1", "dark", { "--bg-card": "#212121" });
  assert.ok(fragDoc.includes('data-theme="dark"'));
  assert.ok(fragDoc.includes("connect-src 'none'"));
  assert.ok(fragDoc.includes("--bg-card:#212121;"));
  assert.ok(fragDoc.includes("window.chatcoder"));
  assert.ok(fragDoc.includes('<div id="__cc_root"><div>Hello</div></div>'));

  const fullDoc = buildSandboxDocument("<!doctype html><html><head><title>T</title></head><body><main>X</main></body></html>", "inst-2", "light", { "--text-1": "#111" });
  assert.ok(fullDoc.includes("connect-src 'none'"));
  assert.ok(fullDoc.includes("--text-1:#111;"));
  assert.ok(fullDoc.includes("window.chatcoder"));
  assert.ok(fullDoc.includes("<main>X</main>"));
}

console.log("interactive-ui.test.ts: all assertions passed");
