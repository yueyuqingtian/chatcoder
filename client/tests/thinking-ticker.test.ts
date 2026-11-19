import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { buildSync } from "esbuild";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import type { ThinkingTicker as ThinkingTickerComponent } from "../src/components/chat/ThinkingTicker";

const { outputFiles } = buildSync({
  entryPoints: ["src/components/chat/ThinkingTicker.tsx"],
  bundle: true,
  format: "cjs",
  platform: "node",
  packages: "external",
  write: false,
});
const componentModule = { exports: {} as { ThinkingTicker: typeof ThinkingTickerComponent } };
new Function("require", "module", "exports", outputFiles[0].text)(
  createRequire(import.meta.url), componentModule, componentModule.exports,
);
const { ThinkingTicker } = componentModule.exports;

// plan-20261119-thinking-preview：用户收起已有思考时首帧即可看到文字，尾部空行不会顶掉预览。
for (const tail of ["", "\n", "\n\n", "\r\n \t\r\n"]) {
  const html = renderToStaticMarkup(createElement(ThinkingTicker, { text: `先检查设计\n核对截图细节${tail}` }));
  assert.match(html, /thinking-ticker-line">核对截图细节</);
  assert.doesNotMatch(html, /先检查设计|正在深入思考/);
}

for (const text of ["", " \t\r\n"]) {
  const html = renderToStaticMarkup(createElement(ThinkingTicker, { text }));
  assert.match(html, /thinking-ticker-line">正在深入思考…</);
}

console.log("通过：折叠思考首帧显示最近一行有内容的文字，空内容保留占位提示");
