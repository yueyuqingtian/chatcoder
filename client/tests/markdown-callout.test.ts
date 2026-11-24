/** GFM 语义提示块（callout.ts）纯函数单元测试（plan-seamless-flow-ui）。 */
import assert from "node:assert/strict";
import { createElement } from "react";
import { parseCalloutChildren } from "../src/components/ui/callout";
import { nodeText } from "../src/components/ui/markdownData";

// 1. 标准多行 [!NOTE]
{
  const el = createElement("p", { className: "md-p" }, "[!NOTE]\n所有批量接口均采用增量覆盖协议。");
  const parsed = parseCalloutChildren(["\n", el, "\n"]);
  assert.ok(parsed);
  assert.equal(parsed.kind, "note");
  assert.equal(parsed.tone, "info");
  assert.equal(parsed.title, "提示");
  assert.equal(nodeText(parsed.body).trim(), "所有批量接口均采用增量覆盖协议。");
}

// 2. 自定义同行标题 + 正文（大小写不敏感）
{
  const el = createElement("p", null, "[!warning] 窄面板适配\n侧边对话宽度 ≤360px 时自动单列收敛。");
  const parsed = parseCalloutChildren(el);
  assert.ok(parsed);
  assert.equal(parsed.kind, "warning");
  assert.equal(parsed.tone, "warn");
  assert.equal(parsed.title, "注意 · 窄面板适配");
  assert.equal(nodeText(parsed.body).trim(), "侧边对话宽度 ≤360px 时自动单列收敛。");
}

// 3. 单行简写（无第二行正文时保留默认标题，把同行内容放入正文区）
{
  const el = createElement("p", null, "[!TIP] 改动完成后请先运行 npm run typecheck");
  const parsed = parseCalloutChildren(el);
  assert.ok(parsed);
  assert.equal(parsed.kind, "tip");
  assert.equal(parsed.tone, "tip");
  assert.equal(parsed.title, "建议");
  assert.equal(nodeText(parsed.body).trim(), "改动完成后请先运行 npm run typecheck");
}

// 4. 首段含行内 code 与后续段落
{
  const p1 = createElement("p", null, "[!IMPORTANT]\n使用 ", createElement("code", null, "present_ui"), " 展示。");
  const p2 = createElement("p", null, "支持单选与多选。");
  const parsed = parseCalloutChildren([p1, "\n", p2]);
  assert.ok(parsed);
  assert.equal(parsed.kind, "important");
  assert.equal(parsed.tone, "important");
  assert.equal(parsed.title, "重要");
  assert.equal(parsed.body.length, 2);
  assert.equal(nodeText(parsed.body[0]), "使用 present_ui 展示。");
  assert.equal(nodeText(parsed.body[1]), "支持单选与多选。");
}

// 5. 首段仅含标记，正文在下一段（[!CAUTION]）
{
  const p1 = createElement("p", null, "[!CAUTION]");
  const p2 = createElement("p", null, "此操作不可撤销。");
  const parsed = parseCalloutChildren([p1, p2]);
  assert.ok(parsed);
  assert.equal(parsed.kind, "caution");
  assert.equal(parsed.tone, "danger");
  assert.equal(parsed.title, "警示");
  assert.equal(parsed.body.length, 1);
  assert.equal(nodeText(parsed.body[0]), "此操作不可撤销。");
}

// 6. 普通引用块与非匹配内容返回 null
{
  assert.equal(parseCalloutChildren(createElement("p", null, "这是一段普通引用文本。")), null);
  assert.equal(parseCalloutChildren(createElement("p", null, "[!UNKNOWN]\n未知标签")), null);
  assert.equal(parseCalloutChildren("   "), null);
}

console.log("markdown-callout.test.ts: all assertions passed");
