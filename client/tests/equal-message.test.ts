import assert from "node:assert/strict";
import { equalJson } from "../src/utils/equalMessage.ts";
assert.equal(equalJson({ text: "甲乙", attachments: [{ path: "a" }] }, { text: "甲丙", attachments: [{ path: "a" }] }), false);
assert.equal(equalJson({ text: "相同", nested: { rows: [1, null, true] } }, { text: "相同", nested: { rows: [1, null, true] } }), true);
assert.equal(equalJson({ a: undefined }, { b: undefined }), false);
assert.equal(equalJson([], {}), false);
console.log("同长度正文与嵌套内容更新回归通过");
